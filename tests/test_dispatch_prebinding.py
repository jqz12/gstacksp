import subprocess
import tempfile
import unittest
from pathlib import Path

from gsp.dispatch import load_dispatch
from gsp.git_topology import branch_name
from gsp.runtime import Project, WorkflowError, dispatch, parser, read


class DispatchPrebindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-dispatch-prebind-")
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Prebind Test")
        self.git("config", "user.email", "prebind@example.invalid")
        (self.root / "README.md").write_text("# prebind\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-qm", "chore: baseline")
        self.project = Project(self.root)
        self.project.initialize()
        self.cmd("new", "work", "--title", "派发预绑定", "--intent", "cross-session")
        state = self.state()
        self.project.document(state, "design").write_text("# Design\nFixture prebinding.", encoding="utf-8")
        self.cmd("approve", "work", "--quote", "SYNTHETIC approved")
        self.cmd("task-add", "work", "T10", "--title", "编号服务",
                 "--acceptance", "绑定后立即可写", "--role", "backend")
        self.project.document(state, "plan").write_text("# Plan\nT10 prebinding.", encoding="utf-8")
        self.cmd("seal-plan", "work")
        self.cmd("scope", "work", "--off", "--quote", "SYNTHETIC no freeze")
        state = self.state()
        head = self.git_output("rev-parse", "HEAD")
        state["git_topology"] = {
            "schema": 1,
            "status": "selected",
            "enforce": True,
            "intent": "cross-session",
            "requested_mode": "in-place",
            "effective_mode": "in-place",
            "control_root": str(self.root),
            "workspace_root": str(self.root),
            "base_branch": "main",
            "base_head": head,
            "branch": "main",
            "head": head,
        }
        self.project.write(state, "fixture", "prebind test topology")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        result = subprocess.run(
            ["git", *args], cwd=cwd or self.root,
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def git_output(self, *args, cwd=None):
        return self.git(*args, cwd=cwd).stdout.strip()

    def cmd(self, *argv):
        args = parser().parse_args(list(argv))
        result, ok = dispatch(self.project, args)
        self.assertTrue(ok, result)
        return result

    def state(self):
        return read(self.root / ".gsp/runs/work/state.json")

    def register_controller(self):
        self.cmd("session-register", "work", "--role", "controller", "--host", "codex",
                 "--thread", "main-thread", "--write-mode", "write", "--path", ".")

    def dispatch_t10(self):
        return self.cmd(
            "session-dispatch", "work", "--session", "S01", "--task", "T10",
            "--target", "T10=manual", "--path", "T10=src", "--worktree-mode", "auto",
        )

    def test_dispatch_binds_task_immediately(self):
        self.register_controller()
        result = self.dispatch_t10()
        row = result["dispatches"][0]

        self.assertTrue(row["managed_worktree"])
        task = next(t for t in self.state()["tasks"] if t["id"] == "T10")
        topology = task["topology"]
        self.assertEqual(topology["status"], "selected")
        self.assertEqual(Path(topology["workspace_root"]).resolve(), Path(row["worktree"]).resolve())
        self.assertIn("T10", topology["branch"])
        self.assertEqual(topology["decision_source"], "controller-dispatch")
        self.assertIsNone(topology.get("decision_quote"))

        state = self.state()
        state["status"] = "execute"
        self.project.write(state, "fixture", "prebind execute")
        self.cmd("task-start", "work", "T10")
        self.cmd("check-path", "work", str(Path(row["worktree"]) / "src" / "service.py"))

    def test_dispatch_bind_failure_rolls_back(self):
        self.register_controller()
        state = self.state()
        number = state["number"]
        task = next(t for t in state["tasks"] if t["id"] == "T10")
        branch = branch_name(number, "T10", task["title"])
        collision = self.root / ".gsp" / "worktrees" / "collision"
        self.git("worktree", "add", "-b", branch, str(collision))

        with self.assertRaisesRegex(WorkflowError, "WORKTREE_BRANCH_CONFLICT"):
            self.dispatch_t10()

        managed = self.root / ".gsp" / "worktrees" / "sessions" / number / "T10-worktree"
        self.assertFalse(managed.exists())
        data = load_dispatch(self.project, self.state())
        self.assertFalse(any(row.get("task") == "T10" for row in data.get("dispatches", [])))


if __name__ == "__main__":
    unittest.main()
