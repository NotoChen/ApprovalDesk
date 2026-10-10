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


class GroupSearchTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.output = []
        self.finished = threading.Event()

        def receive(event):
            self.output.append(event)
            if event["type"] == "operation_finished":
                self.finished.set()

        self.controller = Controller(Events(receive))
        self.stack.enter_context(patch("worker.executable", return_value="/fixture/dws"))
        self.stack.enter_context(patch("worker.core.ensure_dws_authentication"))
        self.dws_factory = self.stack.enter_context(patch("worker.AppDws"))
        self.dws = self.dws_factory.return_value
        self.dws.profile = "corp:user"

    def search(self, kind, query):
        self.output.clear()
        self.finished.clear()
        self.controller.dispatch({"command": "groups", "kind": kind, "query": query, "request_id": "group-request"})
        self.assertTrue(self.finished.wait(3), "Group search did not finish")
        self.assertEqual(self.output[-1]["command"], "groups")

    def test_both_group_selectors_receive_candidates_and_their_target(self):
        for kind, query in (("approval", "代码审批"), ("sender", "向钱冲")):
            with self.subTest(kind=kind):
                self.dws.search_groups.return_value = [core.GroupCandidate(query, "group-1", 9)]
                self.search(kind, query)
                self.assertEqual([event for event in self.output if event["type"] == "error"], [])
                result = next(event for event in self.output if event["type"] == "groups")
                self.assertEqual(result["kind"], kind)
                self.assertEqual(result["profile"], "corp:user")
                self.assertEqual(result["request_id"], "group-request")
                self.assertEqual(result["groups"], [{"id": "group-1", "title": query, "count": 9}])
                self.dws.search_groups.assert_called_with(query)
                self.dws.validate_consume.assert_not_called()

    def test_empty_search_returns_an_empty_group_list(self):
        self.dws.search_groups.return_value = []
        self.search("approval", "没有匹配的群")
        self.assertEqual([event for event in self.output if event["type"] == "error"], [])
        result = next(event for event in self.output if event["type"] == "groups")
        self.assertEqual(result["groups"], [])

    def test_search_failure_reports_the_cause_and_releases_the_operation(self):
        self.dws.search_groups.side_effect = core.AutomationError("群聊搜索请求超时")
        self.search("sender", "向钱冲")
        error = next(event for event in self.output if event["type"] == "error")
        self.assertEqual(error["message"], "群聊搜索请求超时")
        self.assertEqual(error["command"], "groups")
        self.assertFalse(self.controller.operation.locked())
        self.assertFalse(self.controller.group_operation.locked())

    def test_group_search_runs_while_listening_owns_the_main_operation(self):
        runtime = Mock()
        self.controller.runtime = runtime
        with self.controller.operation:
            for kind in ("approval", "sender"):
                self.dws.search_groups.return_value = [core.GroupCandidate("候选群", "new-group", 9)]
                self.search(kind, "候选群")
                self.assertFalse(any(event["type"] == "error" for event in self.output))
                self.assertFalse(any(event["type"] == "state" for event in self.output))
                self.assertTrue(self.controller.operation.locked())
                self.assertIs(self.controller.runtime, runtime)
        runtime.request_stop.assert_not_called()
        runtime.clear_queue.assert_not_called()
        runtime.set_paused.assert_not_called()

    def test_failed_lookup_can_retry_without_releasing_the_listener_operation(self):
        with self.controller.operation:
            self.dws.search_groups.side_effect = core.AutomationError("查询失败")
            self.search("approval", "候选群")
            error = next(event for event in self.output if event["type"] == "error")
            self.assertEqual(error["request_id"], "group-request")
            self.assertTrue(self.controller.operation.locked())
            self.assertFalse(self.controller.group_operation.locked())
            self.dws.search_groups.side_effect = None
            self.dws.search_groups.return_value = []
            self.search("sender", "候选群")
            self.assertFalse(any(event["type"] == "error" for event in self.output))
            self.assertTrue(self.controller.operation.locked())

    def test_duplicate_lookup_rejection_does_not_complete_the_first_request(self):
        entered, release = threading.Event(), threading.Event()
        def search(_query):
            entered.set()
            release.wait(3)
            return []
        self.dws.search_groups.side_effect = search
        self.controller.dispatch({"command": "groups", "query": "first", "request_id": "first"})
        try:
            self.assertTrue(entered.wait(2))
            self.controller.dispatch({"command": "groups", "query": "second", "request_id": "second"})
            error = next(event for event in self.output if event["type"] == "error")
            self.assertTrue(error["operation_rejected"])
            self.assertEqual(error["request_id"], "second")
            self.assertFalse(self.finished.is_set())
        finally:
            release.set()
            self.assertTrue(self.finished.wait(3))
        finished = [event["request_id"] for event in self.output if event["type"] == "operation_finished"]
        self.assertEqual(finished, ["first"])

    def test_lookup_uses_requested_settings_snapshot_when_configuration_changes(self):
        entered, release = threading.Event(), threading.Event()
        self.controller.settings["profile"] = "corp:first"
        self.dws.profile = "corp:first"
        def search(_query):
            entered.set()
            release.wait(3)
            return []
        self.dws.search_groups.side_effect = search
        self.controller.dispatch({"command": "groups", "query": "候选群", "request_id": "old-profile"})
        try:
            self.assertTrue(entered.wait(2))
            self.controller.settings["profile"] = "corp:second"
        finally:
            release.set()
            self.assertTrue(self.finished.wait(3))
        self.assertEqual(self.dws_factory.call_args.args[2]["profile"], "corp:first")
        self.assertEqual(self.controller.settings["profile"], "corp:second")
        result = next(event for event in self.output if event["type"] == "groups")
        self.assertEqual((result["request_id"], result["profile"]), ("old-profile", "corp:first"))


if __name__ == "__main__":
    unittest.main()
