from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Engine"))
import listener_core as core


class MRLinkTests(unittest.TestCase):
    legacy = "https://git2.superboss.cc/erp/erp-core/merge_requests/42"
    modern = "https://git2.superboss.cc/erp/erp-core/-/merge_requests/42"

    def test_legacy_browser_link_does_not_gain_a_separator(self):
        ref = core.parse_mr_url(self.legacy)
        self.assertEqual(ref.url, self.legacy)
        self.assertEqual(ref.api_base, "projects/erp%2Ferp-core/merge_requests/42")

    def test_modern_browser_link_keeps_its_separator(self):
        self.assertEqual(core.parse_mr_url(self.modern).url, self.modern)

    def test_subgroups_query_and_fragment_survive_message_parsing(self):
        url = "https://git2.superboss.cc/team/sub-group/repo/merge_requests/42?diff_id=8#note_12"
        ref = core.extract_mr_refs(f"请审批 {url}。")[0]
        self.assertEqual(ref.url, url)
        self.assertEqual(ref.project_path, "team/sub-group/repo")

    def test_browser_route_does_not_change_mr_deduplication(self):
        refs = core.extract_mr_refs(self.legacy + " " + self.modern)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].key, "erp/erp-core!42")
        self.assertEqual(refs[0].url, self.legacy)

    def test_details_use_the_server_browser_url(self):
        client = core.GlabClient("unused-glab", Mock(), "git2.superboss.cc")
        client.api = Mock(return_value={"web_url": self.legacy})
        details = client.details(core.parse_mr_url(self.modern))
        self.assertEqual(details.web_url, self.legacy)
        client.api.assert_called_once_with("projects/erp%2Ferp-core/merge_requests/42", "查询 erp/erp-core!42", False)

    def test_missing_server_browser_url_keeps_the_message_link(self):
        client = core.GlabClient("unused-glab", Mock(), "git2.superboss.cc")
        for value in (None, "", "   "):
            with self.subTest(value=value):
                client.api = Mock(return_value={"web_url": value})
                self.assertEqual(client.details(core.parse_mr_url(self.legacy)).web_url, self.legacy)


if __name__ == "__main__": unittest.main()
