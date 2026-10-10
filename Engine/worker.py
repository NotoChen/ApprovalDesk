#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import pty
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import listener_core as core
from configuration import DEFAULTS, configure_core, executable, validate
from protocol import Events, EventConsole, EventNotifier
from runtime import ApplicationEngine, ApplicationRuntime, PolicyGate
from version import application_version


class AppGlab(core.GlabClient):
    def __init__(self, path, console, settings):
        super().__init__(path, console, settings["gitlab_host"])
        self.primary_token = settings["gitlab_token"].strip()
        self.leader_token = settings["fallback_token"].strip()
        self.uses_legacy_fallback_token = False
        self.commit_counts: dict[str, int] = {}
        self.details_cache = {}

    def environment(self, use_leader):
        env = os.environ.copy()
        for key in ("GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN", "GITLAB_LEADER_TOKEN", "GITLAB_FALLBACK_TOKEN"):
            env.pop(key, None)
        env.update(NO_COLOR="1", GLAB_PAGER="cat")
        token = self.leader_token if use_leader else self.primary_token
        if use_leader and not token:
            raise core.AutomationError("未配置备用审批账号")
        if token:
            env["GITLAB_TOKEN"] = token
        return env

    def commit_count(self, ref, use_leader=False):
        count = super().commit_count(ref, use_leader)
        self.commit_counts[ref.key] = count
        return count

    def details(self, ref, use_leader=False):
        details = super().details(ref, use_leader)
        self.details_cache[ref.key] = details
        return details


class AppDws(core.DwsClient):
    def __init__(self, path, console, settings):
        super().__init__(path, console, settings["profile"] or None)
        self.settings = settings
        self.gate = None
        self.stop_event = None

    def _write(self, arguments, description):
        def operation():
            if self.gate and not self.gate.wait_until_unlocked(self.stop_event, description):
                raise core.ProcessingStopped("监听正在停止")
            return core.run_command(self.command(*arguments), console=self.console, description=description,
                                    retryable_unknown=True)
        core.retry_operation(operation, console=self.console, description=description, stop_event=self.stop_event)

    def mark_read(self, conversation_id, message_id):
        self._write(["chat", "mark-read", "--conversation-id", conversation_id, "--message-id", message_id,
                     "--format", "json", "--yes"], "标记消息已读")

    def add_received(self, conversation_id, message_id):
        self._write(["chat", "message", "add-emoji", "--conversation-id", conversation_id, "--msg-id", message_id,
                     "--emoji", core.RECEIVED_REACTION, "--format", "json"], "添加收到表态")

    def resolve_group(self, name):
        for kind in ("approval", "sender"):
            selected = self.settings.get(f"{kind}_group_id", "")
            if name == self.settings[f"{kind}_group"] and selected:
                matches = [g for g in self.search_groups(name) if g.open_conversation_id == selected]
                if len(matches) == 1:
                    return matches[0]
                raise core.AutomationError(f"当前账号下找不到已选择的 {name} 群，请重新选择")
        return super().resolve_group(name)


class Controller:
    def __init__(self, events=None):
        self.events = events or Events()
        self.settings = dict(DEFAULTS)
        self.console = EventConsole(self.events)
        self.runtime: ApplicationRuntime | None = None
        self.run_thread: threading.Thread | None = None
        self.operation = threading.Lock()
        self.group_operation = threading.Lock()
        self.start_cancelled = threading.Event()
        self.quitting = threading.Event()
        self.auth_process = None
        self.auth_master = None
        self.instance_handle = None

    def current_settings(self):
        return self.settings

    def check(self, key, label, action):
        self.events.send("check", id=key, title=label, status="checking", detail="正在检查")
        try:
            result = action()
            detail = "检查通过"
            if isinstance(result, str):
                detail = result
            elif isinstance(result, core.Identity):
                detail = f"{result.name} · {result.username}"
            elif isinstance(result, dict) and result.get("user_name"):
                detail = str(result["user_name"])
            self.events.send("check", id=key, title=label, status="ready", detail=detail)
            return result
        except Exception as error:
            self.events.send("check", id=key, title=label, status="error", detail=str(error))
            raise

    def accounts(self, request_id: str):
        """Read the two identities independently, without opening a subscription."""
        if self.runtime:
            raise core.AutomationError("请先停止监听，再重新读取账号")
        settings = dict(self.settings)
        configure_core(settings)
        self.console.debug_enabled = settings["debug_output"]

        def read(service):
            self.events.send("account_state", service=service, status="checking", request_id=request_id)
            try:
                component = "dws" if service == "dingtalk" else "glab"
                path = executable(component, settings)
                if not path:
                    raise core.AutomationError(f"未找到 {component} 连接组件，请检查高级设置中的组件路径")
                if service == "dingtalk":
                    dws = AppDws(path, self.console, settings)
                    # Keep the original command error visible instead of converting
                    # network/component failures into an unrelated login prompt.
                    auth = dws.auth_status()
                    if auth.get("authenticated") is not True or auth.get("token_valid") is not True:
                        raise core.AutomationError("钉钉未登录或登录已失效，请在连接与账号中登录后重试")
                    if not auth.get("corp_id") or not auth.get("user_id"):
                        raise core.AutomationError("钉钉登录状态缺少组织或用户信息，请重新登录")
                    name = auth.get("user_name") or str(auth["user_id"])
                    detail = auth.get("corp_name") or str(auth["corp_id"])
                    profile = f"{auth['corp_id']}:{auth['user_id']}"
                else:
                    identity = AppGlab(path, self.console, settings).identity()
                    name, detail, profile = identity.name, identity.username, settings["gitlab_host"]
                self.events.send("identity", service=service, name=name, detail=detail,
                                 profile=profile, request_id=request_id)
            except Exception as error:
                self.events.send("account_state", service=service, status="error",
                                 detail=str(error), request_id=request_id)

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="account") as pool:
            list(pool.map(read, ("dingtalk", "gitlab")))

    def environment(self, settings=None):
        settings = dict(self.settings) if settings is None else settings
        configure_core(settings)
        self.console.debug_enabled = settings["debug_output"]
        dws_path = executable("dws", settings)
        glab_path = executable("glab", settings)
        def require(path, label):
            if not path:
                raise core.AutomationError(f"没有找到 {label}，请在高级设置中选择可执行文件")
            return f"{core.tool_version(path, label)} · {path}"
        self.check("dws", "钉钉连接组件", lambda: require(dws_path, "DWS"))
        self.check("glab", "GitLab 连接组件", lambda: require(glab_path, "glab"))
        dws = AppDws(dws_path, self.console, settings)
        auth = self.check("dws_auth", "钉钉账号", lambda: core.ensure_dws_authentication(dws, console=self.console, allow_setup=False))
        self.events.send("identity", service="dingtalk", name=auth.get("user_name", ""), detail=auth.get("corp_name", ""), profile=dws.profile)
        self.check("subscription", "消息订阅能力", lambda: dws.validate_consume() or "支持 @我 消息监听")
        approval = self.check("approval_group", "审批消息群", lambda: dws.resolve_group(settings["approval_group"]))
        sender_group, self_contact, allowed = self.check("sender_group", "人员来源群", lambda: dws.resolve_group_senders(settings["sender_group"]))
        self.events.send("members", group=sender_group.title, members=[{"id": p.open_dingtalk_id, "name": p.name, "detail": p.nick or p.title} for p in allowed.values()])
        glab = AppGlab(glab_path, self.console, settings)
        self.check("gitlab_api", "GitLab 接口能力", lambda: glab.validate_capabilities() or "审批与合并接口可用")
        identity = self.check("gitlab_auth", "GitLab 账号", lambda: glab.identity())
        self.events.send("identity", service="gitlab", name=identity.name, detail=identity.username, profile=settings["gitlab_host"])
        leader = None
        if settings["fallback_token"]:
            leader = self.check("fallback", "备用审批账号", lambda: glab.identity(True))
            if leader.user_id == identity.user_id:
                raise core.AutomationError("备用账号与当前 GitLab 账号相同，请选择其他账号")
        else:
            self.events.send("check", id="fallback", title="备用审批账号", status="optional", detail="未配置，自己创建的 MR 将跳过")
        return dws, glab, approval, sender_group, self_contact, allowed, identity, leader

    def inspect(self):
        if self.runtime:
            self.events.send("error", message="请先停止监听，再重新检查账号和群配置")
            return
        self.events.send("state", status="checking")
        try:
            self.environment()
            self.events.send("state", status="ready")
        except Exception as error:
            self.events.send("state", status="error", error=str(error))

    def lock_instance(self):
        result = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            try:
                args = shlex.split(parts[1])
            except ValueError:
                continue
            if args and Path(args[0]).name.startswith("python") and any(Path(a).name == "code_approval_listener.py" for a in args[1:]) and not any(a in args for a in ("--check", "--stop", "--status")):
                raise core.AutomationError(f"桌面脚本仍在运行（PID {parts[0]}）。请先在原终端停止，再从审批台开始监听。")
        folder = Path.home() / "Library/Application Support/ApprovalDesk"
        folder.mkdir(parents=True, exist_ok=True)
        self.instance_handle = (folder / "engine.lock").open("a+")
        try:
            fcntl.flock(self.instance_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.instance_handle.close()
            self.instance_handle = None
            raise core.AutomationError("另一个审批台实例已经在监听")

    def start(self, *, settings=None, request_id=None):
        if self.runtime:
            return
        settings = dict(self.settings) if settings is None else settings
        self.start_cancelled.clear()
        self.console.last_error = ""
        self.events.send("state", status="starting")
        try:
            self.lock_instance()
            dws, glab, approval, sender, contact, allowed, identity, leader = self.environment(settings)
            if self.start_cancelled.is_set() or self.quitting.is_set():
                return
            notifier = EventNotifier(self.events, self.current_settings)
            gate = PolicyGate(self.console, self.current_settings)
            stop = threading.Event()
            dws.gate = gate
            dws.stop_event = stop
            engine = ApplicationEngine(glab, dws, identity, leader, self.console, notifier, gate, stop,
                                       settings=dict(settings), events=self.events)
            engine.configured_leader = leader
            runtime = ApplicationRuntime(dws, engine, self.console, notifier, allowed, approval, sender,
                                         settings["gitlab_host"], gate, stop,
                                         events=self.events, settings=self.current_settings)
            self.runtime = runtime
            self.events.send("listening_configuration", request_id=request_id,
                             approval_group={"id": approval.open_conversation_id, "title": approval.title},
                             sender_group={"id": sender.open_conversation_id, "title": sender.title})
            runtime.run()
            if not self.start_cancelled.is_set() and not self.quitting.is_set() and self.console.last_error:
                self.events.send("state", status="error", error=self.console.last_error)
            else:
                self.events.send("state", status="stopped", connected=False, queue_count=0, active_count=0)
        except Exception as error:
            self.events.send("state", status="error", error=str(error), connected=False)
        finally:
            self.runtime = None
            if self.instance_handle:
                self.instance_handle.close()
                self.instance_handle = None
            if self.start_cancelled.is_set():
                self.events.send("state", status="stopped", connected=False, queue_count=0, active_count=0)

    def stop(self):
        self.start_cancelled.set()
        if self.runtime:
            self.runtime.request_stop()
        elif self.run_thread and self.run_thread.is_alive():
            self.events.send("state", status="stopping", connected=False)
        else:
            self.events.send("state", status="stopped", connected=False, queue_count=0, active_count=0)

    def search_groups(self, query: str, kind: str, *, settings=None, request_id=None):
        settings = dict(self.settings) if settings is None else settings
        dws_path = executable("dws", settings)
        if not dws_path:
            raise core.AutomationError("未找到钉钉连接组件")
        dws = AppDws(dws_path, self.console, settings)
        core.ensure_dws_authentication(dws, console=self.console, allow_setup=False)
        groups = dws.search_groups(query)
        self.events.send("groups", kind=kind, profile=dws.profile, request_id=request_id,
                         groups=[{"id": g.open_conversation_id, "title": g.title, "count": g.member_count or 0} for g in groups])

    def profiles(self):
        path = executable("dws", self.settings)
        if not path:
            raise core.AutomationError("未找到钉钉连接组件")
        result = core.run_read_command([path, "profile", "list", "--format", "json"],
                                       console=self.console, description="读取钉钉账号列表")
        payload = core.parse_json_output(result, "读取钉钉账号列表")
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise core.AutomationError("暂时无法读取钉钉账号列表")
        self.events.send("profiles", profiles=[{
            "id": item.get("profile", ""), "name": item.get("userName", ""),
            "organization": item.get("corpName", ""), "current": item.get("isCurrent", False),
        } for item in payload.get("profiles", [])])

    def login(self, service):
        if self.runtime:
            raise core.AutomationError("请先停止监听，再登录或更换账号")
        path = executable("dws" if service == "dingtalk" else "glab", self.settings)
        if not path:
            raise core.AutomationError("未找到登录组件，请检查高级设置")
        if service == "dingtalk":
            command = [path, "auth", "login", "--recommend"]
        else:
            host = self.settings["gitlab_host"]
            command = [path, "auth", "login", "--hostname", host, "--web", "--api-host", host,
                       "--ssh-hostname", host, "--git-protocol", "https", "--api-protocol", "https",
                       "--container-registry-domains", host, "--insecure-storage"]
        master, slave = pty.openpty()
        env = os.environ.copy()
        env.update(TERM="dumb", NO_COLOR="1")
        self.auth_master = master
        self.events.send("auth", status="running", service=service)
        try:
            self.auth_process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
            os.close(slave)
            while True:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                self.events.send("auth_output", text=core.ANSI_ESCAPE_RE.sub("", chunk.decode("utf-8", "replace")))
            code = self.auth_process.wait()
            self.events.send("auth", status="completed" if code == 0 else "failed", service=service,
                             message="授权完成，正在刷新当前账号" if code == 0 else "授权未完成，可查看详情；GitLab 也可以直接填写 Token")
        finally:
            self.auth_process = None
            self.auth_master = None
            os.close(master)

    def dispatch(self, message):
        command = message.get("command")
        if "settings" in message:
            self.settings = validate(message["settings"])
        if command in ("configure", "update_settings"):
            if self.runtime:
                with self.runtime.tasks.mutex:
                    self.runtime.tasks.maxsize = self.settings["queue_capacity"]
                self.console.debug_enabled = self.settings["debug_output"]
                self.runtime.emit_state()
            self.events.send("configured")
        elif command == "stop":
            self.stop()
        elif command in ("pause", "resume"):
            if self.runtime:
                self.runtime.set_paused(command == "pause")
        elif command == "clear_queue":
            if self.runtime:
                self.runtime.clear_queue()
        elif command == "auth_input":
            if self.auth_master is not None:
                os.write(self.auth_master, str(message.get("text", "\n")).encode())
        elif command == "cancel_auth":
            if self.auth_process:
                self.auth_process.terminate()
        elif command in ("accounts", "check", "start", "groups", "login", "profiles"):
            # Listening owns the main operation until it stops. Group lookup
            # is an independent read and must not wait for that session to end.
            operation = self.group_operation if command == "groups" else self.operation
            requested_settings = dict(self.settings) if command in ("groups", "start") else None
            if not operation.acquire(blocking=False):
                self.events.send("error", command=command, request_id=message.get("request_id"),
                                 operation_rejected=True, message="当前操作尚未结束，请稍候")
                return
            def work():
                try:
                    if command == "accounts": self.accounts(str(message.get("request_id", "")))
                    elif command == "check": self.inspect()
                    elif command == "start": self.start(settings=requested_settings, request_id=message.get("request_id"))
                    elif command == "groups": self.search_groups(str(message.get("query", "")), str(message.get("kind", "approval")),
                                                                 settings=requested_settings, request_id=message.get("request_id"))
                    elif command == "login": self.login(str(message.get("service", "dingtalk")))
                    elif command == "profiles": self.profiles()
                except Exception as error:
                    self.events.send("error", command=command, request_id=message.get("request_id"), message=str(error))
                finally:
                    operation.release()
                    # The UI may immediately request profiles or auto-start next.
                    self.events.send("operation_finished", command=command, request_id=message.get("request_id"))
            thread = threading.Thread(target=work, name=f"app-{command}", daemon=False)
            if command == "start": self.run_thread = thread
            thread.start()
        elif command == "shutdown":
            self.shutdown()

    def shutdown(self):
        self.quitting.set()
        self.stop()
        if self.auth_process:
            self.auth_process.terminate()


def main():
    controller = Controller()
    controller.events.send("hello", version=application_version(), pid=os.getpid())
    def terminate(_signum, _frame):
        controller.shutdown()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        for line in sys.stdin:
            message = {}
            try:
                message = json.loads(line)
                controller.dispatch(message)
                if message.get("command") == "shutdown":
                    break
            except Exception as error:
                controller.events.send("error", message=str(error), operation_rejected=True,
                                       command=message.get("command") if isinstance(message, dict) else None,
                                       request_id=message.get("request_id") if isinstance(message, dict) else None)
    finally:
        controller.shutdown()


if __name__ == "__main__":
    main()
