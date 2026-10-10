"""Optional sender avatars, resolved independently of approval processing."""
from __future__ import annotations

import base64
from concurrent.futures import Future, ThreadPoolExecutor
import re
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

import listener_core as core


def avatar_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    value = value.strip()
    if value.startswith("https://"):
        try:
            return value if urlsplit(value).hostname else ""
        except ValueError:
            return ""
    encoded = value.removeprefix("@")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", encoded):
        return ""
    try:
        data = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        # DingTalk legacy image IDs encode [type, id, height, width] as
        # MessagePack integers. No external decoder is needed for this format.
        if not data or data[0] != 0x94:
            return ""
        position, values = 1, []
        for _ in range(4):
            marker = data[position]
            position += 1
            if marker <= 0x7f:
                values.append(marker)
                continue
            length = {0xcc: 1, 0xcd: 2, 0xce: 4, 0xcf: 8}.get(marker)
            if length is None or position + length > len(data):
                return ""
            values.append(int.from_bytes(data[position:position + length], "big"))
            position += length
        height, width = values[2:]
        if position != len(data) or not (0 < height <= 65535 and 0 < width <= 65535):
            return ""
    except (ValueError, IndexError):
        return ""
    # The first extension identifies the original object, not the thumbnail.
    # DWS media format 0 is JPEG and 2 is PNG; both were verified against real
    # avatars. Unknown formats must not silently turn into a guessed JPEG URL.
    extension = {0: "jpg", 2: "png"}.get(values[0])
    if extension is None:
        return ""
    return f"https://static-legacy.dingtalk.com/media/{encoded}_{width}_{height}.{extension}_128x128.jpg"


def lookup_sender_avatar(dws: core.DwsClient, contact: core.ContactCandidate) -> str:
    description = f"读取{contact.name}的头像"
    completed = core.run_command(
        dws.command("aisearch", "person", "--query", contact.name, "--dimension", "name", "--format", "json"),
        console=dws.console, description=description, timeout=min(core.COMMAND_TIMEOUT_SECONDS, 10),
    )
    payload = core.parse_json_output(completed, description)
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise core.AutomationError("发送人头像查询未成功")
    rows = payload.get("result")
    if not isinstance(rows, list):
        raise core.AutomationError("发送人头像查询返回格式异常")
    # Names are only search terms. Bind the result to the exact identity that
    # was admitted from the configured group, including when names are shared.
    matches = [row for row in rows if isinstance(row, dict)
               and row.get("openDingTalkId") == contact.open_dingtalk_id]
    urls = {avatar_url(row.get("authorAvatar")) for row in matches}
    return urls.pop() if len(urls) == 1 else ""


class SenderAvatars:
    def __init__(self, lookup: Callable[[core.ContactCandidate], str], publish: Callable[[str, str], None]):
        self.lookup = lookup
        self.publish = publish
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sender-avatar")
        self.lock = threading.RLock()
        self.cache: dict[str, tuple[str, float]] = {}
        self.waiting: dict[str, set[str]] = {}
        self.closed = False

    def cached_url(self, sender_id: str) -> str:
        with self.lock:
            url, expires = self.cache.get(sender_id, ("", 0))
            return url if expires > time.monotonic() else ""

    def request(self, contact: core.ContactCandidate, task_id: str) -> None:
        sender_id = contact.open_dingtalk_id
        with self.lock:
            if self.closed:
                return
            cached = self.cache.get(sender_id)
            if cached and cached[1] > time.monotonic():
                if cached[0]:
                    self.publish(task_id, cached[0])
                return
            if sender_id in self.waiting:
                self.waiting[sender_id].add(task_id)
                return
            self.waiting[sender_id] = {task_id}
            future = self.pool.submit(self.lookup, contact)
            future.add_done_callback(lambda result: self._finish(sender_id, result))

    def _finish(self, sender_id: str, future: Future) -> None:
        try:
            url = future.result()
        except Exception:
            url = ""
        with self.lock:
            task_ids = self.waiting.pop(sender_id, set())
            if self.closed:
                return
            # Successful addresses last for this listening session. Missing or
            # failed lookups can be retried by a later request after five minutes.
            self.cache[sender_id] = (url, float("inf") if url else time.monotonic() + 300)
            for task_id in task_ids:
                if url:
                    self.publish(task_id, url)

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.waiting.clear()
        self.pool.shutdown(wait=False, cancel_futures=True)
