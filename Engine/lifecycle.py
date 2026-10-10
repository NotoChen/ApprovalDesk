"""DWS startup decisions; a live process is not proof of a ready subscription."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
import time

import listener_core as core


@dataclass(frozen=True)
class RetryDecision:
    reason: str
    additional_attempts: int
    wait_seconds: float = 0
    terminal: bool = False


def startup_decision(lines: list[str], returncode: int, now: float | None = None) -> RetryDecision:
    text = "\n".join(lines)
    fields = {}

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"retryable", "retry_after_seconds", "next_retry_at", "reason", "category"}:
                    fields[key] = item
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    try:
        collect(json.loads(text))
    except (ValueError, TypeError):
        for line in lines:
            try:
                collect(json.loads(line[line.index("{"):]))
            except (ValueError, TypeError):
                pass
    for key in ("retryable", "retry_after_seconds", "next_retry_at"):
        matches = re.findall(rf"\b{key}=([^\s,]+)", text)
        if matches and key not in fields:
            fields[key] = matches[-1].strip('"')

    retryable = fields.get("retryable")
    if isinstance(retryable, str):
        retryable = {"true": True, "false": False}.get(retryable.lower())
    attempts = 2 if retryable is True else 0 if retryable is False else 1
    lowered = text.lower()
    reason, stale_bus, _ = core.dws_listener_error_reason(text, returncode)
    if fields.get("reason"):
        reason += f"（{fields['reason']}）"
    terminal = retryable is False or stale_bus or any(marker in lowered for marker in (
        "terminal_hold", "in_flight", "auth_refresh_failed", "unauthorized", "authentication failed",
    )) or fields.get("category") in {"auth", "validation"}
    delay = 0.0
    try:
        delay = max(0.0, float(fields.get("retry_after_seconds", 0)))
    except (TypeError, ValueError):
        pass
    next_retry = fields.get("next_retry_at")
    if next_retry:
        try:
            timestamp = float(next_retry)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
        except (TypeError, ValueError):
            try:
                timestamp = datetime.fromisoformat(str(next_retry).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                timestamp = 0
        delay = max(delay, timestamp - (time.time() if now is None else now))
    # A cooldown without an announced retry time cannot be worked around.
    if "cooldown" in lowered and delay <= 0:
        terminal = True
    return RetryDecision(reason, 0 if terminal else attempts, max(0.0, delay), terminal)
