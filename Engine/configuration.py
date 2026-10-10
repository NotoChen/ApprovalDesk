from __future__ import annotations

import os
import hashlib
import shutil
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "schema_version": 1,
    "profile": "",
    "approval_group": "代码审批",
    "approval_group_id": "",
    "sender_group": "向钱冲",
    "sender_group_id": "",
    "gitlab_host": "git2.superboss.cc",
    "gitlab_token": "",
    "fallback_token": "",
    "dws_path": "",
    "glab_path": "",
    "process_while_locked": False,
    "locked_message_policy": "discard",
    "paused_message_policy": "discard",
    "queue_capacity": 100,
    "queue_overflow_policy": "drop_oldest",
    "max_queue_age_seconds": 0,
    "max_concurrent_mrs": 3,
    "max_mrs_per_message": 30,
    "parse_quoted_messages": True,
    "mark_read": True,
    "send_received": True,
    "received_condition": "all_succeeded_and_owned",
    "include_fallback_for_received": True,
    "use_fallback_for_own_mr": True,
    "command_timeout_seconds": 90,
    "merge_timeout_seconds": 60,
    "merge_poll_seconds": 2,
    "max_attempts": 3,
    "retry_delays": [2, 5],
    "reconnect_delays": [2, 5, 10, 20, 30],
    "notifications_enabled": True,
    "notify_on_request": True,
    "notify_on_success": True,
    "notify_on_failure": True,
    "launch_at_login": True,
    "start_on_launch": True,
    "show_workbench_on_launch": False,
    "appearance": "system",
    "activity_limit": 300,
    "debug_output": False,
}


def validate(raw: dict[str, Any]) -> dict[str, Any]:
    result = DEFAULTS | {k: v for k, v in raw.items() if k in DEFAULTS}
    for key, value in DEFAULTS.items():
        if isinstance(value, bool) and not isinstance(result[key], bool):
            raise ValueError(f"{key} 必须为开关值")
        if isinstance(value, str) and not isinstance(result[key], str):
            raise ValueError(f"{key} 必须为文本")
    ranges = {
        "queue_capacity": (1, 10000), "max_queue_age_seconds": (0, 604800),
        "max_concurrent_mrs": (1, 16), "max_mrs_per_message": (1, 200),
        "command_timeout_seconds": (5, 600), "merge_timeout_seconds": (5, 3600),
        "merge_poll_seconds": (1, 60), "max_attempts": (1, 10),
        "activity_limit": (50, 3000),
    }
    for key, (low, high) in ranges.items():
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ValueError(f"{key} 必须在 {low} 至 {high} 之间")
        if int(value) != value:
            raise ValueError(f"{key} 必须为整数")
        result[key] = int(value)
    for key, choices in {
        "locked_message_policy": {"queue", "discard"},
        "paused_message_policy": {"queue", "discard"},
        "queue_overflow_policy": {"reject_new", "drop_oldest"},
        "received_condition": {"any_owned_merge", "all_succeeded_and_owned"},
        "appearance": {"system", "light", "dark"},
    }.items():
        if result[key] not in choices:
            raise ValueError(f"{key} 的选项不受支持")
    for key in ("retry_delays", "reconnect_delays"):
        values = result[key]
        if not isinstance(values, list) or not values or len(values) > 10 or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not 1 <= v <= 300 for v in values
        ):
            raise ValueError(f"{key} 需要 1 至 10 个 1–300 秒的等待时间")
    for key in ("approval_group", "sender_group", "gitlab_host"):
        if not result[key].strip():
            raise ValueError("审批群、人员来源群和 GitLab 地址不能为空")
        result[key] = result[key].strip()
    from listener_core import gitlab_hostname_argument
    result["gitlab_host"] = gitlab_hostname_argument(result["gitlab_host"])
    return result


def configure_core(settings: dict[str, Any]) -> None:
    import listener_core as core
    for attr, key in {
        "MAX_URLS": "max_mrs_per_message", "MAX_CONCURRENT_MRS": "max_concurrent_mrs",
        "QUEUE_SIZE": "queue_capacity", "COMMAND_TIMEOUT_SECONDS": "command_timeout_seconds",
        "MERGE_VERIFY_TIMEOUT_SECONDS": "merge_timeout_seconds",
        "MERGE_VERIFY_INTERVAL_SECONDS": "merge_poll_seconds", "RETRY_ATTEMPTS": "max_attempts",
        "RETRY_DELAYS_SECONDS": "retry_delays", "RECONNECT_DELAYS": "reconnect_delays",
    }.items():
        setattr(core, attr, settings[key])


def executable(name: str, settings: dict[str, Any]) -> str | None:
    custom = settings.get(f"{name}_path", "").strip()
    if custom:
        path = Path(custom).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        return None
    bundled = Path(__file__).resolve().parent.parent / "bin" / name
    candidates = [bundled, Path.home() / ".local/bin" / name, Path("/opt/homebrew/bin") / name, Path("/usr/local/bin") / name]
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            if name == "dws" and path == bundled:
                return str(stage_dws(path))
            return str(path)
    return shutil.which(name)


def stage_dws(source: Path) -> Path:
    """DWS expands native runtime sidecars next to itself; keep the .app immutable."""
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    folder = Path(os.environ.get("APPROVALDESK_DATA_DIR", str(Path.home() / "Library/Application Support/ApprovalDesk")))
    folder = folder / "Components" / f"dws-{digest}"
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / "dws"
    if not destination.exists():
        import tempfile
        descriptor, name = tempfile.mkstemp(prefix=".dws-", dir=folder)
        os.close(descriptor)
        temporary = Path(name)
        try:
            shutil.copy2(source, temporary)
            temporary.chmod(0o755)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return destination
