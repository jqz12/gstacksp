import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from gsp.dispatch import dispatch_tasks
from gsp.runtime import Project, WorkflowError, dispatch, parser, read
from gsp.sessions import load_registry, register_session, write_report


class SessionAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-session-acceptance-")
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Acceptance Test")
        self.git("config", "user.email", "acceptance@example.invalid")
        (self.root / "README.md").write_text("# acceptance\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-qm", "chore: baseline")
        self.project = Project(self.root)
        self.project.initialize()
        self.cmd("new", "work", "--title", "接受清理", "--intent", "cross-session")
        self.cmd("task-add", "work", "T01", "--title", "服务", "--acceptance", "服务测试", "--role", "backend")
        self.state = read(self.root / ".gsp/runs/work/state.json")
        head = self.git_output("rev-parse", "HEAD")
        self.state["git_topology"] = {
            "enforce": True,
            "intent": "cross-session",
            "workspace_root": str(self.root),
            "control_root": str(self.root),
            "branch": "main",
            "head": head,
            "base_head": head,
        }
        self.project.write(self.state, "fixture", "test topology")
        self.state = read(self.root / ".gsp/runs/work/state.json")
        register_session(self.project, self.state, SimpleNamespace(
            role="controller", write_mode="write", session=None, host="codex",
            thread="main-thread", task=None, worktree=str(self.root), branch="main", path=["."],
        ))
        self.state = read(self.root / ".gsp/runs/work/state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        result = subprocess.run(["git", *args], cwd=cwd or self.root,
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def git_output(self, *args, cwd=None):
        return self.git(*args, cwd=cwd).stdout.strip()

    def cmd(self, *argv):
        return dispatch(self.project, parser().parse_args(list(argv)))[0]

    def dispatch_args(self, **overrides):
        values = {
            "session": "S01", "tasks": ["T01"], "target": ["T01=manual"],
            "path": ["T01=src"], "worktree": [], "platform": None,
            "dispatch_mode": None, "max_parallel": 2, "retry": False,
            "worktree_mode": "auto",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def dispatch_one(self, **overrides):
        current = read(self.root / ".gsp/runs/work/state.json")
        result = dispatch_tasks(self.project, current, self.dispatch_args(**overrides))
        return result["dispatches"][0], read(self.root / ".gsp/runs/work/state.json")

    def report(self, dispatch_row, status="DONE"):
        current = read(self.root / ".gsp/runs/work/state.json")
        register_session(self.project, current, SimpleNamespace(
            role="worker", write_mode="write", session="S02", host="manual",
            thread="worker-thread", task="T01", worktree=dispatch_row["worktree"], branch=dispatch_row.get("branch"), path=["src"],
        ))
        current = read(self.root / ".gsp/runs/work/state.json")
        write_report(self.project, current, SimpleNamespace(
            session="S02", task="T01", status=status, summary="完成局部实现",
            files=["src/service.py"], tests=["unit: pass"], interfaces=[], evidence=[], need_from_main="", ruling="",
        ))
        current = read(self.root / ".gsp/runs/work/state.json")
        current["tasks"][0]["status"] = "done"
        self.project.write(current, "fixture-done", "report and test evidence")
        return read(self.root / ".gsp/runs/work/state.json")

    def test_dispatch_without_worktree_creates_managed_resource(self):
        row, _ = self.dispatch_one()

        self.assertTrue(row["managed_worktree"])
        self.assertEqual(row["worktree_state"], "created")
        self.assertTrue(Path(row["worktree"]).is_dir())
        self.assertEqual(row["worktree_mode"], "managed")

    def test_acceptance_releases_lease_and_removes_clean_managed_worktree(self):
        row, _ = self.dispatch_one()
        current = self.report(row)

        from gsp.dispatch import accept_session_task
        result = accept_session_task(self.project, current, SimpleNamespace(
            task="T01", session="S01", reason="controller verified report and tests"
        ))

        self.assertEqual(result["acceptance"], "accepted")
        self.assertEqual(result["cleanup"]["result"], "removed")
        self.assertFalse(Path(row["worktree"]).exists())
        registry = load_registry(self.project, current)
        self.assertEqual(registry["tasks"]["T01"]["acceptance_status"], "accepted")
        self.assertEqual(registry["tasks"]["T01"]["cleanup_status"], "removed")
        self.assertNotIn("S02", registry["workspace_leases"].values())

    def test_dirty_acceptance_keeps_worktree_pending(self):
        row, _ = self.dispatch_one()
        Path(row["worktree"], "uncommitted.txt").write_text("keep\n", encoding="utf-8")
        current = self.report(row)

        from gsp.dispatch import accept_session_task
        result = accept_session_task(self.project, current, SimpleNamespace(
            task="T01", session="S01", reason="accept despite retained diagnostics"
        ))

        self.assertEqual(result["acceptance"], "accepted")
        self.assertEqual(result["cleanup"]["result"], "cleanup-pending")
        self.assertTrue(Path(row["worktree"]).exists())
        self.assertEqual(result["cleanup"]["cleanup_error"]["code"], "WORKTREE_DIRTY")

    def test_bound_worktree_is_released_without_removal(self):
        external = self.root / ".gsp" / "worktrees" / "bound"
        self.git("worktree", "add", "-b", "feature/bound", str(external))
        row, _ = self.dispatch_one(worktree=[f"T01={external}"])
        current = self.report(row)

        from gsp.dispatch import accept_session_task
        result = accept_session_task(self.project, current, SimpleNamespace(
            task="T01", session="S01", reason="bound worktree accepted"
        ))

        self.assertFalse(row["managed_worktree"])
        self.assertEqual(result["cleanup"]["result"], "released")
        self.assertTrue(external.exists())

    def test_non_done_report_cannot_be_accepted(self):
        row, _ = self.dispatch_one()
        current = self.report(row, status="BLOCKED")

        from gsp.dispatch import accept_session_task
        with self.assertRaisesRegex(WorkflowError, "ACCEPTANCE_REPORT_REQUIRED"):
            accept_session_task(self.project, current, SimpleNamespace(
                task="T01", session="S01", reason="should reject blocked report"
            ))
        self.assertTrue(Path(row["worktree"]).exists())


if __name__ == "__main__":
    unittest.main()



