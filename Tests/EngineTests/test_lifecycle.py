import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Engine"))
import listener_core as core
from configuration import DEFAULTS
from lifecycle import startup_decision
from protocol import Events, EventConsole, EventNotifier
from runtime import ApplicationRuntime, PolicyGate


class LifecycleTests(unittest.TestCase):
    def test_retryable_0_2_1_budget(self):
        for retryable, expected in [(False, 0), (True, 2), (None, 1)]:
            decision = startup_decision([json.dumps({"error": {"retryable": retryable}})], 1)
            self.assertEqual(decision.additional_attempts, expected)

    def test_retry_after_and_timestamp_both_are_respected(self):
        decision = startup_decision(['{"error":{"retryable":true,"retry_after_seconds":12,"next_retry_at":"2026-01-01T00:01:00Z"}}'], 1, now=1767225600)
        self.assertEqual(decision.wait_seconds, 60)
        self.assertEqual(startup_decision(["retryable=true retry_after_seconds=15"], 1).wait_seconds, 15)

    def test_protection_is_not_bypassed(self):
        for reason in ["terminal_hold", "in_flight", "cooldown"]:
            self.assertTrue(startup_decision([json.dumps({"reason": reason, "retryable": True})], 1).terminal)
        decision = startup_decision(['{"reason":"cooldown","retryable":true,"retry_after_seconds":20}'], 1)
        self.assertFalse(decision.terminal)
        self.assertEqual(decision.wait_seconds, 20)

    def runtime(self, code):
        settings = dict(DEFAULTS, reconnect_delays=[0], parse_quoted_messages=False)
        output = []
        events = Events(output.append)
        console = EventConsole(events)
        gate = PolicyGate(console, lambda: settings)
        gate.refresh = lambda: False
        dws = Mock()
        dws.consume_command.return_value = [sys.executable, "-B", "-u", "-c", code]
        engine = Mock(active_mrs=0, configured_leader=None)
        group = core.GroupCandidate("审批", "group", 2)
        contact = core.ContactCandidate("成员", "user", "sender", "", "", "")
        runtime = ApplicationRuntime(dws, engine, console, EventNotifier(events, lambda: settings),
                                     {"sender": contact}, group, group, "git2.superboss.cc", gate, threading.Event(),
                                     settings=lambda: settings, events=events)
        return runtime, engine, dws, output

    def test_ready_is_required_before_processing_stdout(self):
        event = {"type": core.EVENT_KEY, "event_id": "event", "message_id": "message", "conversation_id": "group",
                 "sender_open_dingtalk_id": "sender", "content": "https://git2.superboss.cc/erp/core/-/merge_requests/1"}
        code = f"import sys,time\nprint({json.dumps(event)!r}, flush=True)\ntime.sleep(.08)\nprint('[event] ready event_key=test bus_pid=1',file=sys.stderr,flush=True)\ntime.sleep(5)"
        runtime, engine, _, output = self.runtime(code)
        engine.process.side_effect = lambda _: runtime.request_stop()
        runtime.run()
        engine.process.assert_called_once()
        ready_index = next(i for i, e in enumerate(output) if e.get("message") == "钉钉真实订阅已就绪")
        task_index = next(i for i, e in enumerate(output) if e["type"] == "task")
        self.assertLess(ready_index, task_index)

    def test_failed_start_is_bounded_and_never_processes_events(self):
        for retryable, count in [(False, 1), (True, 3), (None, 2)]:
            with self.subTest(retryable=retryable):
                code = f"import sys\nprint({json.dumps({'error': {'retryable': retryable}})!r}, file=sys.stderr)\nsys.exit(1)"
                runtime, engine, dws, _ = self.runtime(code)
                with self.assertRaises(core.AutomationError): runtime.run()
                self.assertEqual(dws.consume_command.call_count, count)
                engine.process.assert_not_called()

    def test_live_mr_limit_does_not_depend_on_previous_task_globals(self):
        saved = core.MAX_URLS
        core.MAX_URLS = 1
        try:
            content = " ".join(f"https://git2.superboss.cc/erp/core/-/merge_requests/{i}" for i in (1, 2))
            refs, _ = core.extract_mr_refs_from_message(content, max_urls=2)
            self.assertEqual(len(refs), 2)
        finally:
            core.MAX_URLS = saved


if __name__ == "__main__": unittest.main()
