from dataclasses import replace
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Engine"))
import listener_core as core
from configuration import DEFAULTS, configure_core
from protocol import Events, EventConsole, EventNotifier
from runtime import ApplicationEngine, PolicyGate
from worker import AppDws


class GitLabFake:
    def __init__(self):
        self.calls = []
        self.items = {}
        self.approved = set()
        self.commit_counts = {}
        self.lost_merge_response = False
        self.changed_sha = False
        self.preflight_barrier = None
        self.seen = set()
        self.lock = threading.Lock()

    def add(self, iid=1, **changes):
        ref = core.parse_mr_url(f"https://git2.superboss.cc/erp/core/-/merge_requests/{iid}")
        details = core.MRDetails(ref, "测试修改", "opened", "sha-original", 20, "作者", "author", None, "",
                                 False, False, "can_be_merged", "feature/test", "main", "3", "2026-10-01", "2026-10-08")
        self.items[iid] = replace(details, **changes)
        return ref

    def details(self, ref):
        with self.lock:
            first = ref.iid not in self.seen
            self.seen.add(ref.iid)
            self.calls.append(("details", ref.iid))
        if first and self.preflight_barrier and ref.iid <= 2:
            self.preflight_barrier.wait(timeout=2)
        return replace(self.items[ref.iid])

    def commit_count(self, ref):
        self.calls.append(("commits", ref.iid))
        self.commit_counts[ref.key] = 2
        return 2

    def approvals(self, ref, use_leader=False):
        self.calls.append(("approvals", ref.iid, use_leader))
        done = ref.iid in self.approved
        actor = 2 if use_leader else 1
        return core.ApprovalInfo(1, 0 if done else 1, done, True, {actor: "本人"} if done else {})

    def approve(self, ref, sha, use_leader=False):
        self.calls.append(("approve", ref.iid, sha, use_leader))
        self.approved.add(ref.iid)

    def merge(self, ref, sha, use_leader=False):
        self.calls.append(("merge", ref.iid, sha, use_leader))
        if self.changed_sha:
            self.items[ref.iid].sha = "sha-new"
            raise core.AutomationError("version changed", retryable=True)
        self.items[ref.iid].state = "merged"
        self.items[ref.iid].merged_by_id = 2 if use_leader else 1
        self.items[ref.iid].merged_by_name = "备用账号" if use_leader else "本人"
        if self.lost_merge_response:
            raise core.AutomationError("connection reset", retryable=True)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.settings = dict(DEFAULTS)
        configure_core(self.settings)
        self.events = []
        sink = Events(self.events.append)
        self.console = EventConsole(sink)
        self.glab = GitLabFake()
        self.receipts = []
        self.dws = type("DwsFake", (), {"add_received": lambda _, conversation, message: self.receipts.append(message)})()
        self.stop = threading.Event()
        self.gate = PolicyGate(self.console, lambda: self.settings)
        self.gate.refresh = lambda: False
        self.engine = ApplicationEngine(self.glab, self.dws, core.Identity(1, "本人", "me"), core.Identity(2, "备用", "backup"),
                                        self.console, EventNotifier(sink, lambda: self.settings), self.gate, self.stop,
                                        settings=self.settings, events=sink)

    def tearDown(self):
        configure_core(DEFAULTS)

    def process(self, *refs):
        self.engine.process(core.MessageTask("event", "message", "group", "发送人", tuple(refs)))
        return [event for event in self.events if event["type"] == "task_update"][-1]

    def mr_state(self, ref):
        state = {}
        for event in self.events:
            if event["type"] == "mr" and event["key"] == ref.key:
                state.update(event)
        return state

    def merge_405(self, ref, sha, use_leader=False):
        raise core.AutomationError("合并请求失败（HTTP 405）", "glab: Method Not Allowed (HTTP 405)", retryable=True)

    def finish_elsewhere(self, ref, **changes):
        self.glab.items[ref.iid] = replace(self.glab.items[ref.iid], state="merged",
                                         merged_by_id=20, merged_by_name="协作成员", **changes)

    def test_full_sequence_approves_and_merges_the_checked_sha(self):
        result = self.process(self.glab.add())
        self.assertEqual(self.glab.calls, [("details", 1), ("commits", 1), ("approvals", 1, False),
                                          ("approve", 1, "sha-original", False), ("approvals", 1, False),
                                          ("details", 1), ("merge", 1, "sha-original", False), ("details", 1)])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.receipts, ["message"])

    def test_mr_details_publish_the_server_link_to_the_native_ui(self):
        ref = self.glab.add()
        url = "https://git2.superboss.cc/erp/core/merge_requests/1"
        self.glab.items[1].web_url = url
        self.process(ref)
        links = [event["url"] for event in self.events if event["type"] == "mr" and "url" in event]
        self.assertTrue(links)
        self.assertEqual(set(links), {url})

    def test_conflicted_mr_has_no_writes_but_success_can_receive(self):
        self.settings["received_condition"] = "any_owned_merge"
        result = self.process(self.glab.add(), self.glab.add(2, has_conflicts=True))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["merged_count"], 1)
        self.assertEqual(self.receipts, ["message"])
        self.assertFalse(any(c[0] in {"approve", "merge"} and c[1] == 2 for c in self.glab.calls))

    def test_own_mr_uses_fallback_and_receipt_can_exclude_it(self):
        self.settings["include_fallback_for_received"] = False
        result = self.process(self.glab.add(author_id=1, author_username="me"))
        self.assertEqual(result["status"], "completed")
        self.assertIn(("approve", 1, "sha-original", True), self.glab.calls)
        self.assertIn(("merge", 1, "sha-original", True), self.glab.calls)
        self.assertEqual(self.receipts, [])

    def test_already_merged_by_other_person_has_no_writes_or_receipt(self):
        ref = self.glab.add(state="merged", merged_by_id=20, merged_by_name="同事")
        result = self.process(ref)
        self.assertFalse(any(c[0] in {"approve", "merge"} for c in self.glab.calls))
        self.assertEqual(self.receipts, [])
        self.assertEqual(result["merged_count"], 1)
        self.assertEqual(self.mr_state(ref)["merged_by"], "同事")
        self.assertEqual(self.mr_state(ref)["status"], "completed")

    def test_merge_405_confirmed_merged_by_other_person_completes_without_receipt(self):
        ref = self.glab.add()
        def conflict(ref, sha, use_leader=False):
            self.finish_elsewhere(ref)
            self.merge_405(ref, sha, use_leader)
        with patch.object(self.glab, "merge", side_effect=conflict) as merge:
            result = self.process(ref)
        self.assertEqual(merge.call_count, 1)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["merged_count"], 1)
        self.assertIn("已合并 1 个", result["summary"])
        self.assertEqual(result["error"], "")
        mr = self.mr_state(ref)
        self.assertEqual((mr["status"], mr["merged_by"], mr["error"]), ("completed", "协作成员", ""))
        self.assertEqual(mr["actor"], "本人")
        self.assertFalse(mr["owned_merge"])
        self.assertEqual(self.receipts, [])

    def test_default_receipt_accepts_own_merge_plus_confirmed_other_merge(self):
        first, second = self.glab.add(), self.glab.add(2)
        original = self.glab.merge
        def conflict(ref, sha, use_leader=False):
            if ref == first:
                return original(ref, sha, use_leader)
            self.finish_elsewhere(ref)
            self.merge_405(ref, sha, use_leader)
        with patch.object(self.glab, "merge", side_effect=conflict):
            result = self.process(first, second)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["merged_count"], 2)
        self.assertTrue(self.mr_state(first)["owned_merge"])
        self.assertFalse(self.mr_state(second)["owned_merge"])
        self.assertEqual(self.receipts, ["message"])

    def test_merge_405_with_open_mr_does_not_invent_success(self):
        ref = self.glab.add()
        with patch.object(self.glab, "merge", side_effect=self.merge_405) as merge, \
             patch.object(core, "MERGE_VERIFY_TIMEOUT_SECONDS", 0), patch.object(core, "RETRY_ATTEMPTS", 1):
            result = self.process(ref)
        self.assertEqual(merge.call_count, 1)
        self.assertEqual(result["merged_count"], 0)
        self.assertEqual(self.mr_state(ref)["status"], "failed")
        self.assertIn("405", result["error"])
        self.assertIn("仍显示未合并", result["error"])
        self.assertEqual(self.receipts, [])

    def test_merge_405_with_closed_mr_reports_closed_not_merged(self):
        ref = self.glab.add()
        def conflict(ref, sha, use_leader=False):
            self.glab.items[ref.iid].state = "closed"
            self.merge_405(ref, sha, use_leader)
        with patch.object(self.glab, "merge", side_effect=conflict) as merge:
            result = self.process(ref)
        self.assertEqual(merge.call_count, 1)
        self.assertIn("已关闭，未合并", result["error"])
        self.assertEqual(result["merged_count"], 0)
        self.assertEqual(self.receipts, [])

    def test_failed_verification_keeps_original_merge_405(self):
        ref = self.glab.add()
        original_details = self.glab.details
        failed = False
        def conflict(ref, sha, use_leader=False):
            nonlocal failed
            failed = True
            self.merge_405(ref, sha, use_leader)
        def details(ref):
            if failed:
                raise core.AutomationError("查询失败（HTTP 503）", "glab: Service Unavailable (HTTP 503)")
            return original_details(ref)
        with patch.object(self.glab, "details", side_effect=details), \
             patch.object(self.glab, "merge", side_effect=conflict) as merge, \
             patch.object(core, "RETRY_ATTEMPTS", 3), patch.object(core, "RETRY_DELAYS_SECONDS", [0]):
            result = self.process(ref)
        self.assertEqual(merge.call_count, 1)
        self.assertIn("405", result["error"])
        self.assertIn("503", result["error"])
        self.assertIn("无法确认 MR 最终状态", result["error"])
        self.assertEqual(result["error"].count("无法确认 MR 最终状态"), 1)
        self.assertEqual(result["merged_count"], 0)
        self.assertEqual(self.mr_state(ref)["status"], "failed")
        self.assertEqual(self.receipts, [])

    def test_retry_rechecks_server_before_another_merge_write(self):
        ref = self.glab.add()
        def during_backoff(_delay):
            self.finish_elsewhere(ref)
            return False
        with patch.object(self.stop, "wait", side_effect=during_backoff), \
             patch.object(self.glab, "merge", side_effect=self.merge_405) as merge, \
             patch.object(core, "MERGE_VERIFY_TIMEOUT_SECONDS", 0), \
             patch.object(core, "RETRY_ATTEMPTS", 2), patch.object(core, "RETRY_DELAYS_SECONDS", [0]):
            result = self.process(ref)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(merge.call_count, 1)
        self.assertEqual(self.mr_state(ref)["merged_by"], "协作成员")
        self.assertEqual(self.receipts, [])

    def test_final_verification_recovers_merge_after_last_poll(self):
        ref = self.glab.add()
        def after_poll(_plan, _initial=None):
            self.finish_elsewhere(ref)
            raise core.AutomationError("轮询未确认合并", retryable=True)
        with patch.object(self.engine, "_wait_until_merged", side_effect=after_poll), \
             patch.object(self.glab, "merge", side_effect=self.merge_405) as merge, \
             patch.object(core, "MERGE_VERIFY_TIMEOUT_SECONDS", 0), patch.object(core, "RETRY_ATTEMPTS", 1):
            result = self.process(ref)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(merge.call_count, 1)
        self.assertEqual(self.mr_state(ref)["merged_by"], "协作成员")
        self.assertEqual(self.mr_state(ref)["error"], "")
        self.assertEqual(self.receipts, [])

    def test_approval_405_is_also_reconciled_with_actual_merge_state(self):
        ref = self.glab.add()
        def conflict(ref, sha, use_leader=False):
            self.finish_elsewhere(ref)
            raise core.AutomationError("审批失败（HTTP 405）")
        with patch.object(self.glab, "approve", side_effect=conflict):
            result = self.process(ref)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["approved_count"], 0)
        self.assertEqual(result["merged_count"], 1)
        self.assertEqual(self.mr_state(ref)["merged_by"], "协作成员")
        self.assertFalse(any(call[0] == "merge" for call in self.glab.calls))
        self.assertEqual(self.receipts, [])

    def test_merged_without_identity_completes_without_claiming_ownership(self):
        ref = self.glab.add()
        def conflict(ref, sha, use_leader=False):
            self.glab.items[ref.iid].state = "merged"
            self.merge_405(ref, sha, use_leader)
        with patch.object(self.glab, "merge", side_effect=conflict):
            result = self.process(ref)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.mr_state(ref)["merged_by"], "")
        self.assertFalse(self.mr_state(ref)["owned_merge"])
        self.assertEqual(self.receipts, [])

    def test_merge_during_approval_is_detected_before_the_merge_write(self):
        ref = self.glab.add()
        approve = self.glab.approve
        def finish(ref, sha, use_leader=False):
            approve(ref, sha, use_leader)
            self.finish_elsewhere(ref)
        with patch.object(self.glab, "approve", side_effect=finish):
            result = self.process(ref)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(any(call[0] == "merge" for call in self.glab.calls))
        self.assertEqual(self.mr_state(ref)["merged_by"], "协作成员")
        self.assertEqual(self.receipts, [])

    def test_lost_success_response_is_verified_without_second_merge(self):
        self.glab.lost_merge_response = True
        result = self.process(self.glab.add())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len([c for c in self.glab.calls if c[0] == "merge"]), 1)

    def test_changed_head_stops_without_retrying_a_different_version(self):
        self.glab.changed_sha = True
        result = self.process(self.glab.add())
        self.assertIn("代码版本已经变化", result["error"])
        self.assertEqual(len([c for c in self.glab.calls if c[0] == "merge"]), 1)
        self.assertEqual(self.receipts, [])

    def test_parallel_mrs_obey_configured_limit(self):
        self.settings["max_concurrent_mrs"] = 2
        self.glab.preflight_barrier = threading.Barrier(2)
        active = []
        self.engine.progress = lambda: active.append(self.engine.active_mrs)
        self.process(*(self.glab.add(n) for n in range(1, 5)))
        self.assertEqual(max(active), 2)
        self.assertEqual(self.engine.active_mrs, 0)

    def test_stopped_task_does_not_start_gitlab_calls(self):
        ref = self.glab.add()
        self.stop.set()
        with self.assertRaises(core.ProcessingStopped): self.process(ref)
        self.assertEqual(self.glab.calls, [])

    def test_dingtalk_retry_rechecks_pause_before_each_write(self):
        dws = AppDws("/unused/dws", self.console, self.settings)
        dws.stop_event = self.stop
        dws.gate = Mock()
        dws.gate.wait_until_unlocked.side_effect = [True, False]
        with patch.object(core, "RETRY_DELAYS_SECONDS", [0]), patch.object(core, "run_command", side_effect=core.AutomationError("network", retryable=True)) as command:
            with self.assertRaises(core.ProcessingStopped): dws.mark_read("group", "message")
            self.assertEqual(command.call_count, 1)
            self.assertEqual(dws.gate.wait_until_unlocked.call_count, 2)


if __name__ == "__main__": unittest.main()
