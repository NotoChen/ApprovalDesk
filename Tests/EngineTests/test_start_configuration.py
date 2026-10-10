from contextlib import ExitStack
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Engine"))
import listener_core as core
from protocol import Events
from worker import Controller


class ListeningConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.output = []
        self.controller = Controller(Events(self.output.append))
        self.controller.lock_instance = Mock()
        self.stack.enter_context(patch("worker.executable", return_value="/fixture/tool"))
        self.stack.enter_context(patch("worker.core.tool_version", return_value="fixture"))
        self.stack.enter_context(patch("worker.core.ensure_dws_authentication", return_value={"user_name": "测试账号"}))
        self.dws_factory = self.stack.enter_context(patch("worker.AppDws"))
        self.dws = self.dws_factory.return_value
        self.dws.profile = "corp:user"
        self.dws.validate_consume.return_value = None
        self.approval = core.GroupCandidate("代码审批", "current-approval", 9)
        self.sender = core.GroupCandidate("向钱冲", "current-sender", 5)
        self.dws.resolve_group.return_value = self.approval
        self.dws.resolve_group_senders.return_value = (self.sender, Mock(), {})
        self.glab_factory = self.stack.enter_context(patch("worker.AppGlab"))
        self.glab = self.glab_factory.return_value
        self.glab.identity.return_value = Mock(name="账号", username="tester")
        self.engine_factory = self.stack.enter_context(patch("worker.ApplicationEngine"))
        self.runtime_factory = self.stack.enter_context(patch("worker.ApplicationRuntime"))
        self.runtime = self.runtime_factory.return_value
        self.runtime.run.side_effect = lambda: self.output.append({"type": "runtime_run"})

    def finish(self):
        self.controller.run_thread.join(3)
        self.assertFalse(self.controller.run_thread.is_alive(), "Listener fixture did not finish")
        self.assertEqual(self.output[-1]["type"], "operation_finished")
        self.assertEqual(self.output[-1]["command"], "start")

    def test_start_publishes_actual_group_identities_before_running(self):
        self.assertEqual(self.controller.settings["approval_group_id"], "")
        self.assertEqual(self.controller.settings["sender_group_id"], "")
        self.controller.dispatch({"command": "start", "request_id": "session-1"})
        self.finish()
        event = next(event for event in self.output if event["type"] == "listening_configuration")
        self.assertEqual(event["request_id"], "session-1")
        self.assertEqual(event["approval_group"], {"id": "current-approval", "title": "代码审批"})
        self.assertEqual(event["sender_group"], {"id": "current-sender", "title": "向钱冲"})
        self.assertLess(self.output.index(event), next(i for i, item in enumerate(self.output) if item["type"] == "runtime_run"))
        self.assertIs(self.runtime_factory.call_args.args[5], self.approval)
        self.assertIs(self.runtime_factory.call_args.args[6], self.sender)
        self.assertEqual(self.controller.settings["approval_group_id"], "")
        self.assertFalse(self.controller.operation.locked())

    def test_start_uses_command_snapshot_when_settings_change_during_resolution(self):
        entered, release = threading.Event(), threading.Event()
        def wait_for_edit():
            entered.set()
            self.assertTrue(release.wait(3))
        self.controller.lock_instance.side_effect = wait_for_edit
        original = dict(self.controller.settings)
        self.controller.dispatch({"command": "start", "settings": original, "request_id": "original-session"})
        try:
            self.assertTrue(entered.wait(2))
            changed = dict(original, approval_group_id="other-approval", sender_group_id="other-sender",
                           profile="corp:other", gitlab_host="other.example", queue_capacity=250)
            self.controller.dispatch({"command": "update_settings", "settings": changed})
        finally:
            release.set()
            self.finish()
        self.assertEqual(self.dws_factory.call_args.args[2], original)
        self.assertEqual(self.glab_factory.call_args.args[2], original)
        self.assertEqual(self.engine_factory.call_args.kwargs["settings"], original)
        self.assertEqual(self.runtime_factory.call_args.args[7], original["gitlab_host"])
        # Runtime policies still read the latest settings independently.
        self.assertEqual(self.runtime_factory.call_args.kwargs["settings"]()["queue_capacity"], 250)
        event = next(event for event in self.output if event["type"] == "listening_configuration")
        self.assertEqual((event["request_id"], event["approval_group"]["id"]), ("original-session", "current-approval"))
        self.assertEqual(self.controller.settings["approval_group_id"], "other-approval")

    def test_cancelled_start_does_not_publish_a_running_configuration(self):
        self.controller.lock_instance.side_effect = self.controller.stop
        self.controller.dispatch({"command": "start", "request_id": "cancelled"})
        self.finish()
        self.assertFalse(any(event["type"] == "listening_configuration" for event in self.output))
        self.runtime_factory.assert_not_called()
        self.assertTrue(any(event.get("status") == "stopped" for event in self.output))

    def test_failed_start_does_not_publish_a_running_configuration(self):
        self.glab.identity.side_effect = core.AutomationError("账号检查失败")
        self.controller.dispatch({"command": "start", "request_id": "failed"})
        self.finish()
        self.assertFalse(any(event["type"] == "listening_configuration" for event in self.output))
        self.runtime_factory.assert_not_called()
        self.assertTrue(any(event.get("status") == "error" for event in self.output))

    def test_inspection_does_not_replace_the_running_configuration(self):
        self.controller.inspect()
        self.assertFalse(any(event["type"] == "listening_configuration" for event in self.output))
        self.runtime_factory.assert_not_called()
        self.assertEqual(self.output[-1]["status"], "ready")


if __name__ == "__main__":
    unittest.main()
