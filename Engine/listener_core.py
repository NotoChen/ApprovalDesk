#!/usr/bin/env python3
"""监听钉钉代码审批消息，并通过 glab 自动审批、合并和添加“收到”表态。"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote, unquote, urlsplit


DEFAULT_APPROVAL_GROUP_NAME = "代码审批"
DEFAULT_SENDER_GROUP_NAME = "向钱冲"
DEFAULT_GITLAB_HOST = "git2.superboss.cc"
EVENT_KEY = "user_im_message_receive_at"
RECEIVED_REACTION = "OK"
MAX_URLS = 30
MAX_CONCURRENT_MRS = 3
QUEUE_SIZE = 100
RECENT_MESSAGE_LIMIT = 2048
COMMAND_TIMEOUT_SECONDS = 90
MERGE_VERIFY_TIMEOUT_SECONDS = 60
MERGE_VERIFY_INTERVAL_SECONDS = 2
RETRY_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (2, 5)
MAX_ERROR_DETAIL_LENGTH = 500
LOCK_POLL_INTERVAL_SECONDS = 1
LOCK_STATE_TIMEOUT_SECONDS = 5
RECONNECT_DELAYS = (2, 5, 10, 20, 30)
DWS_BUS_STOP_TIMEOUT_SECONDS = 10
MAX_DWS_STATUS_LINES = 30
MAX_NOTIFICATION_LENGTH = 220
SCRIPT_PATH = Path(__file__).resolve()
DWS_INSTALL_SCRIPT_URL = (
    "https://raw.githubusercontent.com/DingTalk-Real-AI/"
    "dingtalk-workspace-cli/main/scripts/install.sh"
)
DWS_OFFICIAL_URL = (
    "https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli#installation"
)
GLAB_OFFICIAL_URL = "https://docs.gitlab.com/cli/"
GLAB_INSTALL_OPTIONS_URL = (
    "https://gitlab.com/gitlab-org/cli/-/blob/main/docs/installation_options.md"
)
HOMEBREW_OFFICIAL_URL = "https://brew.sh/"
MINIMUM_PYTHON = (3, 10)

URL_RE = re.compile(r"https?://[^\s<>\"']+")
PROJECT_COMPONENT_RE = re.compile(r"[A-Za-z0-9_.-]+")
TRAILING_URL_PUNCTUATION = ").,;:!?]}>,，。；：！？）】》"


class AutomationError(RuntimeError):
    def __init__(
        self,
        message: str,
        detail: str = "",
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.detail = detail
        self.retryable = retryable


class ProcessingStopped(RuntimeError):
    """当前监听正在停止，任务不应继续产生外部副作用。"""


class Console:
    def __init__(self, debug_enabled: bool) -> None:
        self.debug_enabled = debug_enabled
        self._lock = threading.Lock()

    def _write(self, icon: str, message: str) -> None:
        with self._lock:
            print(f"{time.strftime('%H:%M:%S')}  {icon} {message}", flush=True)

    def info(self, message: str) -> None:
        self._write("ℹ️", message)

    def ready(self, message: str) -> None:
        self._write("🟢", message)

    def received(self, message: str) -> None:
        self._write("📨", message)

    def success(self, message: str) -> None:
        self._write("✅", message)

    def warning(self, message: str) -> None:
        self._write("⚠️", message)

    def error(self, message: str) -> None:
        self._write("❌", message)

    def debug(self, message: str) -> None:
        if self.debug_enabled:
            self._write("🔧", message)


class ScreenLockGuard:
    """在 macOS 锁屏期间阻止审批、合并和消息表态。"""

    LOCK_PATTERN = re.compile(r'"IOConsoleLocked"\s*=\s*(Yes|No)')

    def __init__(self, console: Console) -> None:
        self.console = console
        self._state_lock = threading.Lock()
        self._last_locked: bool | None = None
        self._last_error_at = 0.0

    def _read_locked(self) -> bool:
        if sys.platform != "darwin":
            return False

        try:
            completed = subprocess.run(
                ["/usr/sbin/ioreg", "-n", "Root", "-d", "1"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=LOCK_STATE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise AutomationError(f"无法读取 macOS 锁屏状态：{error}") from error

        match = self.LOCK_PATTERN.search(completed.stdout or "")
        if completed.returncode != 0 or match is None:
            detail = "\n".join(
                part
                for part in (
                    (completed.stderr or "").strip(),
                    (completed.stdout or "").strip()[:1000],
                )
                if part
            )
            raise AutomationError("无法识别 macOS 锁屏状态", detail)
        return match.group(1) == "Yes"

    def _report_unavailable(self, error: AutomationError) -> None:
        now = time.monotonic()
        with self._state_lock:
            should_report = now - self._last_error_at >= 30
            if should_report:
                self._last_error_at = now
        if should_report:
            self.console.warning("无法确认锁屏状态，按锁屏处理，暂停审批任务")
            self.console.debug(error.detail or str(error))

    def is_locked_or_unknown(self) -> bool:
        try:
            locked = self._read_locked()
        except AutomationError as error:
            self._report_unavailable(error)
            return True

        with self._state_lock:
            previous = self._last_locked
            self._last_locked = locked
        if previous is None:
            self.console.info(
                "锁屏保护已启用，当前电脑状态：" + ("已锁屏" if locked else "已解锁")
            )
        elif previous != locked:
            if locked:
                self.console.warning("检测到电脑已锁屏，暂停审批任务")
            else:
                self.console.success("电脑已解锁，恢复审批任务")
        return locked

    def wait_until_unlocked(
        self,
        stop_event: threading.Event,
        action: str,
    ) -> bool:
        announced = False
        while not stop_event.is_set():
            if not self.is_locked_or_unknown():
                return True
            if not announced:
                self.console.warning(f"电脑处于锁屏状态，暂缓{action}，解锁后继续")
                announced = True
            stop_event.wait(LOCK_POLL_INTERVAL_SECONDS)
        return False


class Notifier:
    def __init__(self, enabled: bool, console: Console) -> None:
        self.enabled = enabled
        self.console = console
        self._osascript = Path("/usr/bin/osascript")

    def send(self, title: str, message: str, subtitle: str = "") -> bool:
        if not self.enabled:
            return False
        if sys.platform != "darwin" or not self._osascript.is_file():
            self.console.warning("当前系统不支持 macOS 系统通知")
            return False

        compact_message = " ".join(message.split())[:MAX_NOTIFICATION_LENGTH]
        script = """
on run argv
    set notificationTitle to item 1 of argv
    set notificationBody to item 2 of argv
    set notificationSubtitle to item 3 of argv
    if notificationSubtitle is "" then
        display notification notificationBody with title notificationTitle
    else
        display notification notificationBody with title notificationTitle subtitle notificationSubtitle
    end if
end run
"""
        try:
            completed = subprocess.run(
                [str(self._osascript), "-e", script, title, compact_message, subtitle],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            self.console.debug(f"系统通知发送异常：{error}")
            return False
        if completed.returncode != 0:
            self.console.debug(
                "系统通知发送失败："
                + ((completed.stderr or "").strip() or f"退出码 {completed.returncode}")
            )
            return False
        return True


def command_text(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(?:glpat-|token[=: ]+|password[=: ]+|secret[=: ]+)[^\s,;]+"
)


def compact_error_detail(detail: str) -> str:
    cleaned = ANSI_ESCAPE_RE.sub("", detail or "")
    cleaned = SENSITIVE_VALUE_RE.sub("[已隐藏敏感信息]", cleaned)
    compact = " ".join(cleaned.split())
    if len(compact) > MAX_ERROR_DETAIL_LENGTH:
        return compact[:MAX_ERROR_DETAIL_LENGTH] + "..."
    return compact


def classify_command_error(output: str) -> tuple[str, bool]:
    lowered = output.lower()
    if any(word in lowered for word in ("401", "unauthorized", "authentication failed")):
        return "认证失败", False
    if any(word in lowered for word in ("403", "forbidden", "permission denied", "not allowed")):
        return "权限不足", False
    if any(
        word in lowered
        for word in (
            "could not resolve host",
            "connection refused",
            "connection reset",
            "network error",
            "network is unreachable",
            "no such host",
            "temporary failure",
            "timed out",
            "timeout",
            "unexpected eof",
            "broken pipe",
            "tls handshake",
        )
    ):
        return "临时网络故障", True
    if any(
        word in lowered
        for word in ("429", "too many requests", "rate limit", "rate_limit")
    ):
        return "服务端限流", True
    if re.search(r"\b(?:400|422)\b", lowered) or any(
        word in lowered for word in ("bad request", "unprocessable entity")
    ):
        return "请求参数或当前状态不合法", False
    if any(
        word in lowered
        for word in ("500", "502", "503", "504", "internal server error", "bad gateway")
    ):
        return "GitLab 服务暂时不可用", True
    if re.search(r"\b404\b", lowered) or "not found" in lowered:
        return "MR 不存在或当前账号无权访问", False
    if "sha" in lowered and any(
        word in lowered for word in ("does not match", "mismatch", "changed")
    ):
        return "代码版本已经变化", False
    if any(
        word in lowered
        for word in ("conflict", "cannot be merged", "cannot_be_merged")
    ):
        return "合并请求存在冲突或当前不可合并", False
    if any(word in lowered for word in ("pipeline", "ci job", "checks failed")):
        return "流水线或检查条件尚未满足", False
    if any(word in lowered for word in ("merge request is closed", "already closed")):
        return "合并请求已经关闭", False
    return "命令返回失败", False


def friendly_command_error(description: str, output: str, returncode: int) -> str:
    reason, _retryable = classify_command_error(output)
    detail = compact_error_detail(output)
    message = f"{description}失败：{reason}（退出码 {returncode}）"
    if detail:
        message += f"；返回信息：{detail}"
    return message


def dws_error_payload(output: str) -> Mapping[str, Any] | None:
    decoder = json.JSONDecoder()
    for index, character in enumerate(output):
        if character != "{":
            continue
        try:
            payload, _end = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            return payload["error"]
    return None


def dws_listener_error_reason(output: str, returncode: int) -> tuple[str, bool, bool]:
    lowered = output.lower()
    stale_bus = all(
        marker in lowered
        for marker in (
            "discover bus",
            "inspect socket directory",
            "no such file or directory",
            "bus child reported startup failure",
        )
    )
    if stale_bus:
        return "DWS 升级后残留的旧事件总线与当前版本不兼容", True, True
    if "terminal_hold" in lowered:
        return "DWS 已触发订阅保护，请按返回的允许重试时间稍后再启动", False, False
    if "cooldown" in lowered or "in_flight" in lowered:
        return "DWS 正在创建或冷却同一订阅，请稍后再启动", False, False
    if any(
        marker in lowered
        for marker in ("auth_refresh_failed", "unauthorized", "authentication failed")
    ):
        return "DWS 登录状态已失效，请重新登录", False, False
    if any(
        marker in lowered
        for marker in (
            "network error",
            "connection reset",
            "connection closed",
            "network is unreachable",
            "no such host",
            "timed out",
            "timeout",
        )
    ):
        return "钉钉网络连接暂时不可用", False, True

    error = dws_error_payload(output)
    if error is not None:
        category = str(error.get("category") or "").strip()
        retryable = error.get("retryable") is True
        if category == "auth":
            return "DWS 登录或身份认证失败", False, retryable
        if category == "validation":
            return "DWS 监听参数与当前版本不兼容", False, retryable
        if category == "api":
            return "钉钉服务端拒绝了监听请求", False, retryable
        if category == "internal":
            return "DWS 内部运行异常", False, retryable
    return f"DWS 监听进程异常退出（退出码 {returncode}）", False, False


def run_command(
    command: Sequence[str],
    *,
    console: Console,
    description: str,
    env: Mapping[str, str] | None = None,
    timeout: int | None = None,
    retryable_unknown: bool = False,
) -> subprocess.CompletedProcess[str]:
    console.debug(f"执行：{command_text(command)}")
    try:
        completed = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(env) if env is not None else None,
            timeout=COMMAND_TIMEOUT_SECONDS if timeout is None else timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise AutomationError(
            f"{description}超时：可能是网络或服务端暂时无响应",
            retryable=True,
        ) from error
    except OSError as error:
        raise AutomationError(f"{description}无法启动：{error}") from error

    if completed.returncode != 0:
        detail = "\n".join(
            part for part in ((completed.stderr or "").strip(), (completed.stdout or "").strip()) if part
        )
        _reason, retryable = classify_command_error(detail)
        raise AutomationError(
            friendly_command_error(description, detail, completed.returncode),
            detail,
            retryable=retryable or retryable_unknown,
        )
    return completed


def retry_operation(
    action: Callable[[], Any],
    *,
    console: Console,
    description: str,
    stop_event: threading.Event | None = None,
    attempts: int | None = None,
) -> Any:
    attempts = RETRY_ATTEMPTS if attempts is None else attempts
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except AutomationError as error:
            if not error.retryable or attempt >= attempts:
                raise
            delay = RETRY_DELAYS_SECONDS[min(attempt - 1, len(RETRY_DELAYS_SECONDS) - 1)]
            console.warning(
                f"{description}暂时失败（第 {attempt}/{attempts} 次）：{error}；"
                f"{delay} 秒后重试"
            )
            if stop_event is not None:
                if stop_event.wait(delay):
                    raise ProcessingStopped(f"监听正在停止，未继续执行{description}")
            else:
                time.sleep(delay)
    raise AssertionError("重试循环未返回或抛出异常")


def run_read_command(
    command: Sequence[str],
    *,
    console: Console,
    description: str,
    env: Mapping[str, str] | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return retry_operation(
        lambda: run_command(
            command,
            console=console,
            description=description,
            env=env,
            timeout=timeout,
            retryable_unknown=True,
        ),
        console=console,
        description=description,
    )


def parse_json_output(completed: subprocess.CompletedProcess[str], description: str) -> Any:
    output = (completed.stdout or "").strip()
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        detail = compact_error_detail(output)
        message = f"{description}返回了无法识别的数据"
        if detail:
            message += f"；返回信息：{detail}"
        raise AutomationError(message, output) from error


def format_gitlab_time(value: str) -> str:
    if not value:
        return "未知"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M")


@dataclass(frozen=True)
class MRRef:
    project_path: str
    iid: int
    url: str
    gitlab_host: str = DEFAULT_GITLAB_HOST

    @property
    def key(self) -> str:
        return f"{self.project_path}!{self.iid}"

    @property
    def repo_url(self) -> str:
        return f"https://{self.gitlab_host}/{self.project_path}"

    @property
    def api_base(self) -> str:
        encoded = quote(self.project_path, safe="")
        return f"projects/{encoded}/merge_requests/{self.iid}"


@dataclass(frozen=True)
class Identity:
    user_id: int
    name: str
    username: str


@dataclass(frozen=True)
class ContactCandidate:
    name: str
    user_id: str
    open_dingtalk_id: str
    title: str
    nick: str
    flower_name: str

    @property
    def description(self) -> str:
        details = [
            value
            for value in (
                self.nick,
                self.title,
                f"userId={self.user_id}" if self.user_id else "",
            )
            if value
        ]
        if not details:
            return self.name
        return f"{self.name}（{'，'.join(details)}）"


@dataclass(frozen=True)
class GroupCandidate:
    title: str
    open_conversation_id: str
    member_count: int | None


@dataclass
class MRDetails:
    ref: MRRef
    title: str
    state: str
    sha: str
    author_id: int
    author_name: str
    author_username: str
    merged_by_id: int | None
    merged_by_name: str
    work_in_progress: bool
    has_conflicts: bool | None
    merge_status: str
    source_branch: str
    target_branch: str
    changes_count: str
    created_at: str
    updated_at: str
    web_url: str = ""


@dataclass
class ApprovalInfo:
    approvals_required: int
    approvals_left: int
    user_has_approved: bool
    user_can_approve: bool
    approved_by: dict[int, str]


@dataclass
class MRPlan:
    ref: MRRef
    details: MRDetails
    approvals: ApprovalInfo | None
    use_leader: bool
    actor: Identity
    needs_approval: bool
    already_merged: bool
    third_party_approvers: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class MessageTask:
    event_key: str
    message_id: str
    conversation_id: str
    sender_name: str
    refs: tuple[MRRef, ...]


class RecentKeys:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._values: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def add_if_new(self, key: str) -> bool:
        with self._lock:
            if key in self._values:
                self._values.move_to_end(key)
                return False
            self._values[key] = None
            while len(self._values) > self.limit:
                self._values.popitem(last=False)
            return True


def parse_mr_url(raw_url: str, gitlab_host: str = DEFAULT_GITLAB_HOST) -> MRRef:
    candidate = raw_url.rstrip(TRAILING_URL_PUNCTUATION)
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() != "https":
        raise AutomationError(f"只接受 HTTPS GitLab 合并请求链接：{candidate}")
    try:
        parsed_port = parsed.port
    except ValueError as error:
        raise AutomationError(f"GitLab 链接端口格式错误：{candidate}") from error
    if (
        parsed.hostname != gitlab_host
        or parsed_port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise AutomationError(f"不是受信任的 GitLab 地址：{candidate}")

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[-2] != "merge_requests" or not parts[-1].isdigit():
        raise AutomationError(f"合并请求链接不完整：{candidate}")

    project_parts = parts[:-2]
    if project_parts and project_parts[-1] == "-":
        project_parts = project_parts[:-1]
    if len(project_parts) < 2:
        raise AutomationError(f"合并请求项目路径不完整：{candidate}")
    if not all(PROJECT_COMPONENT_RE.fullmatch(part) for part in project_parts):
        raise AutomationError(f"合并请求项目路径包含不支持的字符：{candidate}")

    project_path = "/".join(project_parts)
    iid = int(parts[-1])
    return MRRef(
        project_path=project_path,
        iid=iid,
        # GitLab versions use different browser routes. The original validated
        # link remains usable; API routing and deduplication use project_path/iid.
        url=candidate,
        gitlab_host=gitlab_host,
    )


def extract_mr_refs(
    content: str,
    gitlab_host: str = DEFAULT_GITLAB_HOST,
    max_urls: int | None = None,
) -> tuple[MRRef, ...]:
    limit = MAX_URLS if max_urls is None else max_urls
    raw_urls = URL_RE.findall(content)
    if not raw_urls:
        raise AutomationError("消息中没有发现合并请求链接")

    refs: list[MRRef] = []
    seen: set[str] = set()
    errors: list[str] = []
    suspicious_gitlab_url = False

    for raw_url in raw_urls:
        candidate = raw_url.rstrip(TRAILING_URL_PUNCTUATION)
        parsed = urlsplit(candidate)
        hostname = parsed.hostname or ""
        if hostname == gitlab_host:
            try:
                ref = parse_mr_url(candidate, gitlab_host)
            except AutomationError as error:
                errors.append(str(error))
                continue
            if ref.key not in seen:
                seen.add(ref.key)
                refs.append(ref)
            continue
        if hostname and gitlab_host.startswith(hostname):
            suspicious_gitlab_url = True

    if suspicious_gitlab_url:
        errors.append("发现疑似被截断的 GitLab 链接")
    if errors:
        raise AutomationError("整条消息预检失败：" + "；".join(errors))
    if not refs:
        raise AutomationError("消息中没有可处理的公司 GitLab 合并请求链接")
    if len(refs) > limit:
        raise AutomationError(f"单条消息包含 {len(refs)} 个 MR，超过上限 {limit}")
    return tuple(refs)


def extract_mr_refs_from_message(
    content: str,
    message_detail: Mapping[str, Any] | None = None,
    gitlab_host: str = DEFAULT_GITLAB_HOST,
    max_urls: int | None = None,
) -> tuple[tuple[MRRef, ...], bool]:
    """从当前消息和引用上下文中提取 MR，返回是否使用了引用内容。"""
    contents: list[str] = [content]
    seen_contents = {content}
    used_quote = False
    current: Mapping[str, Any] | None = message_detail
    visited_message_ids: set[str] = set()

    while isinstance(current, Mapping):
        detail_content = current.get("content")
        if isinstance(detail_content, str) and detail_content not in seen_contents:
            contents.append(detail_content)
            seen_contents.add(detail_content)

        quoted = current.get("quotedMessage")
        if not isinstance(quoted, Mapping):
            break
        quoted_id = str(quoted.get("openMessageId") or "").strip()
        if quoted_id:
            if quoted_id in visited_message_ids:
                break
            visited_message_ids.add(quoted_id)
        quoted_content = quoted.get("content")
        if isinstance(quoted_content, str):
            if quoted_content not in seen_contents:
                contents.append(quoted_content)
                seen_contents.add(quoted_content)
            used_quote = True
        current = quoted

    return extract_mr_refs("\n".join(contents), gitlab_host, max_urls), used_quote


def event_key(event: Mapping[str, Any]) -> str:
    value = str(event.get("event_id") or event.get("message_id") or "").strip()
    if not value:
        raise AutomationError("事件缺少唯一标识，无法安全处理")
    return value


def event_matches(
    event: Mapping[str, Any],
    allowed_senders: Mapping[str, ContactCandidate],
    approval_group_id: str,
) -> tuple[bool, str]:
    if str(event.get("type") or EVENT_KEY) != EVENT_KEY:
        return False, "不是 @我 消息事件"
    if str(event.get("conversation_id") or "") != approval_group_id:
        return False, "不是目标审批群"
    sender_id = str(event.get("sender_open_dingtalk_id") or "")
    if sender_id not in allowed_senders:
        return False, "发送人不在允许名单中"
    if not str(event.get("message_id") or "").strip():
        return False, "缺少消息 ID"
    if not isinstance(event.get("content"), str):
        return False, "缺少消息正文"
    return True, ""


class GlabClient:
    def __init__(self, executable: str, console: Console, hostname: str) -> None:
        self.executable = executable
        self.console = console
        self.hostname = hostname
        fallback_token = os.environ.get("GITLAB_FALLBACK_TOKEN", "").strip()
        legacy_token = os.environ.get("GITLAB_LEADER_TOKEN", "").strip()
        self.leader_token = fallback_token or legacy_token
        self.uses_legacy_fallback_token = bool(legacy_token and not fallback_token)

    def validate_capabilities(self) -> None:
        checks = (
            ([self.executable, "api", "--help"], ("--paginate", "--output")),
            ([self.executable, "mr", "approve", "--help"], ("--sha",)),
        )
        for command, required_flags in checks:
            completed = run_command(
                command,
                console=self.console,
                description="检查 glab 命令能力",
            )
            output = "\n".join(
                ((completed.stdout or ""), (completed.stderr or ""))
            )
            missing = [flag for flag in required_flags if flag not in output]
            if missing:
                raise AutomationError(
                    "当前 glab 版本缺少脚本所需参数："
                    + "、".join(missing)
                    + "；请按 GitLab 官方方式升级 glab"
                )

    def environment(self, use_leader: bool) -> dict[str, str]:
        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        env["GLAB_PAGER"] = "cat"
        if use_leader:
            if not self.leader_token:
                raise AutomationError("未设置备用审批 Token，无法处理你本人创建的 MR")
            env["GITLAB_TOKEN"] = self.leader_token
        return env

    def api(self, endpoint: str, description: str, use_leader: bool = False) -> Any:
        completed = run_read_command(
            [
                self.executable,
                "api",
                endpoint,
                "--hostname",
                self.hostname,
            ],
            console=self.console,
            description=description,
            env=self.environment(use_leader),
        )
        return parse_json_output(completed, description)

    def identity(self, use_leader: bool = False) -> Identity:
        description = "验证备用审批账号" if use_leader else "验证当前 GitLab 账号"
        payload = self.api("user", description, use_leader)
        if not isinstance(payload, dict) or payload.get("id") is None:
            raise AutomationError(f"{description}返回缺少用户信息")
        return Identity(
            user_id=int(payload["id"]),
            name=str(payload.get("name") or payload.get("username") or payload["id"]),
            username=str(payload.get("username") or ""),
        )

    def details(self, ref: MRRef, use_leader: bool = False) -> MRDetails:
        payload = self.api(ref.api_base, f"查询 {ref.key}", use_leader)
        if not isinstance(payload, dict):
            raise AutomationError(f"{ref.key} 详情格式异常")
        author = payload.get("author") if isinstance(payload.get("author"), dict) else {}
        # GitLab renamed merged_by to merge_user. Keep each identity intact
        # and prefer the current field, while supporting older servers.
        merged_by = payload.get("merge_user")
        if not isinstance(merged_by, dict) or not merged_by:
            merged_by = payload.get("merged_by")
        if not isinstance(merged_by, dict):
            merged_by = {}
        sha = str(
            payload.get("sha")
            or (
                payload.get("diff_refs", {}).get("head_sha")
                if isinstance(payload.get("diff_refs"), dict)
                else ""
            )
            or ""
        )
        merged_by_id = merged_by.get("id")
        web_url = payload.get("web_url")
        return MRDetails(
            ref=ref,
            title=str(payload.get("title") or ref.key),
            state=str(payload.get("state") or ""),
            sha=sha,
            author_id=int(author.get("id") or 0),
            author_name=str(author.get("name") or author.get("username") or "未知"),
            author_username=str(author.get("username") or ""),
            merged_by_id=int(merged_by_id) if merged_by_id is not None else None,
            merged_by_name=str(merged_by.get("name") or merged_by.get("username") or ""),
            work_in_progress=bool(
                payload.get("work_in_progress") is True or payload.get("draft") is True
            ),
            has_conflicts=(
                bool(payload["has_conflicts"])
                if isinstance(payload.get("has_conflicts"), bool)
                else None
            ),
            merge_status=str(
                payload.get("detailed_merge_status")
                or payload.get("merge_status")
                or ""
            ),
            source_branch=str(payload.get("source_branch") or ""),
            target_branch=str(payload.get("target_branch") or ""),
            changes_count=str(payload.get("changes_count") or ""),
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            web_url=web_url.strip() if isinstance(web_url, str) and web_url.strip() else ref.url,
        )

    def commit_count(self, ref: MRRef, use_leader: bool = False) -> int:
        description = f"查询 {ref.key} 提交数"
        completed = run_read_command(
            [
                self.executable,
                "api",
                f"{ref.api_base}/commits?per_page=100",
                "--hostname",
                self.hostname,
                "--paginate",
                "--output",
                "json",
            ],
            console=self.console,
            description=description,
            env=self.environment(use_leader),
        )
        payload = parse_json_output(completed, description)
        if not isinstance(payload, list):
            raise AutomationError(f"{ref.key} 提交列表格式异常")
        return len(payload)

    def approvals(self, ref: MRRef, use_leader: bool) -> ApprovalInfo:
        payload = self.api(
            f"{ref.api_base}/approvals",
            f"查询 {ref.key} 审批状态",
            use_leader,
        )
        if not isinstance(payload, dict):
            raise AutomationError(f"{ref.key} 审批状态格式异常")
        approved_by: dict[int, str] = {}
        raw_approved_by = payload.get("approved_by")
        if raw_approved_by is None:
            raw_approved_by = []
        if not isinstance(raw_approved_by, list):
            raise AutomationError(f"{ref.key} 审批人列表格式异常")
        for item in raw_approved_by:
            if not isinstance(item, dict) or not isinstance(item.get("user"), dict):
                continue
            user = item["user"]
            if user.get("id") is None:
                continue
            user_id = int(user["id"])
            approved_by[user_id] = str(
                user.get("name") or user.get("username") or user_id
            )
        return ApprovalInfo(
            approvals_required=int(payload.get("approvals_required") or 0),
            approvals_left=int(payload.get("approvals_left") or 0),
            user_has_approved=payload.get("user_has_approved") is True,
            user_can_approve=payload.get("user_can_approve") is True,
            approved_by=approved_by,
        )

    def approve(self, ref: MRRef, sha: str, use_leader: bool) -> None:
        run_command(
            [
                self.executable,
                "mr",
                "approve",
                str(ref.iid),
                "-R",
                ref.repo_url,
                "--sha",
                sha,
            ],
            console=self.console,
            description=f"审批 {ref.key}",
            env=self.environment(use_leader),
            retryable_unknown=True,
        )

    def merge(self, ref: MRRef, sha: str, use_leader: bool) -> None:
        completed = run_command(
            [
                self.executable,
                "api",
                f"{ref.api_base}/merge",
                "--hostname",
                self.hostname,
                "--method",
                "PUT",
                "--field",
                f"sha={sha}",
            ],
            console=self.console,
            description=f"合并 {ref.key}",
            env=self.environment(use_leader),
            retryable_unknown=True,
        )
        parse_json_output(completed, f"合并 {ref.key}")


class DwsClient:
    def __init__(
        self,
        executable: str,
        console: Console,
        profile: str | None = None,
    ) -> None:
        self.executable = executable
        self.console = console
        self.profile = profile

    def command(self, *arguments: str) -> list[str]:
        command = [self.executable]
        if self.profile:
            command.extend(["--profile", self.profile])
        command.extend(arguments)
        return command

    def auth_status(self) -> dict[str, Any]:
        description = "检查 DWS 登录状态"
        completed = run_command(
            self.command("auth", "status", "--format", "json"),
            console=self.console,
            description=description,
        )
        payload = parse_json_output(completed, description)
        if not isinstance(payload, dict):
            raise AutomationError("DWS 登录状态格式异常")
        return payload

    def login_command(self) -> list[str]:
        return self.command("auth", "login", "--recommend")

    def consume_command(self, dry_run: bool = False) -> list[str]:
        command = self.command(
            "event",
            "+listen-im",
            "--kind",
            "at-me",
            "--format",
            "ndjson",
        )
        if dry_run:
            command.append("--dry-run")
        return command

    def validate_consume(self) -> None:
        run_read_command(
            self.consume_command(dry_run=True),
            console=self.console,
            description="检查钉钉长连接配置",
            timeout=COMMAND_TIMEOUT_SECONDS,
        )

    def recover_stale_event_bus(self) -> tuple[bool, str]:
        description = "检查 DWS 事件总线状态"
        try:
            completed = subprocess.run(
                self.command("event", "status", "--format", "json"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return False, f"{description}失败：{error}"
        if completed.returncode != 0:
            return False, friendly_command_error(
                description,
                "\n".join(
                    part
                    for part in (
                        (completed.stderr or "").strip(),
                        (completed.stdout or "").strip(),
                    )
                    if part
                ),
                completed.returncode,
            )
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError:
            return False, "DWS 事件总线状态无法识别"
        if not isinstance(payload, dict):
            return False, "DWS 事件总线状态格式异常"

        subscriptions = payload.get("subscriptions")
        if not isinstance(subscriptions, list):
            return False, "DWS 没有返回可确认的订阅列表"
        if subscriptions:
            return False, "DWS 仍有活跃订阅，为避免中断其他监听，不自动停止总线"

        bus = payload.get("bus")
        if isinstance(bus, dict) and isinstance(bus.get("live"), dict):
            return False, "DWS 事件总线仍能正常响应，为避免误伤其他监听，不自动停止"
        entry = bus.get("entry") if isinstance(bus, dict) else None
        if not isinstance(entry, dict) or entry.get("state") != "running":
            return False, "没有发现仍在运行的旧 DWS 事件总线"
        try:
            pid = int(entry.get("holder_pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid <= 1 or pid == os.getpid():
            return False, "DWS 返回的旧事件总线 PID 无效"

        try:
            process = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "command="],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return False, f"无法核对旧事件总线进程：{error}"
        process_command = (process.stdout or "").strip()
        if process.returncode != 0 or not re.search(
            r"(?:^|/)dws\s+event\s+_bus(?:\s|$)",
            process_command,
        ):
            return False, f"PID {pid} 不是可确认的 DWS 事件总线，不自动停止"

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True, f"旧 DWS 事件总线 {pid} 已经退出"
        except OSError as error:
            return False, f"无法停止旧 DWS 事件总线 {pid}：{error}"

        deadline = time.monotonic() + DWS_BUS_STOP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True, f"已正常停止旧 DWS 事件总线 {pid}"
            except OSError as error:
                return False, f"无法确认旧 DWS 事件总线 {pid} 的状态：{error}"
            time.sleep(0.2)
        return False, f"旧 DWS 事件总线 {pid} 在 {DWS_BUS_STOP_TIMEOUT_SECONDS} 秒内未退出"

    def search_groups(self, query: str) -> list[GroupCandidate]:
        completed = run_read_command(
            self.command(
                "chat",
                "search",
                "--query",
                query,
                "--limit",
                "20",
                "--cursor",
                "0",
                "--format",
                "json",
            ),
            console=self.console,
            description=f"查找群聊 {query}",
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        payload = parse_json_output(completed, f"查找群聊 {query}")
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise AutomationError(f"查找群聊 {query} 返回失败")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise AutomationError(f"查找群聊 {query} 返回格式异常")
        raw_groups = result.get("groups") or []
        if not isinstance(raw_groups, list):
            raise AutomationError(f"查找群聊 {query} 返回格式异常")

        groups: list[GroupCandidate] = []
        for item in raw_groups:
            if not isinstance(item, dict):
                continue
            raw_count = item.get("memberCount")
            try:
                member_count = int(raw_count) if raw_count is not None else None
            except (TypeError, ValueError):
                member_count = None
            groups.append(
                GroupCandidate(
                    title=str(item.get("title") or "").strip(),
                    open_conversation_id=str(
                        item.get("openConversationId")
                        or item.get("openconversationId")
                        or ""
                    ).strip(),
                    member_count=member_count,
                )
            )
        return groups

    def resolve_group(self, group_name: str) -> GroupCandidate:
        groups = self.search_groups(group_name)
        exact_matches = [group for group in groups if group.title == group_name]
        if not exact_matches:
            candidates = "；".join(
                f"{group.title}（成员 {group.member_count or '未知'} 人）"
                for group in groups[:5]
            )
            if candidates:
                raise AutomationError(
                    f"没有找到名称完全匹配的群聊 {group_name}，搜索候选：{candidates}"
                )
            raise AutomationError(f"没有找到名称为 {group_name} 的群聊")
        if len(exact_matches) > 1:
            choices = "；".join(
                f"{group.title}（会话 ID={group.open_conversation_id}）"
                for group in exact_matches
            )
            raise AutomationError(f"群聊 {group_name} 存在重名，请确认：{choices}")
        selected = exact_matches[0]
        if not selected.open_conversation_id:
            raise AutomationError(f"群聊 {group_name} 缺少会话 ID")
        return selected

    def self_contact(self) -> ContactCandidate:
        completed = run_read_command(
            self.command(
                "contact",
                "user",
                "get-self",
                "--format",
                "json",
            ),
            console=self.console,
            description="查询当前钉钉用户",
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        payload = parse_json_output(completed, "查询当前钉钉用户")
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise AutomationError("查询当前钉钉用户返回失败")
        result = payload.get("result")
        if not isinstance(result, list) or len(result) != 1:
            raise AutomationError("查询当前钉钉用户返回格式异常")
        raw_self = result[0]
        if not isinstance(raw_self, dict):
            raise AutomationError("查询当前钉钉用户返回格式异常")
        employee = raw_self.get("orgEmployeeModel")
        if not isinstance(employee, dict):
            raise AutomationError("当前钉钉用户缺少企业身份信息")
        user_id = str(employee.get("userId") or "").strip()
        name = str(employee.get("orgUserName") or "").strip()
        if not user_id or not name:
            raise AutomationError("当前钉钉用户缺少 userId 或姓名")

        matches = [
            candidate
            for candidate in self.search_contacts(name)
            if candidate.name == name and candidate.user_id == user_id
        ]
        if len(matches) != 1:
            raise AutomationError(
                f"无法唯一解析当前钉钉用户 {name}（userId={user_id}）的 openDingTalkId"
            )
        if not matches[0].open_dingtalk_id:
            raise AutomationError(f"当前钉钉用户 {matches[0].description} 缺少 openDingTalkId")
        return matches[0]

    def group_members(
        self,
        group_id: str,
        group_name: str,
    ) -> list[ContactCandidate]:
        members: dict[str, ContactCandidate] = {}
        cursor = "0"
        visited_cursors: set[str] = set()
        while True:
            if cursor in visited_cursors:
                raise AutomationError(f"查询群成员分页游标重复：{cursor}")
            visited_cursors.add(cursor)
            completed = run_read_command(
                self.command(
                    "chat",
                    "group",
                    "members",
                    "--id",
                    group_id,
                    "--cursor",
                    cursor,
                    "--format",
                    "json",
                ),
                console=self.console,
                description=f"查询{group_name}群成员",
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
            payload = parse_json_output(completed, f"查询{group_name}群成员")
            if not isinstance(payload, dict) or payload.get("success") is not True:
                raise AutomationError(f"查询{group_name}群成员返回失败")
            result = payload.get("result")
            if not isinstance(result, dict):
                raise AutomationError(f"查询{group_name}群成员返回格式异常")
            raw_members = result.get("list") or []
            if not isinstance(raw_members, list):
                raise AutomationError(f"查询{group_name}群成员返回格式异常")
            for item in raw_members:
                if not isinstance(item, dict):
                    continue
                open_id = str(
                    item.get("openDingtalkId")
                    or item.get("openDingTalkId")
                    or ""
                ).strip()
                name = str(item.get("memberEmpName") or "").strip()
                if not name:
                    continue
                if not open_id:
                    raise AutomationError(f"群成员 {name} 缺少 openDingTalkId")
                members[open_id] = ContactCandidate(
                    name=name,
                    user_id="",
                    open_dingtalk_id=open_id,
                    title=str(item.get("memberRoleDesc") or "").strip(),
                    nick=str(item.get("memberNick") or "").strip(),
                    flower_name="",
                )
            if result.get("hasMore") is not True:
                break
            next_cursor = str(result.get("nextCursor") or "").strip()
            if not next_cursor:
                raise AutomationError(
                    f"查询{group_name}群成员返回 hasMore，但缺少下一页游标"
                )
            cursor = next_cursor
        return list(members.values())

    def resolve_group_senders(
        self,
        group_name: str,
    ) -> tuple[GroupCandidate, ContactCandidate, dict[str, ContactCandidate]]:
        group = self.resolve_group(group_name)
        self_candidate = self.self_contact()
        members = self.group_members(group.open_conversation_id, group_name)
        if not members:
            raise AutomationError(f"群聊 {group_name} 没有可识别的成员")
        allowed_senders = {
            member.open_dingtalk_id: member
            for member in members
            if member.open_dingtalk_id != self_candidate.open_dingtalk_id
        }
        if not allowed_senders:
            raise AutomationError(f"群聊 {group_name} 排除本人后没有可监听的成员")
        return group, self_candidate, allowed_senders

    def search_contacts(self, query: str) -> list[ContactCandidate]:
        completed = run_read_command(
            self.command(
                "contact",
                "user",
                "search",
                "--query",
                query,
                "--format",
                "json",
            ),
            console=self.console,
            description=f"按姓名查找 {query}",
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        payload = parse_json_output(completed, f"按姓名查找 {query}")
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise AutomationError(f"按姓名查找 {query} 返回失败")
        result = payload.get("result")
        if result is None:
            result = []
        if not isinstance(result, list):
            raise AutomationError(f"按姓名查找 {query} 返回格式异常")

        candidates: list[ContactCandidate] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            candidates.append(
                ContactCandidate(
                    name=str(item.get("name") or "").strip(),
                    user_id=str(item.get("userId") or "").strip(),
                    open_dingtalk_id=str(item.get("openDingTalkId") or "").strip(),
                    title=str(item.get("title") or "").strip(),
                    nick=str(item.get("nick") or "").strip(),
                    flower_name=str(item.get("flowerName") or "").strip(),
                )
            )
        return candidates

    def mark_read(self, conversation_id: str, message_id: str) -> None:
        retry_operation(
            lambda: run_command(
                self.command(
                    "chat",
                    "mark-read",
                    "--conversation-id",
                    conversation_id,
                    "--message-id",
                    message_id,
                    "--format",
                    "json",
                    "--yes",
                ),
                console=self.console,
                description="标记钉钉消息已读",
                timeout=COMMAND_TIMEOUT_SECONDS,
                retryable_unknown=True,
            ),
            console=self.console,
            description="标记钉钉消息已读",
        )

    def message_by_id(self, message_id: str) -> dict[str, Any]:
        completed = run_read_command(
            self.command(
                "chat",
                "message",
                "list-by-ids",
                "--msg-ids",
                message_id,
                "--format",
                "json",
            ),
            console=self.console,
            description="查询钉钉消息及引用内容",
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        payload = parse_json_output(completed, "查询钉钉消息及引用内容")
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise AutomationError("查询钉钉消息及引用内容返回失败")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise AutomationError("查询钉钉消息及引用内容返回格式异常")
        messages = result.get("messages")
        if not isinstance(messages, list):
            raise AutomationError("查询钉钉消息及引用内容返回格式异常")
        for message in messages:
            if not isinstance(message, dict):
                continue
            if str(message.get("openMessageId") or "").strip() == message_id:
                return message
        raise AutomationError("查询钉钉消息及引用内容未返回当前消息")

    def add_received(self, conversation_id: str, message_id: str) -> None:
        retry_operation(
            lambda: run_command(
                self.command(
                    "chat",
                    "message",
                    "add-emoji",
                    "--conversation-id",
                    conversation_id,
                    "--msg-id",
                    message_id,
                    "--emoji",
                    RECEIVED_REACTION,
                    "--format",
                    "json",
                ),
                console=self.console,
                description="添加“收到”表态",
                timeout=COMMAND_TIMEOUT_SECONDS,
                retryable_unknown=True,
            ),
            console=self.console,
            description="添加“收到”表态",
        )

    def status(self) -> Any:
        completed = run_read_command(
            self.command(
                "event",
                "status",
                "--event",
                EVENT_KEY,
                "--format",
                "json",
            ),
            console=self.console,
            description="查询钉钉监听状态",
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        return parse_json_output(completed, "查询钉钉监听状态")


class ApprovalEngine:
    def __init__(
        self,
        glab: GlabClient,
        dws: DwsClient,
        primary_identity: Identity,
        leader_identity: Identity | None,
        console: Console,
        notifier: Notifier,
        screen_lock: ScreenLockGuard,
        stop_event: threading.Event,
    ) -> None:
        self.glab = glab
        self.dws = dws
        self.primary_identity = primary_identity
        self.leader_identity = leader_identity
        self.console = console
        self.notifier = notifier
        self.screen_lock = screen_lock
        self.stop_event = stop_event
        self.own_user_ids = {primary_identity.user_id}
        if leader_identity is not None:
            self.own_user_ids.add(leader_identity.user_id)

    def _wait_until_unlocked(self, action: str) -> None:
        if not self.screen_lock.wait_until_unlocked(self.stop_event, action):
            raise ProcessingStopped(f"监听正在停止，未继续执行{action}")

    def _third_party_names(self, approvals: ApprovalInfo) -> set[str]:
        return {
            name
            for user_id, name in approvals.approved_by.items()
            if user_id not in self.own_user_ids
        }

    def _record_non_own_merge(
        self,
        details: MRDetails,
        no_received_reasons: set[str],
    ) -> None:
        if details.merged_by_id in self.own_user_ids:
            return
        if details.merged_by_id is None:
            no_received_reasons.add(f"{details.ref.key} 无法确认合并人")
            return
        no_received_reasons.add(
            f"{details.ref.key} 由 {details.merged_by_name or '其他人员'} 合并"
        )

    def _preflight_one(self, ref: MRRef, index: int, total: int) -> MRPlan:
        details = self.glab.details(ref)
        prefix = f"[{index}/{total}] {ref.key}"
        if not details.state:
            raise AutomationError(f"{prefix} 缺少状态")

        try:
            commit_count: int | None = self.glab.commit_count(ref)
        except AutomationError as error:
            commit_count = None
            self.console.warning(f"{prefix} 无法读取提交数，将继续处理：{error}")
            self.console.debug(error.detail)
        self.console.info(
            f"{prefix} MR 信息："
            f"{details.source_branch or '未知'} → {details.target_branch or '未知'}，"
            f"变更文件={details.changes_count or '未知'}，"
            f"提交={commit_count if commit_count is not None else '未知'}，"
            f"创建={format_gitlab_time(details.created_at)}，"
            f"更新={format_gitlab_time(details.updated_at)}"
        )

        if details.state == "merged":
            merged_by = details.merged_by_name or "未知人员"
            self.console.warning(f"{prefix} 原本已经由 {merged_by} 合并")
            return MRPlan(
                ref=ref,
                details=details,
                approvals=None,
                use_leader=False,
                actor=self.primary_identity,
                needs_approval=False,
                already_merged=True,
            )
        if details.state == "closed":
            raise AutomationError(f"{prefix} 已关闭")
        if details.state != "opened":
            raise AutomationError(f"{prefix} 状态不支持自动处理：{details.state}")
        if details.work_in_progress:
            raise AutomationError(f"{prefix} 是草稿，禁止自动合并")
        if details.has_conflicts is True:
            raise AutomationError(f"{prefix} 存在代码冲突")
        if not details.sha:
            raise AutomationError(f"{prefix} 缺少 HEAD SHA")

        use_leader = details.author_username == self.primary_identity.username
        if use_leader and self.leader_identity is None:
            raise AutomationError(
                f"{prefix} 由当前账号创建，未配置可选的备用审批账号，已跳过"
            )
        actor = self.leader_identity if use_leader else self.primary_identity
        assert actor is not None

        approvals = self.glab.approvals(ref, use_leader)
        third_party = self._third_party_names(approvals)
        actor_has_approved = (
            actor.user_id in approvals.approved_by or approvals.user_has_approved
        )
        needs_approval = not actor_has_approved

        if needs_approval and not approvals.user_can_approve:
            if approvals.approvals_left > 0:
                raise AutomationError(f"{prefix} 当前账号没有审批权限")
            needs_approval = False

        actor_text = "备用账号" if use_leader else "当前账号"
        self.console.info(
            f"{prefix} 预检通过，作者={details.author_name}，执行账号={actor_text}"
        )
        if third_party:
            self.console.info(
                f"{prefix} 已有其他审批人：{'、'.join(sorted(third_party))}"
                "（不影响“收到”，以最终合并人为准）"
            )
        return MRPlan(
            ref=ref,
            details=details,
            approvals=approvals,
            use_leader=use_leader,
            actor=actor,
            needs_approval=needs_approval,
            already_merged=False,
            third_party_approvers=set(third_party),
        )

    def _approve_and_verify(self, plan: MRPlan) -> ApprovalInfo:
        first_attempt = True

        def attempt() -> ApprovalInfo:
            nonlocal first_attempt
            if first_attempt and plan.approvals is not None:
                approvals = plan.approvals
                first_attempt = False
            else:
                approvals = self.glab.approvals(plan.ref, plan.use_leader)
            plan.third_party_approvers.update(self._third_party_names(approvals))
            actor_has_approved = (
                plan.actor.user_id in approvals.approved_by or approvals.user_has_approved
            )
            if actor_has_approved:
                return approvals

            self._wait_until_unlocked("审批")
            try:
                self.glab.approve(plan.ref, plan.details.sha, plan.use_leader)
            except AutomationError:
                approvals = self.glab.approvals(plan.ref, plan.use_leader)
                plan.third_party_approvers.update(self._third_party_names(approvals))
                actor_has_approved = (
                    plan.actor.user_id in approvals.approved_by
                    or approvals.user_has_approved
                )
                if actor_has_approved:
                    return approvals
                raise
            approvals = self.glab.approvals(plan.ref, plan.use_leader)
            plan.third_party_approvers.update(self._third_party_names(approvals))
            actor_has_approved = (
                plan.actor.user_id in approvals.approved_by or approvals.user_has_approved
            )
            if not actor_has_approved:
                raise AutomationError(
                    f"{plan.ref.key} 审批命令已返回，但暂未确认审批成功",
                    retryable=True,
                )
            return approvals

        return retry_operation(
            attempt,
            console=self.console,
            description=f"审批 {plan.ref.key}",
            stop_event=self.stop_event,
        )

    def _wait_until_merged(self, plan: MRPlan, initial: MRDetails | None = None) -> MRDetails:
        deadline = time.monotonic() + MERGE_VERIFY_TIMEOUT_SECONDS
        last_details = initial if initial is not None else self.glab.details(plan.ref)
        while last_details.state != "merged":
            if last_details.state == "closed":
                raise AutomationError(f"{plan.ref.key} 已关闭而非合并")
            if time.monotonic() >= deadline:
                raise AutomationError(
                    f"{plan.ref.key} 在 {MERGE_VERIFY_TIMEOUT_SECONDS} 秒内未确认合并，"
                    "GitLab 仍显示未合并",
                    retryable=True,
                )
            if self.stop_event.wait(MERGE_VERIFY_INTERVAL_SECONDS):
                raise ProcessingStopped(f"监听正在停止，未继续确认{plan.ref.key}的合并状态")
            last_details = self.glab.details(plan.ref)
        return last_details

    @staticmethod
    def _verification_error(original: AutomationError, verification: AutomationError) -> AutomationError:
        message = str(original)
        verification_message = f"无法确认 MR 最终状态：{verification}"
        if verification_message not in message:
            message += f"；{verification_message}"
        detail = original.detail or str(original)
        verification_detail = verification.detail or str(verification)
        if verification_detail not in detail:
            detail += f"\n{verification_detail}"
        return AutomationError(
            message,
            detail,
            retryable=original.retryable or verification.retryable,
        )

    @staticmethod
    def _check_merge_candidate(plan: MRPlan, current: MRDetails) -> None:
        if current.state == "closed":
            raise AutomationError(f"{plan.ref.key} 已关闭，未合并")
        if current.state != "opened":
            raise AutomationError(f"{plan.ref.key} 状态不支持合并：{current.state or '未知'}")
        if current.sha != plan.details.sha:
            raise AutomationError(f"{plan.ref.key} 代码版本已经变化")

    def _merge_attempt(self, plan: MRPlan) -> MRDetails:
        self._wait_until_unlocked("合并")
        # Recheck before every write, including retries: another client may
        # have finished the merge during approval, polling, or backoff.
        current = self.glab.details(plan.ref)
        if current.state == "merged":
            return current
        self._check_merge_candidate(plan, current)
        self._wait_until_unlocked("合并")
        merge_error: AutomationError | None = None
        initial: MRDetails | None = None
        try:
            self.glab.merge(plan.ref, plan.details.sha, plan.use_leader)
        except AutomationError as error:
            merge_error = error
            self.console.warning(
                f"{plan.ref.key} 合并请求未获成功响应，正在核对 GitLab 最终状态：{error}"
            )
            self.console.debug(error.detail)
            try:
                current = self.glab.details(plan.ref)
            except AutomationError as verification_error:
                raise self._verification_error(error, verification_error) from verification_error
            if current.state == "merged":
                return current
            try:
                self._check_merge_candidate(plan, current)
            except AutomationError as state_error:
                raise AutomationError(f"{state_error}；{error}", error.detail) from error
            if not error.retryable:
                raise
            initial = current

        try:
            return self._wait_until_merged(plan, initial)
        except AutomationError as wait_error:
            if merge_error is None:
                raise
            detail = merge_error.detail or str(merge_error)
            raise AutomationError(
                f"{merge_error}；{wait_error}",
                detail,
                retryable=merge_error.retryable or wait_error.retryable,
            ) from wait_error

    def _merge_until_confirmed(self, plan: MRPlan) -> MRDetails:
        errors: list[AutomationError] = []

        def attempt() -> MRDetails:
            try:
                return self._merge_attempt(plan)
            except AutomationError as error:
                if not any(str(error) in str(previous) for previous in errors):
                    errors.append(error)
                raise

        try:
            return retry_operation(
                attempt,
                console=self.console,
                description=f"合并 {plan.ref.key}",
                stop_event=self.stop_event,
            )
        except AutomationError as error:
            # A later read-only retry must not erase the original merge error.
            if len(errors) == 1 and errors[0] is error:
                raise
            raise AutomationError(
                "；".join(str(previous) for previous in errors),
                "\n".join(previous.detail or str(previous) for previous in errors),
                retryable=error.retryable,
            ) from error

    def _on_merge_confirmed(self, details: MRDetails) -> None:
        who = f"已由 {details.merged_by_name} 合并" if details.merged_by_name else "已合并，GitLab 未返回合并人"
        self.console.success(f"{details.ref.key} {who}")

    def _process_one(
        self,
        ref: MRRef,
        index: int,
        total: int,
    ) -> tuple[int, int, str | None, bool | None, set[str]]:
        prefix = f"[{index}/{total}] {ref.key}"
        approval_count = 0
        no_received_reasons: set[str] = set()
        plan: MRPlan | None = None
        final_details: MRDetails | None = None

        try:
            self.console.info(f"{prefix} 开始独立处理")
            plan = self._preflight_one(ref, index, total)

            if plan.already_merged:
                final_details = plan.details
            else:
                if plan.needs_approval:
                    plan.approvals = self._approve_and_verify(plan)
                    approval_count = 1
                    self.console.success(f"{prefix} 审批成功")
                else:
                    self.console.info(f"{prefix} 当前执行账号已经审批，跳过审批")

                approvals = plan.approvals
                if approvals is None:
                    raise AutomationError(f"{prefix} 缺少审批状态")
                if approvals.approvals_left > 0:
                    self.console.warning(
                        f"{prefix} GitLab 仍显示缺少 {approvals.approvals_left} 个审批，"
                        "按现有规则继续尝试合并"
                    )
                final_details = self._merge_until_confirmed(plan)

            if final_details.state != "merged":
                raise AutomationError(f"{prefix} 最终状态为 {final_details.state}")
        except AutomationError as error:
            # Approval can also lose a race with another merger. Before
            # reporting a terminal failure, reconcile once more without writes.
            final_details = None
            if plan is not None:
                try:
                    current = self.glab.details(ref)
                    if current.state == "merged":
                        final_details = current
                except AutomationError as verification_error:
                    error = self._verification_error(error, verification_error)
            if final_details is None:
                message = f"{prefix} 处理失败：{error}"
                self.console.error(message)
                if error.detail:
                    self.console.debug(error.detail)
                return approval_count, 0, message, None, no_received_reasons
            self.console.info(f"{prefix} 重新核验后确认 MR 已合并")

        # Count the confirmed server state, including an MR merged by someone
        # else. Receipt ownership remains a separate identity check.
        assert final_details is not None
        self._on_merge_confirmed(final_details)
        self._record_non_own_merge(final_details, no_received_reasons)
        self.console.success(f"{prefix} 单 MR 流程完成")
        return (
            approval_count,
            1,
            None,
            final_details.merged_by_id in self.own_user_ids,
            no_received_reasons,
        )

    def process(self, task: MessageTask) -> None:
        total = len(task.refs)
        if total == 1:
            self.console.info("开始处理 1 个 MR")
        else:
            worker_count = min(total, MAX_CONCURRENT_MRS)
            self.console.info(
                f"开始按 MR 并发处理，共 {total} 个 MR，最大并发 {worker_count}"
            )
        no_received_reasons: set[str] = set()
        approval_count = 0
        merge_count = 0
        own_merge_count = 0
        errors: list[str] = []

        results: dict[
            int,
            tuple[int, int, str | None, bool | None, set[str]],
        ] = {}
        if total == 1:
            results[1] = self._process_one(task.refs[0], 1, total)
        else:
            with ThreadPoolExecutor(
                max_workers=min(total, MAX_CONCURRENT_MRS),
                thread_name_prefix="mr-approval",
            ) as executor:
                futures = {
                    executor.submit(self._process_one, ref, index, total): index
                    for index, ref in enumerate(task.refs, 1)
                }
                for future in as_completed(futures):
                    results[futures[future]] = future.result()

        for index in range(1, total + 1):
            approvals, merges, error, merged_by_own_account, reasons = results[index]
            approval_count += approvals
            merge_count += merges
            no_received_reasons.update(reasons)
            if merged_by_own_account is True:
                own_merge_count += 1
            if error is not None:
                errors.append(error)

        if errors:
            summary = (
                f"共 {total} 个 MR：审批 {approval_count} 个，合并 {merge_count} 个；"
                f"{len(errors)} 个 MR 处理失败，未点击“收到”"
            )
            self.console.warning(summary)
            for error in errors:
                self.console.warning(f"失败明细：{error}")
            self.notifier.send(
                "代码审批部分完成",
                summary,
                subtitle=f"发送人：{task.sender_name}",
            )
            return

        if own_merge_count == 0:
            reason = "；".join(sorted(no_received_reasons)) or "所有 MR 均由其他人员合并"
            self.console.warning(
                "全部 MR 均已合并，但没有任何 MR 由当前账号合并，"
                f"不点击“收到”：{reason}"
            )
            self.notifier.send(
                "代码审批处理完成",
                f"{total} 个 MR 均已合并；没有 MR 由当前账号合并，"
                f"未点击“收到”：{reason}",
                subtitle=f"发送人：{task.sender_name}",
            )
            return

        self._wait_until_unlocked("点击“收到”")
        try:
            self.dws.add_received(task.conversation_id, task.message_id)
        except AutomationError as error:
            message = (
                f"{total} 个 MR 已完成：审批 {approval_count} 个，合并 {merge_count} 个；"
                f"但添加“收到”失败：{error}"
            )
            self.console.error(message)
            self.notifier.send(
                "代码审批已完成但收到失败",
                message,
                subtitle=f"发送人：{task.sender_name}",
            )
            return
        self.console.success("已给原钉钉消息添加“👌 收到”表态")
        self.notifier.send(
            "代码审批处理完成",
            f"{total} 个 MR 已完成：审批 {approval_count} 个，合并 {merge_count} 个，并已点击“收到”",
            subtitle=f"发送人：{task.sender_name}",
        )


class ListenerRuntime:
    def __init__(
        self,
        dws: DwsClient,
        engine: ApprovalEngine,
        console: Console,
        notifier: Notifier,
        allowed_senders: Mapping[str, ContactCandidate],
        approval_group: GroupCandidate,
        sender_group: GroupCandidate,
        gitlab_host: str,
        screen_lock: ScreenLockGuard,
        stop_event: threading.Event,
    ) -> None:
        self.dws = dws
        self.engine = engine
        self.console = console
        self.notifier = notifier
        self.allowed_senders = dict(allowed_senders)
        self.approval_group = approval_group
        self.sender_group = sender_group
        self.gitlab_host = gitlab_host
        self.screen_lock = screen_lock
        self.stop_event = stop_event
        self.tasks: queue.Queue[MessageTask | None] = queue.Queue(maxsize=QUEUE_SIZE)
        self.recent_messages = RecentKeys(RECENT_MESSAGE_LIMIT)
        self._reservation_lock = threading.Lock()
        self._reserved_mrs: set[str] = set()
        self._process_lock = threading.Lock()
        self._dws_process: subprocess.Popen[str] | None = None
        self._ready_count = 0
        self._worker_active = threading.Event()

    def request_stop(self, signum: int | None = None) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        if signum is None:
            self.console.info("收到停止指令，正在关闭监听")
        else:
            self.console.info(f"收到停止指令，正在关闭监听（信号 {signum}）")
        with self._process_lock:
            process = self._dws_process
        if process is not None and process.poll() is None:
            process.terminate()

    def _reserve(self, refs: Sequence[MRRef]) -> bool:
        keys = {ref.key for ref in refs}
        with self._reservation_lock:
            if keys & self._reserved_mrs:
                return False
            self._reserved_mrs.update(keys)
            return True

    def _release(self, refs: Sequence[MRRef]) -> None:
        with self._reservation_lock:
            for ref in refs:
                self._reserved_mrs.discard(ref.key)

    def _mark_read(self, task: MessageTask) -> None:
        try:
            self.dws.mark_read(task.conversation_id, task.message_id)
            self.console.success("原钉钉消息已标记为已读")
        except AutomationError as error:
            self.console.warning(f"标记已读失败，但继续处理审批：{error}")
            self.console.debug(error.detail)
            self.notifier.send(
                "代码审批消息已收到",
                f"标记已读失败，但将继续处理：{error}",
                subtitle=f"发送人：{task.sender_name}",
            )

    def _handle_event(self, event: Mapping[str, Any]) -> None:
        matched, reason = event_matches(
            event,
            self.allowed_senders,
            self.approval_group.open_conversation_id,
        )
        if not matched:
            self.console.debug(f"忽略消息：{reason}")
            return
        try:
            key = event_key(event)
        except AutomationError as error:
            self.console.error(str(error))
            return
        if not self.recent_messages.add_if_new(key):
            self.console.debug("忽略当前进程内重复投递的消息")
            return

        sender_id = str(event.get("sender_open_dingtalk_id") or "")
        sender = self.allowed_senders[sender_id]
        sender_name = sender.name
        message_id = str(event["message_id"])
        conversation_id = str(event["conversation_id"])
        content = str(event["content"])

        self.console.received(f"收到 {sender_name} 的代码审批消息")
        locked_at_receive = self.screen_lock.is_locked_or_unknown()

        direct_refs: tuple[MRRef, ...] | None = None
        try:
            direct_refs = extract_mr_refs(content, self.gitlab_host)
        except AutomationError:
            pass

        message_detail: Mapping[str, Any] | None = None
        detail_error: AutomationError | None = None
        try:
            message_detail = self.dws.message_by_id(message_id)
        except AutomationError as error:
            detail_error = error
            if direct_refs is None:
                self.console.warning("无法读取当前消息的引用内容，将继续按消息正文判断")
            self.console.debug(error.detail or str(error))

        try:
            refs, used_quote = extract_mr_refs_from_message(
                content,
                message_detail,
                self.gitlab_host,
            )
        except AutomationError as error:
            if detail_error is not None:
                error = AutomationError(
                    f"{error}；补查引用内容失败：{detail_error}",
                    detail_error.detail or str(detail_error),
                )
            self.console.error(str(error))
            self.notifier.send(
                "代码审批请求无效",
                str(error),
                subtitle=f"发送人：{sender_name}",
            )
            return

        if used_quote:
            self.console.info("已从引用消息上下文中读取并提取 MR 链接")
        self.console.info(
            f"消息包含 {len(refs)} 个 MR："
            + "、".join(ref.key for ref in refs)
        )

        if not self._reserve(refs):
            self.console.warning("消息中包含当前正在处理的 MR，本次不重复排队")
            self.notifier.send(
                "代码审批请求已跳过",
                "消息中包含正在处理的 MR，未重复执行",
                subtitle=f"发送人：{sender_name}",
            )
            return

        task = MessageTask(
            event_key=key,
            message_id=message_id,
            conversation_id=conversation_id,
            sender_name=sender_name,
            refs=refs,
        )
        try:
            self.tasks.put_nowait(task)
        except queue.Full:
            self._release(refs)
            self.console.error("审批队列已满，本条消息未执行")
            self.notifier.send(
                "代码审批队列已满",
                "本条审批请求未执行，请稍后重新发送",
                subtitle=f"发送人：{sender_name}",
            )
            return
        waiting = self.tasks.qsize()
        if locked_at_receive:
            self.console.warning("电脑当前已锁屏，本条审批消息已暂存，解锁后继续处理")
        if waiting > 1:
            self.console.info(f"审批请求已进入队列，当前等待数量 {waiting}")

    def _worker(self) -> None:
        while True:
            task = self.tasks.get()
            if task is None:
                self.tasks.task_done()
                return
            try:
                if self.stop_event.is_set():
                    self.console.warning(
                        f"监听正在停止，未开始处理 {task.sender_name} 的排队请求"
                    )
                    continue
                self._worker_active.set()
                if not self.screen_lock.wait_until_unlocked(
                    self.stop_event,
                    "处理审批消息",
                ):
                    continue
                self._mark_read(task)
                if not self.screen_lock.wait_until_unlocked(
                    self.stop_event,
                    "继续处理审批消息",
                ):
                    continue
                self.notifier.send(
                    "收到代码审批请求",
                    f"收到 {len(task.refs)} 个 MR，开始处理",
                    subtitle=f"发送人：{task.sender_name}",
                )
                self.engine.process(task)
            except ProcessingStopped as error:
                self.console.warning(str(error))
            except AutomationError as error:
                self.console.error(f"审批处理失败：{error}")
                self.console.debug(error.detail)
                self.notifier.send(
                    "代码审批处理失败",
                    str(error),
                    subtitle=f"发送人：{task.sender_name}",
                )
            except Exception as error:
                self.console.error("审批处理发生未预期异常")
                self.console.debug(repr(error))
                self.notifier.send(
                    "代码审批处理异常",
                    "发生未预期异常，未点击“收到”",
                    subtitle=f"发送人：{task.sender_name}",
                )
            finally:
                self._worker_active.clear()
                self._release(task.refs)
                self.tasks.task_done()

    def _relay_dws_status(
        self,
        stream: Any,
        ready_event: threading.Event,
        status_lines: list[str],
    ) -> None:
        for line in stream:
            status = line.strip()
            if not status:
                continue
            status_lines.append(status)
            if len(status_lines) > MAX_DWS_STATUS_LINES:
                del status_lines[:-MAX_DWS_STATUS_LINES]
            if status.startswith("[event] ready"):
                ready_event.set()
                self._ready_count += 1
                if self._ready_count == 1:
                    self.console.ready(
                        f"钉钉监听已就绪，等待{self.sender_group.title}群成员"
                        f"（不含本人）在{self.approval_group.title}群中 @你"
                    )
                    self.notifier.send(
                        "代码审批监听已启动",
                        f"正在等待{self.sender_group.title}群成员（不含本人）"
                        f"在{self.approval_group.title}群中 @你",
                    )
                else:
                    self.console.ready("钉钉长连接已恢复")
                    self.notifier.send(
                        "代码审批监听已恢复",
                        "钉钉长连接已经重新建立",
                    )
                continue
            if status.startswith("[event] bus") and "state=connected" in status:
                self.console.debug("钉钉事件总线已连接")
                continue
            if status.startswith("[event] exited"):
                self.console.debug("钉钉事件消费进程已退出")
                continue
            lowered = status.lower()
            if any(word in lowered for word in ("error", "failed", "fatal")):
                self.console.debug(status)
            else:
                self.console.debug(status)

    def run(self) -> int:
        self.screen_lock.is_locked_or_unknown()
        worker = threading.Thread(
            target=self._worker,
            name="approval-worker",
            daemon=False,
        )
        worker.start()
        reconnect_index = 0

        try:
            while not self.stop_event.is_set():
                ready_event = threading.Event()
                status_lines: list[str] = []
                self.console.info("正在建立钉钉 @我 消息长连接")
                try:
                    process = subprocess.Popen(
                        self.dws.consume_command(),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                    )
                except OSError as error:
                    self.console.error(f"无法启动钉钉监听：{error}")
                    process = None

                if process is not None:
                    with self._process_lock:
                        self._dws_process = process
                    assert process.stdout is not None
                    assert process.stderr is not None
                    status_thread = threading.Thread(
                        target=self._relay_dws_status,
                        args=(process.stderr, ready_event, status_lines),
                        name="dws-status",
                        daemon=True,
                    )
                    status_thread.start()
                    for line in process.stdout:
                        if self.stop_event.is_set():
                            break
                        stripped = line.strip()
                        if not stripped:
                            continue
                        try:
                            payload = json.loads(stripped)
                        except json.JSONDecodeError:
                            self.console.debug(f"忽略无法识别的钉钉事件：{stripped}")
                            continue
                        if isinstance(payload, dict):
                            self._handle_event(payload)

                    returncode = process.wait()
                    status_thread.join(timeout=2)
                    with self._process_lock:
                        self._dws_process = None
                    if self.stop_event.is_set():
                        break
                    if ready_event.is_set():
                        reconnect_index = 0
                    failure_detail = "\n".join(status_lines)
                    reason, stale_bus, retryable = dws_listener_error_reason(
                        failure_detail,
                        returncode,
                    )
                    if stale_bus:
                        self.console.warning(f"钉钉监听无法启动：{reason}，正在安全恢复")
                        recovered, recovery_message = self.dws.recover_stale_event_bus()
                        if recovered:
                            self.console.success(recovery_message)
                            reconnect_index = 0
                        else:
                            self.console.error(f"DWS 事件总线自动恢复失败：{recovery_message}")
                            self.notifier.send(
                                "代码审批监听已停止",
                                f"DWS 事件总线恢复失败：{recovery_message}",
                            )
                            self.stop_event.set()
                    else:
                        if retryable:
                            self.console.warning(f"钉钉监听已断开：{reason}，准备自动重连")
                        else:
                            self.console.error(f"钉钉监听已停止：{reason}，不会盲目重试")
                            self.notifier.send("代码审批监听已停止", reason)
                            self.stop_event.set()
                    self.console.debug(failure_detail)
                    if self.stop_event.is_set():
                        break
                else:
                    returncode = 1

                delay = RECONNECT_DELAYS[min(reconnect_index, len(RECONNECT_DELAYS) - 1)]
                reconnect_index += 1
                self.console.info(f"{delay} 秒后重新连接钉钉")
                if self.stop_event.wait(delay):
                    break
        finally:
            with self._process_lock:
                process = self._dws_process
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if self._worker_active.is_set() or not self.tasks.empty():
                self.console.info("等待当前审批操作安全结束")
            self.tasks.put(None)
            worker.join()
            self.console.success("代码审批监听已停止")
        return 0


def find_running_instances() -> list[tuple[int, str, str]]:
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,etime=,command="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    result: list[tuple[int, str, str]] = []
    script_text = str(SCRIPT_PATH)
    for line in (completed.stdout or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        elapsed, command = parts[1], parts[2]
        try:
            command_parts = shlex.split(command)
        except ValueError:
            command_parts = command.split()
        if not command_parts:
            continue
        executable_name = Path(command_parts[0]).name.lower()
        script_is_argument = any(
            argument == script_text or Path(argument).name == SCRIPT_PATH.name
            for argument in command_parts[1:]
        )
        is_python_process = executable_name.startswith("python")
        if (
            script_is_argument
            and is_python_process
            and "--check" not in command_parts
            and "--status" not in command_parts
            and "--stop" not in command_parts
        ):
            result.append((pid, elapsed, command))
    return result


def acquire_single_instance_lock() -> Any:
    handle = SCRIPT_PATH.open("rb")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise AutomationError("代码审批监听已经在运行") from error
    return handle


@dataclass(frozen=True)
class DependencyPaths:
    dws: str
    glab: str


def confirm_action(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


def run_setup_command(
    command: Sequence[str],
    *,
    console: Console,
    description: str,
) -> None:
    console.info(f"执行：{command_text(command)}")
    try:
        completed = subprocess.run(list(command), check=False)
    except OSError as error:
        raise AutomationError(f"{description}无法启动：{error}") from error
    if completed.returncode != 0:
        raise AutomationError(f"{description}失败（退出码 {completed.returncode}）")


def tool_version(executable: str, tool_name: str) -> str:
    try:
        completed = subprocess.run(
            [executable, "version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AutomationError(f"无法检查 {tool_name} 版本：{error}") from error
    if completed.returncode != 0:
        detail = "\n".join(
            part
            for part in (
                (completed.stderr or "").strip(),
                (completed.stdout or "").strip(),
            )
            if part
        )
        raise AutomationError(
            f"{tool_name} 无法正常运行（退出码 {completed.returncode}）",
            detail,
        )
    lines = [
        line.strip()
        for line in (completed.stdout or "").splitlines()
        if line.strip()
    ]
    if tool_name == "DWS":
        for line in lines:
            if line.startswith("Version:"):
                return line.removeprefix("Version:").strip()
    return lines[0] if lines else "版本未知"


def install_dws(console: Console) -> None:
    curl_path = shutil.which("curl")
    shell_path = shutil.which("sh") or "/bin/sh"
    if not curl_path or not Path(shell_path).exists():
        raise AutomationError(
            "无法运行 DWS 官方安装器：系统缺少 curl 或 sh；"
            f"请参考 {DWS_OFFICIAL_URL}"
        )
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix="dws-official-installer-",
        suffix=".sh",
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        run_setup_command(
            [curl_path, "-fsSL", DWS_INSTALL_SCRIPT_URL, "-o", str(temporary_path)],
            console=console,
            description="下载 DWS 官方安装器",
        )
        run_setup_command(
            [shell_path, str(temporary_path)],
            console=console,
            description="安装 DWS",
        )
    finally:
        temporary_path.unlink(missing_ok=True)


def locate_dws() -> str | None:
    executable = shutil.which("dws")
    if executable:
        return executable
    standard_path = Path.home() / ".local" / "bin" / "dws"
    if standard_path.is_file() and os.access(standard_path, os.X_OK):
        return str(standard_path)
    return None


def ensure_dependencies(console: Console, allow_setup: bool) -> DependencyPaths:
    if sys.version_info < MINIMUM_PYTHON:
        required = ".".join(str(part) for part in MINIMUM_PYTHON)
        current = f"{sys.version_info.major}.{sys.version_info.minor}"
        raise AutomationError(f"需要 Python {required} 或更高版本，当前为 {current}")
    console.success(
        f"Python 检查通过：{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )

    dws_path = locate_dws()
    if not dws_path:
        console.warning("未检测到 DWS")
        console.info(
            "DWS 官方安装命令："
            f"curl -fsSL {DWS_INSTALL_SCRIPT_URL} | sh"
        )
        console.info(f"DWS 官方说明：{DWS_OFFICIAL_URL}")
        if not allow_setup or not confirm_action("是否下载并运行 DWS 官方安装器？"):
            raise AutomationError("DWS 尚未安装，完成官方安装后请重新运行脚本")
        install_dws(console)
        dws_path = locate_dws()
        if not dws_path:
            raise AutomationError(
                "DWS 安装器已结束，但当前 PATH 中仍找不到 dws；"
                "请按安装器提示配置 PATH 后重新运行"
            )
    dws_version = tool_version(dws_path, "DWS")
    console.success(f"DWS 检查通过：{dws_version}（{dws_path}）")

    glab_path = shutil.which("glab")
    if not glab_path:
        console.warning("未检测到 GitLab CLI（glab）")
        brew_path = shutil.which("brew")
        if not brew_path:
            console.info("GitLab 官方支持的 macOS 安装方式需要 Homebrew")
            console.info(f"Homebrew 官方网站：{HOMEBREW_OFFICIAL_URL}")
            console.info(f"GitLab CLI 官方安装说明：{GLAB_INSTALL_OPTIONS_URL}")
            raise AutomationError(
                "glab 尚未安装，且当前没有 Homebrew；"
                "本脚本不会自动安装 Homebrew"
            )
        console.info("GitLab 官方安装命令：brew install glab")
        console.info(f"GitLab CLI 官方说明：{GLAB_OFFICIAL_URL}")
        if not allow_setup or not confirm_action("是否通过 Homebrew 安装 glab？"):
            raise AutomationError("glab 尚未安装，完成官方安装后请重新运行脚本")
        run_setup_command(
            [brew_path, "install", "glab"],
            console=console,
            description="安装 glab",
        )
        glab_path = shutil.which("glab")
        if not glab_path:
            raise AutomationError("glab 安装完成，但当前 PATH 中仍找不到 glab")
    glab_version = tool_version(glab_path, "glab")
    console.success(f"glab 检查通过：{glab_version}（{glab_path}）")
    return DependencyPaths(dws=dws_path, glab=glab_path)


def ensure_dws_authentication(
    dws: DwsClient,
    *,
    console: Console,
    allow_setup: bool,
) -> dict[str, Any]:
    try:
        payload = dws.auth_status()
    except AutomationError as error:
        payload = {}
        console.debug(error.detail or str(error))
    authenticated = payload.get("authenticated") is True
    token_valid = payload.get("token_valid") is True
    if not authenticated or not token_valid:
        console.warning("DWS 尚未登录，或当前登录状态已经失效")
        console.info("DWS 官方登录命令：dws auth login --recommend")
        if not allow_setup or not confirm_action("是否启动 DWS 官方 OAuth 登录？"):
            raise AutomationError("DWS 登录不可用，请完成登录后重新运行脚本")
        run_setup_command(
            dws.login_command(),
            console=console,
            description="登录 DWS",
        )
        payload = dws.auth_status()
        authenticated = payload.get("authenticated") is True
        token_valid = payload.get("token_valid") is True
        if not authenticated or not token_valid:
            raise AutomationError("DWS 登录完成后仍未通过认证检查")

    corp_id = str(payload.get("corp_id") or "").strip()
    user_id = str(payload.get("user_id") or "").strip()
    if not corp_id or not user_id:
        raise AutomationError("DWS 登录状态缺少组织或用户标识")
    dws.profile = f"{corp_id}:{user_id}"
    console.success(
        "DWS 登录检查通过："
        f"{payload.get('corp_name') or corp_id} / "
        f"{payload.get('user_name') or user_id}"
    )
    return payload


def ensure_glab_authentication(
    glab: GlabClient,
    *,
    console: Console,
    allow_setup: bool,
) -> Identity:
    try:
        identity = glab.identity(use_leader=False)
    except AutomationError as error:
        console.warning(f"公司 GitLab 登录检查失败：{error}")
        console.info(
            f"GitLab 官方登录命令：glab auth login --hostname {glab.hostname}"
        )
        injected_token = any(
            os.environ.get(name, "").strip()
            for name in ("GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN")
        )
        if injected_token:
            raise AutomationError(
                "当前环境变量中的 GitLab Token 会覆盖 glab 登录状态；"
                "请先移除或更新失效的 Token"
            ) from error
        if not allow_setup or not confirm_action("是否启动 glab 官方登录流程？"):
            raise AutomationError("公司 GitLab 尚未登录，请完成登录后重新运行脚本") from error
        run_setup_command(
            [
                glab.executable,
                "auth",
                "login",
                "--hostname",
                glab.hostname,
            ],
            console=console,
            description="登录公司 GitLab",
        )
        identity = glab.identity(use_leader=False)
    console.success(
        f"GitLab 登录检查通过：{identity.name}（{identity.username}）@{glab.hostname}"
    )
    return identity


@dataclass
class Environment:
    console: Console
    notifier: Notifier
    dws: DwsClient
    glab: GlabClient
    primary_identity: Identity
    leader_identity: Identity | None
    approval_group: GroupCandidate
    sender_group: GroupCandidate
    self_contact: ContactCandidate
    allowed_senders: dict[str, ContactCandidate]


def check_environment(
    console: Console,
    notifications: bool,
    *,
    profile: str | None,
    approval_group_name: str,
    sender_group_name: str,
    gitlab_host: str,
    allow_setup: bool,
) -> Environment:
    dependencies = ensure_dependencies(console, allow_setup)
    notifier = Notifier(notifications, console)
    dws = DwsClient(dependencies.dws, console, profile)
    ensure_dws_authentication(dws, console=console, allow_setup=allow_setup)
    glab = GlabClient(dependencies.glab, console, gitlab_host)

    dws.validate_consume()
    approval_group = dws.resolve_group(approval_group_name)
    sender_group, self_contact, allowed_senders = dws.resolve_group_senders(
        sender_group_name
    )
    glab.validate_capabilities()
    primary_identity = ensure_glab_authentication(
        glab,
        console=console,
        allow_setup=allow_setup,
    )
    leader_identity: Identity | None = None
    if glab.leader_token:
        if glab.uses_legacy_fallback_token:
            console.warning(
                "检测到旧变量 GITLAB_LEADER_TOKEN，当前继续兼容；"
                "后续建议改用 GITLAB_FALLBACK_TOKEN"
            )
        leader_identity = glab.identity(use_leader=True)
        if primary_identity.user_id == leader_identity.user_id:
            raise AutomationError("当前账号与备用审批账号指向同一个 GitLab 用户")
        console.success(
            "备用审批账号检查通过："
            f"{leader_identity.name}（{leader_identity.username}）"
        )
    else:
        console.info(
            "未配置可选的 GITLAB_FALLBACK_TOKEN；普通 MR 不受影响，"
            "自己创建的 MR 将跳过"
        )

    console.success(
        "启动检查通过："
        f"DWS profile={dws.profile}，GitLab={gitlab_host}，"
        f"当前账号={primary_identity.name}"
    )
    console.success(
        f"审批消息来源：群聊 {approval_group.title}；"
        f"已动态解析会话 ID"
    )
    console.success(
        f"允许发送人来源：群聊 {sender_group.title}，"
        f"成员 {sender_group.member_count or len(allowed_senders) + 1} 人，"
        f"已排除本人 {self_contact.name}；当前纳入 {len(allowed_senders)} 人"
    )
    console.success(
        "当前群成员："
        + "、".join(
            candidate.description for candidate in allowed_senders.values()
        )
    )
    return Environment(
        console=console,
        notifier=notifier,
        dws=dws,
        glab=glab,
        primary_identity=primary_identity,
        leader_identity=leader_identity,
        approval_group=approval_group,
        sender_group=sender_group,
        self_contact=self_contact,
        allowed_senders=allowed_senders,
    )


def print_status(console: Console, profile: str | None) -> int:
    instances = find_running_instances()
    if not instances:
        console.warning("代码审批监听当前没有运行")
    else:
        for pid, elapsed, _command in instances:
            console.ready(f"代码审批监听正在运行：PID={pid}，已运行={elapsed}")

    screen_lock = ScreenLockGuard(console)
    if screen_lock.is_locked_or_unknown():
        console.warning("电脑当前处于锁屏状态，审批任务会暂停到解锁后继续")
    else:
        console.ready("电脑当前已解锁，审批任务可以正常处理")

    dws_path = shutil.which("dws")
    if not dws_path:
        console.warning("找不到 dws，无法查询钉钉连接状态")
        return 0 if instances else 1
    try:
        dws = DwsClient(dws_path, console, profile)
        payload = dws.status()
        live = (
            payload.get("bus", {}).get("live", {})
            if isinstance(payload, dict)
            else {}
        )
        source_state = (
            live.get("source_state", {}).get("state")
            if isinstance(live, dict)
            else None
        )
        consumers = live.get("consumers", []) if isinstance(live, dict) else []
        if source_state == "connected":
            console.ready(f"钉钉事件总线已连接，消费者数量={len(consumers)}")
        elif isinstance(payload, dict):
            bus = payload.get("bus") if isinstance(payload.get("bus"), dict) else {}
            entry = bus.get("entry") if isinstance(bus.get("entry"), dict) else {}
            subscriptions = payload.get("subscriptions")
            subscription_count = (
                len(subscriptions) if isinstance(subscriptions, list) else 0
            )
            if entry.get("state") == "running" and subscription_count > 0:
                console.ready(
                    f"钉钉事件总线正在运行，活跃订阅数量={subscription_count}"
                )
            elif entry.get("state") == "running":
                console.warning("钉钉事件总线正在运行，但当前没有活跃订阅")
            else:
                console.warning("钉钉事件总线当前未运行")
        else:
            console.warning("钉钉事件总线状态格式异常")
    except AutomationError as error:
        console.warning(f"无法查询钉钉连接状态：{error}")
        console.debug(error.detail)
    return 0 if instances else 1


def stop_running(console: Console) -> int:
    instances = find_running_instances()
    if not instances:
        console.warning("代码审批监听当前没有运行")
        return 0
    for pid, _elapsed, _command in instances:
        try:
            os.kill(pid, signal.SIGTERM)
            console.info(f"已向监听进程 PID={pid} 发送停止指令")
        except ProcessLookupError:
            continue
        except PermissionError as error:
            raise AutomationError(f"没有权限停止进程 PID={pid}") from error

    deadline = time.monotonic() + 15
    remaining = {pid for pid, _elapsed, _command in instances}
    while remaining and time.monotonic() < deadline:
        for pid in list(remaining):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                remaining.discard(pid)
        if remaining:
            time.sleep(0.5)
    if remaining:
        console.warning(
            "部分监听进程仍在安全结束当前操作："
            + "、".join(str(pid) for pid in sorted(remaining))
        )
        return 1
    console.success("代码审批监听已停止")
    return 0


class ChineseArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"参数错误：{message}\n")


def nonempty_argument(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise argparse.ArgumentTypeError("参数值不能为空")
    return normalized


def gitlab_hostname_argument(value: str) -> str:
    normalized = value.strip().lower()
    if (
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", normalized)
        or ".." in normalized
    ):
        raise argparse.ArgumentTypeError("GitLab 主机名格式不正确")
    return normalized


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = ChineseArgumentParser(
        description="监听钉钉代码审批消息并自动审批、合并。默认直接前台运行。",
        usage=(
            "%(prog)s [--check | --status | --stop] "
            "[--profile PROFILE] [--approval-group 群名] "
            "[--sender-group 群名] [--gitlab-host 主机名] "
            "[--debug] [--no-notification]"
        ),
        add_help=False,
    )
    parser._positionals.title = "位置参数"
    parser._optionals.title = "可选参数"
    actions = parser.add_mutually_exclusive_group()
    parser.add_argument("-h", "--help", action="help", help="显示帮助后退出")
    actions.add_argument("--check", action="store_true", help="检查依赖和账号后退出")
    actions.add_argument("--status", action="store_true", help="查看监听运行状态")
    actions.add_argument("--stop", action="store_true", help="优雅停止正在运行的监听")
    parser.add_argument("--debug", action="store_true", help="显示底层调试信息")
    parser.add_argument(
        "--profile",
        type=nonempty_argument,
        help="指定 DWS profile；默认使用当前登录账号",
    )
    parser.add_argument(
        "--approval-group",
        type=nonempty_argument,
        default=DEFAULT_APPROVAL_GROUP_NAME,
        help=f"接收审批请求的群名（默认：{DEFAULT_APPROVAL_GROUP_NAME}）",
    )
    parser.add_argument(
        "--sender-group",
        type=nonempty_argument,
        default=DEFAULT_SENDER_GROUP_NAME,
        help=f"允许发送人的来源群名（默认：{DEFAULT_SENDER_GROUP_NAME}）",
    )
    parser.add_argument(
        "--gitlab-host",
        type=gitlab_hostname_argument,
        default=DEFAULT_GITLAB_HOST,
        help=f"公司 GitLab 主机名（默认：{DEFAULT_GITLAB_HOST}）",
    )
    parser.add_argument(
        "--no-notification",
        action="store_true",
        help="关闭 macOS 系统通知",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    console = Console(debug_enabled=args.debug)
    try:
        if args.status:
            return print_status(console, args.profile)
        if args.stop:
            return stop_running(console)

        environment = check_environment(
            console,
            notifications=not args.no_notification,
            profile=args.profile,
            approval_group_name=args.approval_group,
            sender_group_name=args.sender_group,
            gitlab_host=args.gitlab_host,
            allow_setup=not args.check,
        )
        if args.check:
            return 0

        lock_handle = acquire_single_instance_lock()
        _ = lock_handle
        screen_lock = ScreenLockGuard(console)
        stop_event = threading.Event()
        engine = ApprovalEngine(
            glab=environment.glab,
            dws=environment.dws,
            primary_identity=environment.primary_identity,
            leader_identity=environment.leader_identity,
            console=console,
            notifier=environment.notifier,
            screen_lock=screen_lock,
            stop_event=stop_event,
        )
        runtime = ListenerRuntime(
            dws=environment.dws,
            engine=engine,
            console=console,
            notifier=environment.notifier,
            allowed_senders=environment.allowed_senders,
            approval_group=environment.approval_group,
            sender_group=environment.sender_group,
            gitlab_host=environment.glab.hostname,
            screen_lock=screen_lock,
            stop_event=stop_event,
        )

        def signal_handler(signum: int, _frame: Any) -> None:
            runtime.request_stop(signum)

        previous_int = signal.signal(signal.SIGINT, signal_handler)
        previous_term = signal.signal(signal.SIGTERM, signal_handler)
        try:
            return runtime.run()
        finally:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
    except AutomationError as error:
        console.error(str(error))
        console.debug(error.detail)
        return 1
    except KeyboardInterrupt:
        console.info("已停止")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
