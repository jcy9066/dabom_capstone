"""Read-only version audit: never authorize deployment based on only one host."""
import unittest

from scripts.check_runtime_versions import check_versions

SHA = "a" * 40
OTHER = "b" * 40


class RuntimeVersionAuditTests(unittest.TestCase):
    def setUp(self):
        self.local = {"schema": 1, "sha": SHA, "branch": "dev", "dirty": False}

    def test_exact_release_match(self):
        self.assertEqual([], check_versions(self.local, dict(self.local), SHA))

    def test_branch_name_does_not_override_identical_sha(self):
        peer = dict(self.local, branch="final")
        self.assertEqual([], check_versions(self.local, peer, SHA))

    def test_different_peer_commit_fails_closed(self):
        issues = check_versions(self.local, dict(self.local, sha=OTHER), SHA)
        self.assertIn("GPU_PI_HEAD_MISMATCH", issues)

    def test_stale_expected_release_rejected(self):
        self.assertIn("LOCAL_HEAD_NOT_EXPECTED", check_versions(self.local, None, OTHER))

    def test_dirty_peer_refused(self):
        self.assertIn("PEER_WORKTREE_NOT_CLEAN",
                      check_versions(self.local, dict(self.local, dirty=True), SHA))

    def test_peer_missing_clean_state_refused(self):
        self.assertIn("PEER_WORKTREE_NOT_CLEAN",
                      check_versions(self.local, {"schema": 1, "sha": SHA}, SHA))

    def test_missing_or_bad_identity_refused(self):
        self.assertIn("PEER_IDENTITY_INVALID",
                      check_versions(self.local, {"schema": 1, "sha": "bad"}, SHA))

    def test_local_dirty_refused(self):
        self.assertIn("LOCAL_WORKTREE_NOT_CLEAN",
                      check_versions(dict(self.local, dirty=True), self.local, SHA))


if __name__ == "__main__":
    unittest.main()
