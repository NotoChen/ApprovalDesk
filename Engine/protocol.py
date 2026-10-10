from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any, Callable

import listener_core as core


class Events:
    def __init__(self, sink: Callable[[dict[str, Any]], None] | None = None):
        self.lock = threading.Lock()
        self.sink = sink

    def send(self, event_type: str, /, **values: Any) -> None:
        payload = {"type": event_type, "timestamp": time.time(), **values}
        with self.lock:
            if self.sink:
                self.sink(payload)
            else:
                try:
                    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
                    sys.stdout.flush()
                except BrokenPipeError:
                    pass


class EventConsole(core.Console):
    def __init__(self, events: Events, debug: bool = False):
        super().__init__(debug)
        self.events = events
        self.local = threading.local()
        self.last_error = ""
        self.on_ready: Callable[[], None] | None = None

    def _write(self, icon: str, message: str) -> None:
        levels = {"❌": "error", "⚠️": "warning", "✅": "success", "🔧": "debug"}
        level = levels.get(icon, "info")
        if level == "error":
            self.last_error = message
        self.events.send("log", level=level, message=message,
                         task_id=getattr(self.local, "task_id", ""),
                         mr_key=getattr(self.local, "mr_key", ""))
        if icon == "🟢" and self.on_ready:
            self.on_ready()


class EventNotifier:
    def __init__(self, events: Events, settings: Callable[[], dict[str, Any]]):
        self.events = events
        self.settings = settings

    def send(self, title: str, message: str, subtitle: str = "", task_id: str = "") -> bool:
        settings = self.settings()
        category = "failure" if any(v in title for v in ("失败", "异常", "无效", "停止", "部分")) else "request" if "收到代码审批请求" in title else "success"
        if settings["notifications_enabled"] and settings[f"notify_on_{category}"]:
            self.events.send("notification", title=title, message=message, subtitle=subtitle, task_id=task_id)
            return True
        return False
