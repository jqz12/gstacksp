import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from gsp.dispatch import accept_session_task, dispatch_tasks, load_dispatch
from gsp.runtime import Project, dispatch, parser, read
from gsp.sessions import register_session, write_report
from gsp.session_context import load_binding
try:
    from process_helpers import session_binding
except ModuleNotFoundError:
    from tests.process_helpers import session_binding


class AcceptDeferredCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-accept-deferred-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Deferred Cleanup Test")
        self.git("config", "user.email", "deferred@example.invalid")
        (self.root / "README.md").write_text("# fixture\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-qm", "chore: baseline")
        self.project = Project(self.root)
        self.project.initialize()
        self.cmd("new", "work", "--title", "延迟清理", "--intent", "cross-session")
        state = self.state()
        # This compatibility fixture predates independent convergence reviews;
        # its contract is integration proof before managed-tree disposal.
        state.pop('convergence_schema', None)
        self.project.write(state, 'fixture', 'legacy deferred cleanup',
                           action_uid='fixture:legacy-deferred-cleanup')
        self.project.document(state, "design").write_text(
            "# 延迟清理\n\n子任务接受后保留工作树，最终集成回归通过后再清理。\n", encoding="utf-8"
        )
        self.cmd("approve", "work", "--quote", "SYNTHETIC deferred cleanup design approved")
        for task_id in ("T01", "T02", "T03"):
            self.cmd(
                "task-add", "work", task_id, "--title", f"子任务 {task_id}",
                "--acceptance", f"{task_id} 回归通过", "--role", "backend",
            )
        self.cmd(
            "task-add", "work", "T04", "--title", "集成",
            "--acceptance", "集成回归通过", "--role", "integration",
            "--depends", "T01,T02,T03",
        )
        self.project.document(state, "plan").write_text(
            '# Plan\n\n' + '\n'.join(
                f'## Task {tid}: Independent child\n**Writes:** ' +
                json.dumps({'paths':[f'{tid}_test.py']}) + '\n'
                for tid in ('T01','T02','T03')) +
            '## Task T04: Integration\n**Writes:** ' +
            json.dumps({'paths':['T01_test.py','T02_test.py','T03_test.py','all_test.py']}) + '\n',
            encoding="utf-8"
        )
        self.cmd("seal-plan", "work")
        self.cmd("scope", "work", "--off", "--quote", "SYNTHETIC no freeze")
        self.cmd("topology-select", "work", "--mode", "in-place", "--quote", "SYNTHETIC control root")
        self.cmd("advance", "work", "--to", "execute")
        ctrl_state = self.state()
        ctrl_binding = session_binding(self.root, ctrl_state["work_uuid"], "main", "codex", "controller-thread")
        controller = register_session(self.project, ctrl_state, SimpleNamespace(
            role="controller", write_mode="write", session=None, host="codex",
            thread="controller-thread", task=None, worktree=str(self.root),
            branch="main", path=["."], context=str(self.root / ctrl_binding.handle_path),
        ))
        self.controller = controller["session"]["id"]
        self.controller_binding = load_binding(self.root / ctrl_binding.handle_path)

    def git(self, *args, cwd=None):
        result = subprocess.run(
            ["git", *args], cwd=cwd or self.root, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def cmd(self, *argv):
        values = list(argv)
        if "--" in values:
            split = values.index("--")
            args = parser().parse_args(values[:split])
            args.argv = values[split + 1:]
        else:
            args = parser().parse_args(values)
        return dispatch(self.project, args)[0]

    def state(self):
        return read(self.root / ".gsp/runs/work/state.json")

    def bind_dispatched_tasks(self, rows):
        state = self.state()
        for row in rows:
            task = next(item for item in state["tasks"] if item["id"] == row["task"])
            head = self.git("rev-parse", "HEAD", cwd=row["worktree"])
            task["topology"] = {
                "schema": 1,
                "status": "selected",
                "enforce": True,
                "intent": "cross-session",
                "effective_mode": "worktree",
                "requested_mode": "worktree",
                "control_root": str(self.root),
                "workspace_root": row["worktree"],
                "branch": row["branch"],
                "base_branch": "main",
                "base_head": row["base"],
                "head": head,
                "decision_quote": "SYNTHETIC fixture managed child",
                "decision_source": "caller-attributed-user",
                "task": row["task"],
            }
        self.project.write(state, "fixture-bind", "SYNTHETIC bind dispatched managed worktrees")

    def complete_and_accept(self, task_id, row):
        self.cmd("task-start", "work", task_id)
        worktree = Path(row["worktree"])
        test_file = worktree / f"{task_id}_test.py"
        test_file.write_text(
            "import unittest\n"
            "class Check(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )
        self.git("add", test_file.name, cwd=worktree)
        self.git("commit", "-qm", f"test: {task_id} fixture", cwd=worktree)
        self.cmd("observe-head", "work", "--task", task_id, "--reason", "SYNTHETIC inspect child commit")
        self.cmd("run", "work", "--task", task_id, "--kind", "tests", "--", sys.executable, str(test_file))
        self.cmd("task-done", "work", task_id)

        current = self.state()
        worker_binding = session_binding(self.root, current["work_uuid"], "child", "manual", f"worker-{task_id}")
        register_session(self.project, current, SimpleNamespace(
            role="worker", write_mode="write", session=row["session_hint"], host="manual",
            thread=f"worker-{task_id}", task=task_id, worktree=row["worktree"],
            branch=row["branch"], path=[test_file.name],
            context=str(self.root / worker_binding.handle_path),
        ))
        current = self.state()
        write_report(self.project, current, SimpleNamespace(
            session=row["session_hint"], task=task_id, status="DONE",
            summary=f"{task_id} 已完成", files=[test_file.name], tests=["fixture: 1 passed"],
            interfaces=[], evidence=[], need_from_main="", ruling="",
        ), caller_binding=load_binding(self.root / worker_binding.handle_path))
        current = self.state()
        accepted = accept_session_task(self.project, current, SimpleNamespace(
            task=task_id, session=self.controller, reason="SYNTHETIC controller verified report",
        ), caller_binding=self.controller_binding)
        self.assertEqual(accepted["acceptance"], "accepted")
        return accepted

    def prepare_accepted_children(self):
        current = self.state()
        dispatched = dispatch_tasks(self.project, current, SimpleNamespace(
            session=self.controller, tasks=["T01", "T02", "T03"],
            target=["T01=manual", "T02=manual", "T03=manual"],
            path=["T01=T01_test.py", "T02=T02_test.py", "T03=T03_test.py"],
            worktree=[], platform=None, dispatch_mode=None, max_parallel=3,
            retry=False, worktree_mode="auto",
        ))
        rows = dispatched["dispatches"]
        self.bind_dispatched_tasks(rows)
        for row in rows:
            result = self.complete_and_accept(row["task"], row)
            self.assertEqual(result["cleanup"]["result"], "deferred")
            self.assertTrue(Path(row["worktree"]).is_dir())
        return rows

    def integrate_and_run_all(self, rows):
        self.cmd(
            "topology-select", "work", "--task", "T04", "--mode", "worktree",
            "--quote", "SYNTHETIC integration worktree",
        )
        self.cmd("task-start", "work", "T04")
        integration = self.project.workspace_root(self.state(), task_id="T04")
        for row in rows:
            self.git("merge", "--no-edit", row["branch"], cwd=integration)
        all_test = integration / "all_test.py"
        all_test.write_text(
            "import unittest\n"
            "class Final(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )
        self.git("add", all_test.name, cwd=integration)
        self.git("commit", "-qm", "test: integration fixture", cwd=integration)
        self.cmd("observe-head", "work", "--task", "T04", "--reason", "SYNTHETIC inspect merged integration")
        self.cmd("run", "work", "--task", "T04", "--kind", "tests", "--", sys.executable, str(all_test))
        self.cmd("task-done", "work", "T04")
        receipt = self.cmd("run", "work", "--task", "all", "--kind", "tests", "--", sys.executable, str(all_test))
        self.assertEqual(receipt["status"], "pass")
        return receipt

    def test_accept_keeps_worktree_until_integration_passes(self):
        rows = self.prepare_accepted_children()
        self.integrate_and_run_all(rows)

        current = self.state()
        dispatch_state = load_dispatch(self.project, current)
        cleanup = dispatch_state["cleanup"]
        for row in rows:
            self.assertFalse(Path(row["worktree"]).exists())
            task_cleanup = [item for item in cleanup if item["task"] == row["task"]]
            self.assertTrue(task_cleanup)
            self.assertEqual(task_cleanup[-1]["result"], "removed")

        self.project.validate_integration(current)

    def test_integration_cleanup_failure_pending(self):
        rows = self.prepare_accepted_children()
        locked = rows[1]
        self.git("worktree", "lock", locked["worktree"])
        try:
            self.integrate_and_run_all(rows)

            current = self.state()
            dispatch_state = load_dispatch(self.project, current)
            pending = [
                item for item in dispatch_state["cleanup"]
                if item["task"] == locked["task"] and item["result"] == "cleanup-pending"
            ]
            self.assertTrue(pending)
            self.assertEqual(pending[-1]["cleanup_error"]["code"], "WORKTREE_REMOVE_FAILED")
            self.assertTrue(Path(locked["worktree"]).is_dir())
        finally:
            self.git("worktree", "unlock", locked["worktree"])

        retried = dispatch(self.project, parser().parse_args([
            "session-clean", "work", "--task", locked["task"], "--result", "removed",
            "--reason", "SYNTHETIC retry after unlock", "--retry",
        ]))[0]
        self.assertEqual(retried["cleanup"]["result"], "removed")
        self.assertFalse(Path(locked["worktree"]).exists())


if __name__ == "__main__":
    unittest.main()
