from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Engine"))
import listener_core as core


class MergeIdentityTests(unittest.TestCase):
    def details(self, **fields):
        client = core.GlabClient("unused-glab", Mock(), "git2.superboss.cc")
        client.api = Mock(return_value={"state": "merged", **fields})
        return client.details(core.parse_mr_url("https://git2.superboss.cc/erp/core/merge_requests/1"))

    def test_current_merge_user_field_has_priority_over_legacy_identity(self):
        result = self.details(merge_user={"id": 20, "name": "实际合并人"},
                              merged_by={"id": 1, "name": "旧字段账号"})
        self.assertEqual((result.merged_by_id, result.merged_by_name), (20, "实际合并人"))

    def test_legacy_server_and_empty_current_field_keep_the_merger(self):
        for current in (None, {}, "invalid"):
            with self.subTest(current=current):
                result = self.details(merge_user=current, merged_by={"id": 20, "name": "同事"})
                self.assertEqual((result.merged_by_id, result.merged_by_name), (20, "同事"))

    def test_missing_name_uses_username_without_mixing_two_people(self):
        result = self.details(merge_user={"id": 20, "username": "teammate"},
                              merged_by={"id": 1, "name": "其他账号"})
        self.assertEqual((result.merged_by_id, result.merged_by_name), (20, "teammate"))

    def test_missing_merger_still_reports_merged_without_inventing_identity(self):
        result = self.details()
        self.assertEqual(result.state, "merged")
        self.assertIsNone(result.merged_by_id)
        self.assertEqual(result.merged_by_name, "")

    def test_current_merger_without_id_does_not_inherit_a_legacy_account(self):
        result = self.details(merge_user={"name": "实际合并人"}, merged_by={"id": 1, "name": "其他账号"})
        self.assertIsNone(result.merged_by_id)
        self.assertEqual(result.merged_by_name, "实际合并人")


if __name__ == "__main__": unittest.main()
