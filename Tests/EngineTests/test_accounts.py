from contextlib import ExitStack
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Engine"))
import listener_core as core
from configuration import DEFAULTS
from protocol import Events
from worker import Controller


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.output = []
        self.controller = Controller(Events(self.output.append))
        self.resolve = self.stack.enter_context(patch("worker.executable", side_effect=lambda name, _: f"/fixture/{name}"))
        self.stack.enter_context(patch("worker.configure_core"))
        self.dws = self.stack.enter_context(patch("worker.AppDws")).return_value
        self.glab = self.stack.enter_context(patch("worker.AppGlab")).return_value
        self.dws.auth_status.return_value = {
            "authenticated": True, "token_valid": True, "corp_id": "corp", "user_id": "user",
            "user_name": "钉钉用户", "corp_name": "测试组织",
        }
        self.glab.identity.return_value = core.Identity(1, "GitLab 用户", "gitlab-user")

    def identities(self):
        return {event["service"]: event for event in self.output if event["type"] == "identity"}

    def test_reads_both_identities_without_listening_or_group_checks(self):
        self.controller.start = Mock()
        self.controller.accounts("request-1")
        identities = self.identities()
        self.assertEqual(identities["dingtalk"]["name"], "钉钉用户")
        self.assertEqual(identities["gitlab"]["detail"], "gitlab-user")
        self.assertTrue(all(event["request_id"] == "request-1" for event in self.output))
        self.dws.validate_consume.assert_not_called()
        self.dws.resolve_group.assert_not_called()
        self.dws.login_command.assert_not_called()
        self.glab.validate_capabilities.assert_not_called()
        self.controller.start.assert_not_called()
        self.assertIsNone(self.controller.runtime)

    def test_dingtalk_network_failure_does_not_hide_gitlab_account(self):
        self.dws.auth_status.side_effect = core.AutomationError("钉钉请求超时")
        self.controller.accounts("request-2")
        self.assertEqual(set(self.identities()), {"gitlab"})
        error = next(event for event in self.output if event.get("status") == "error")
        self.assertEqual((error["service"], error["detail"]), ("dingtalk", "钉钉请求超时"))

    def test_gitlab_failure_keeps_dingtalk_identity(self):
        self.glab.identity.side_effect = core.AutomationError("GitLab 返回 401")
        self.controller.accounts("request-3")
        self.assertEqual(set(self.identities()), {"dingtalk"})
        error = next(event for event in self.output if event.get("status") == "error")
        self.assertEqual((error["service"], error["detail"]), ("gitlab", "GitLab 返回 401"))

    def test_missing_component_only_fails_its_own_service(self):
        self.resolve.side_effect = lambda name, _: None if name == "dws" else "/fixture/glab"
        self.controller.accounts("missing-component")
        self.assertEqual(set(self.identities()), {"gitlab"})
        error = next(event for event in self.output if event.get("status") == "error")
        self.assertIn("dws", error["detail"])
        self.dws.auth_status.assert_not_called()

    def test_expired_or_incomplete_dingtalk_login_is_not_displayed_as_ready(self):
        for changes in ({"token_valid": False}, {"corp_id": ""}):
            with self.subTest(changes=changes):
                self.output.clear()
                self.dws.auth_status.return_value = {
                    "authenticated": True, "token_valid": True, "corp_id": "corp", "user_id": "user", **changes,
                }
                self.controller.accounts("invalid-login")
                self.assertEqual(set(self.identities()), {"gitlab"})
                self.assertTrue(any(event.get("service") == "dingtalk" and event.get("status") == "error" for event in self.output))

    def test_slow_dingtalk_does_not_delay_gitlab_and_completion_releases_lock(self):
        release_dws = threading.Event()
        gitlab_ready = threading.Event()
        finished = threading.Event()
        released_at_completion = []
        auth = self.dws.auth_status.return_value

        def slow_auth():
            if not release_dws.wait(5):
                raise TimeoutError("test did not release DingTalk lookup")
            return auth

        def receive(event):
            self.output.append(event)
            if event["type"] == "identity" and event["service"] == "gitlab":
                gitlab_ready.set()
            if event["type"] == "operation_finished":
                acquired = self.controller.operation.acquire(blocking=False)
                released_at_completion.append(acquired)
                if acquired:
                    self.controller.operation.release()
                finished.set()

        self.controller.events.sink = receive
        self.dws.auth_status.side_effect = slow_auth
        self.controller.dispatch({"command": "accounts", "request_id": "parallel", "settings": dict(DEFAULTS)})
        try:
            self.assertTrue(gitlab_ready.wait(3), "GitLab must become ready while DingTalk is still waiting")
            self.assertFalse(finished.is_set())
        finally:
            release_dws.set()
            self.assertTrue(finished.wait(3))
        self.assertEqual(released_at_completion, [True])
        self.assertEqual(self.output[-1]["request_id"], "parallel")

    def test_rejected_operation_is_correlated_with_the_request(self):
        with self.controller.operation:
            self.controller.dispatch({"command": "accounts", "request_id": "busy"})
        self.assertEqual(len(self.output), 1)
        self.assertEqual(self.output[0]["command"], "accounts")
        self.assertEqual(self.output[0]["request_id"], "busy")
        self.assertTrue(self.output[0]["operation_rejected"])


if __name__ == "__main__":
    unittest.main()
