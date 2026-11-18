import subprocess
import tempfile
import unittest
from pathlib import Path

from gsp.runtime import Project, WorkflowError
from gsp.worktree_lifecycle import (
    cleanup_worktree,
    inspect_worktree,
    prepare_worktree,
)


class WorktreeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-worktree-lifecycle-")
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Lifecycle Test")
        self.git("config", "user.email", "lifecycle@example.invalid")
        (self.root / "README.md").write_text("# lifecycle\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-qm", "chore: baseline")
        self.project = Project(self.root)
        self.project.initialize()
        head = self.git_output("rev-parse", "HEAD")
        self.state = {
            "id": "cross-session-worktree-lifecycle",
            "number": "GSP-000021",
            "title": "跨会话 worktree 自动生命周期",
            "git_topology": {
                "workspace_root": str(self.root),
                "branch": "main",
                "head": head,
                "base_head": head,
            },
        }

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        result = subprocess.run(
            ["git", *args], cwd=cwd or self.root,
            capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def git_output(self, *args, cwd=None):
        return self.git(*args, cwd=cwd).stdout.strip()

    def dispatch(self, task="T01"):
        return {
            "id": "D001",
            "task": task,
            "worktree": None,
            "branch": None,
            "base": None,
            "claimed_paths": ["src"],
        }

    def task(self, task="T01"):
        return {"id": task, "title": "生命周期适配器"}

    def test_prepare_creates_dedicated_worktree_and_branch(self):
        result = prepare_worktree(self.project, self.state, self.task(), self.dispatch())

        self.assertTrue(result["managed_worktree"])
        self.assertEqual(result["worktree_mode"], "managed")
        self.assertEqual(result["worktree_state"], "created")
        self.assertTrue(Path(result["path"]).is_dir())
        self.assertTrue(result["branch"].startswith("feature/GSP-000021-T01-"))
        inventory = inspect_worktree(self.project, result["path"])
        self.assertEqual(inventory["branch"], result["branch"])
        self.assertEqual(inventory["head"], self.state["git_topology"]["head"])

    def test_explicit_binding_never_claims_delete_ownership(self):
        external = self.root / "external-worktree"
        self.git("worktree", "add", "-b", "feature/external", str(external))
        result = prepare_worktree(
            self.project, self.state, self.task("T02"), self.dispatch("T02"),
            requested_path=external, mode="bind"
        )

        self.assertFalse(result["managed_worktree"])
        self.assertEqual(result["worktree_mode"], "bound")
        cleaned = cleanup_worktree(self.project, result, "accepted")
        self.assertEqual(cleaned["result"], "released")
        self.assertTrue(external.exists())
        self.assertEqual(self.git_output("branch", "--show-current", cwd=external), "feature/external")

    def test_dirty_managed_worktree_becomes_cleanup_pending(self):
        result = prepare_worktree(self.project, self.state, self.task(), self.dispatch())
        path = Path(result["path"])
        (path / "uncommitted.txt").write_text("keep me\n", encoding="utf-8")

        cleaned = cleanup_worktree(self.project, result, "accepted")

        self.assertEqual(cleaned["result"], "cleanup-pending")
        self.assertTrue(path.exists())
        self.assertEqual(cleaned["cleanup_error"]["code"], "WORKTREE_DIRTY")

    def test_clean_managed_worktree_removal_is_idempotent(self):
        result = prepare_worktree(self.project, self.state, self.task(), self.dispatch())

        cleaned = cleanup_worktree(self.project, result, "accepted")
        repeated = cleanup_worktree(self.project, {**result, **cleaned}, "retry")

        self.assertEqual(cleaned["result"], "removed")
        self.assertFalse(Path(result["path"]).exists())
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["result"], "removed")

    def test_existing_branch_collision_is_rejected_before_registration(self):
        path = self.root / "collision"
        self.git("worktree", "add", "-b", "feature/GSP-000021-T01-worktree", str(path))

        with self.assertRaisesRegex(WorkflowError, "WORKTREE_BRANCH_CONFLICT"):
            prepare_worktree(self.project, self.state, self.task(), self.dispatch())


if __name__ == "__main__":
    unittest.main()


