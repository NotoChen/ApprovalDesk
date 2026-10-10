import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Engine"))
import listener_core as core
from configuration import DEFAULTS, configure_core, validate
from protocol import Events, EventConsole, EventNotifier
from runtime import ApplicationEngine, ApplicationRuntime, PolicyGate


class DwsFake:
    def __init__(self):
        self.reads = []
        self.receipts = []
        self.lookups = []

    def message_by_id(self, message_id):
        self.lookups.append(message_id)
        return {}

    def mark_read(self, conversation, message): self.reads.append(message)
    def add_received(self, conversation, message): self.receipts.append(message)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.settings = dict(DEFAULTS)
        self.output = []
        self.events = Events(self.output.append)
        self.console = EventConsole(self.events)
        self.gate = PolicyGate(self.console, lambda: self.settings)
        self.gate.refresh = lambda: self.gate.locked
        self.stop = threading.Event()
        self.dws = DwsFake()
        self.engine = Mock(active_mrs=0, configured_leader=None)
        self.engine.process = Mock()
        self.notifier = EventNotifier(self.events, lambda: self.settings)
        avatar_patch = patch("runtime.lookup_sender_avatar", return_value="")
        self.avatar_lookup = avatar_patch.start()
        self.addCleanup(avatar_patch.stop)

    def runtime(self):
        group = core.GroupCandidate("审批", "approval-group", 2)
        sender = core.GroupCandidate("成员", "sender-group", 2)
        contact = core.ContactCandidate("测试成员", "user", "sender", "", "", "")
        runtime = ApplicationRuntime(self.dws, self.engine, self.console, self.notifier,
                                     {"sender": contact}, group, sender, "git2.superboss.cc", self.gate, self.stop,
                                     events=self.events, settings=lambda: self.settings)
        self.addCleanup(runtime.avatars.close)
        return runtime

    def event(self, number=1):
        return {"type": core.EVENT_KEY, "event_id": f"event-{number}", "message_id": f"message-{number}",
                "conversation_id": "approval-group", "sender_open_dingtalk_id": "sender",
                "content": f"请审批 https://git2.superboss.cc/erp/erp-core/-/merge_requests/{number}"}

    def test_lock_and_pause_are_independent(self):
        self.gate.locked = True
        self.settings["locked_message_policy"] = "discard"
        self.assertEqual(self.gate.disposition(), "discard")
        self.settings["process_while_locked"] = True
        self.assertEqual(self.gate.reason(), "")
        self.gate.paused.set()
        self.settings["paused_message_policy"] = "queue"
        self.assertEqual(self.gate.reason(), "paused")
        self.assertEqual(self.gate.disposition(), "queue")

    def test_default_pause_and_lock_discard_without_message_access(self):
        for reason in ("paused", "locked"):
            with self.subTest(reason=reason):
                self.gate.locked = reason == "locked"
                self.gate.paused.set() if reason == "paused" else self.gate.paused.clear()
                runtime = self.runtime()
                runtime._handle_event(self.event())
                self.assertEqual(runtime.tasks.qsize(), 0)
                self.assertEqual(self.dws.lookups + self.dws.reads + self.dws.receipts, [])
                self.engine.process.assert_not_called()
                self.avatar_lookup.assert_not_called()
                self.assertEqual(self.output[-1]["task"]["status"], "discarded")

    def test_avatar_loading_does_not_delay_queue_and_only_updates_its_request(self):
        entered, release, published = threading.Event(), threading.Event(), threading.Event()
        def lookup(*_):
            entered.set()
            release.wait(3)
            return "https://example.test/avatar.jpg"
        self.avatar_lookup.side_effect = lookup
        def receive(event):
            self.output.append(event)
            if event.get("sender_avatar_url"): published.set()
        self.events.sink = receive
        runtime = self.runtime()
        try:
            runtime._handle_event(self.event())
            self.assertTrue(entered.wait(2))
            self.assertFalse(published.is_set())
            self.assertEqual(runtime.tasks.qsize(), 1)
            release.set()
            self.assertTrue(published.wait(2))
            update = next(e for e in self.output if e.get("sender_avatar_url"))
            self.assertEqual(update["type"], "task_update")
            self.assertEqual(update["id"], "event-1")
            self.assertEqual(update["sender_avatar_url"], "https://example.test/avatar.jpg")
            self.assertNotIn("status", update)
        finally:
            release.set()

    def test_paused_queue_never_executes_before_resume(self):
        self.settings["paused_message_policy"] = "queue"
        self.gate.paused.set()
        runtime = self.runtime()
        thread = threading.Thread(target=runtime._worker)
        thread.start()
        try:
            runtime._handle_event(self.event())
            time.sleep(0.08)
            self.engine.process.assert_not_called()
            self.assertEqual(self.dws.reads, [])
            self.gate.paused.clear()
            deadline = time.monotonic() + 2
            while not self.engine.process.called and time.monotonic() < deadline: time.sleep(0.01)
            self.engine.process.assert_called_once()
            self.assertEqual(self.dws.reads, ["message-1"])
        finally:
            self.stop.set(); runtime.tasks.put(None); thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_reject_new_preserves_existing_queue(self):
        self.settings.update(queue_capacity=1, queue_overflow_policy="reject_new")
        runtime = self.runtime()
        runtime._handle_event(self.event(1)); runtime._handle_event(self.event(2))
        self.assertEqual(runtime.tasks.get_nowait().message_id, "message-1")
        self.assertNotIn("erp/erp-core!2", runtime._reserved_mrs)
        self.assertTrue(any(e.get("id") == "event-2" and e.get("status") == "discarded" for e in self.output))

    def test_default_overflow_drops_oldest_and_releases_its_reservation(self):
        self.settings["queue_capacity"] = 1
        runtime = self.runtime()
        runtime._handle_event(self.event(1)); runtime._handle_event(self.event(2))
        self.assertEqual(runtime.tasks.get_nowait().message_id, "message-2")
        self.assertNotIn("erp/erp-core!1", runtime._reserved_mrs)
        self.assertIn("erp/erp-core!2", runtime._reserved_mrs)

    def test_default_overflow_keeps_running_task_and_removes_oldest_waiting_task(self):
        self.settings["queue_capacity"] = 2
        runtime = self.runtime()
        started, release = threading.Event(), threading.Event()
        def process(task):
            started.set()
            release.wait(3)
        self.engine.process.side_effect = process
        thread = threading.Thread(target=runtime._worker)
        thread.start()
        try:
            runtime._handle_event(self.event(1))
            self.assertTrue(started.wait(2))
            for number in (2, 3, 4): runtime._handle_event(self.event(number))
            with runtime.tasks.mutex:
                self.assertEqual([task.message_id for task in runtime.tasks.queue], ["message-3", "message-4"])
            self.assertIn("erp/erp-core!1", runtime._reserved_mrs)
            self.assertNotIn("erp/erp-core!2", runtime._reserved_mrs)
            discarded = [e.get("id") for e in self.output if e.get("status") == "discarded"]
            self.assertEqual(discarded, ["event-2"])
        finally:
            self.stop.set(); release.set(); runtime.tasks.put(None, timeout=2); thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_clear_queue_does_not_mark_messages_read(self):
        runtime = self.runtime()
        runtime._handle_event(self.event(1)); runtime._handle_event(self.event(2))
        self.assertEqual(runtime.clear_queue(), 2)
        self.assertEqual(runtime._reserved_mrs, set())
        self.assertEqual(self.dws.reads + self.dws.receipts, [])

    def test_expired_queue_is_skipped_after_resume(self):
        self.settings["max_queue_age_seconds"] = 1
        runtime = self.runtime()
        runtime._handle_event(self.event())
        runtime.enqueued["event-1"] = time.monotonic() - 3
        runtime.tasks.put(None)
        runtime._worker()
        self.engine.process.assert_not_called()
        self.assertEqual(self.dws.reads, [])

    def test_changing_disposition_does_not_delete_existing_queue(self):
        runtime = self.runtime()
        runtime._handle_event(self.event(1))
        self.gate.paused.set(); self.settings["paused_message_policy"] = "discard"
        runtime._handle_event(self.event(2))
        self.assertEqual(runtime.tasks.qsize(), 1)
        self.assertEqual(runtime.tasks.get_nowait().message_id, "message-1")

    def test_unmatched_sender_cannot_enter_queue(self):
        runtime = self.runtime()
        event = self.event(); event["sender_open_dingtalk_id"] = "outside"
        runtime._handle_event(event)
        self.assertEqual(runtime.tasks.qsize(), 0)
        self.assertEqual(self.output, [])

    def test_duplicate_event_is_not_enqueued_twice(self):
        runtime = self.runtime()
        runtime._handle_event(self.event()); runtime._handle_event(self.event())
        self.assertEqual(runtime.tasks.qsize(), 1)


class ReceiptTests(unittest.TestCase):
    def run_case(self, condition, results, enabled=True):
        output = []
        events = Events(output.append)
        console = EventConsole(events)
        settings = dict(DEFAULTS, send_received=enabled)
        if condition is not None: settings["received_condition"] = condition
        dws = DwsFake()
        stop = threading.Event()
        gate = PolicyGate(console, lambda: settings)
        gate.refresh = lambda: False
        engine = ApplicationEngine(Mock(), dws, core.Identity(1, "本人", "me"), None,
                                   console, EventNotifier(events, lambda: settings), gate, stop,
                                   settings=settings, events=events)
        engine._process_one = lambda ref, index, total: results[index - 1]
        refs = tuple(core.parse_mr_url(f"https://git2.superboss.cc/erp/erp-core/-/merge_requests/{n + 1}") for n in range(len(results)))
        engine.process(core.MessageTask("event", "message", "group", "成员", refs))
        return dws, output

    def test_one_owned_merge_receives_even_when_another_failed(self):
        dws, events = self.run_case("any_owned_merge", [(1, 1, None, True, set()), (0, 0, "冲突", None, set())])
        self.assertEqual(dws.receipts, ["message"])
        self.assertEqual([e for e in events if e["type"] == "task_update"][-1]["status"], "partial")

    def test_strict_condition_requires_all_success(self):
        dws, _ = self.run_case("all_succeeded_and_owned", [(1, 1, None, True, set()), (0, 0, "冲突", None, set())])
        self.assertEqual(dws.receipts, [])

    def test_default_receipt_requires_all_success_and_an_owned_merge(self):
        cases = [
            ([(1, 1, None, True, set()), (0, 0, "冲突", None, set())], []),
            ([(1, 1, None, True, set()), (0, 0, None, False, {"由其他人合并"})], ["message"]),
            ([(0, 0, None, False, {"由其他人合并"})], []),
        ]
        for results, expected in cases:
            with self.subTest(results=results):
                dws, _ = self.run_case(None, results)
                self.assertEqual(dws.receipts, expected)

    def test_other_people_merges_do_not_receive(self):
        dws, _ = self.run_case("any_owned_merge", [(0, 0, None, False, {"由其他人合并"})])
        self.assertEqual(dws.receipts, [])

    def test_disabled_receipt_never_writes(self):
        dws, _ = self.run_case("any_owned_merge", [(1, 1, None, True, set())], enabled=False)
        self.assertEqual(dws.receipts, [])


class ConfigurationTests(unittest.TestCase):
    def test_invalid_capacity_and_policy_are_rejected(self):
        for values in ({"queue_capacity": 0}, {"locked_message_policy": "unknown"}, {"retry_delays": []}, {"max_attempts": 1.5}):
            with self.assertRaises(ValueError): validate(values)

    def test_attempts_change_reaches_core_calls(self):
        settings = validate({"max_attempts": 1})
        configure_core(settings)
        calls = []
        def operation():
            calls.append(1); raise core.AutomationError("失败", retryable=True)
        try:
            with self.assertRaises(core.AutomationError): core.retry_operation(operation, console=EventConsole(Events(lambda e: None)), description="测试")
            self.assertEqual(len(calls), 1)
        finally: configure_core(DEFAULTS)

    def test_worker_protocol_exits_cleanly_without_external_calls(self):
        worker = Path(__file__).resolve().parents[2] / "Engine/worker.py"
        commands = "\n".join(json.dumps(x) for x in [{"command": "configure", "settings": DEFAULTS}, {"command": "shutdown"}]) + "\n"
        result = subprocess.run([sys.executable, "-B", "-u", str(worker)], input=commands, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(events[0]["type"], "hello")
        self.assertIn("configured", [e["type"] for e in events])


if __name__ == "__main__": unittest.main()
