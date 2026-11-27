"""Real CLI coverage for the controller-only acceptance boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from gsp.runtime import (
    Project,
    WorkflowError,
    _require_controller_acceptance,
    contract_hash,
    dispatch,
    fingerprint,
    parser,
)

try:
    from process_helpers import run, session_binding
except ModuleNotFoundError:
    from tests.process_helpers import run, session_binding


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "gsp.py"


class ControllerAcceptanceGateCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-controller-accept-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Controller Acceptance")
        self.git("config", "user.email", "controller-acceptance@example.invalid")
        (self.root / "README.md").write_text("# fixture\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-qm", "chore: baseline")

        self.run_cli("init")
        self.run_cli(
            "new", "work", "--title", "Controller acceptance",
            "--intent", "cross-session",
        )
        self.project = Project(self.root)
        state = self.state()
        main = session_binding(
            self.root, state["work_uuid"], "main", "codex", "main-thread",
        )
        self.main_context = self.root / main.handle_path
        self.run_cli(
            "session-register", "work", "--session", "S01",
            "--role", "controller", "--host", "codex",
            "--thread", "main-thread", "--write-mode", "write",
            "--path", ".", "--context", str(self.main_context),
        )

        design = self.root / self.state()["documents"]["design"]
        design.write_text(
            "# Controller acceptance design\n\n"
            "Only the current controller may finish and close the work.\n",
            encoding="utf-8",
        )
        self.run_cli("approve", "work", "--quote", "SYNTHETIC approval")
        self.run_cli(
            "task-add", "work", "T01", "--title", "Acceptance behavior",
            "--acceptance", "The focused unittest passes",
        )
        plan = self.root / self.state()["documents"]["plan"]
        plan.write_text(
            "# Controller acceptance plan\n\n"
            "## T01: Acceptance behavior\n"
            'Convergence: {"requires_slices":false,'
            '"single_outcome":"The focused unittest passes"}\n',
            encoding="utf-8",
        )
        self.run_cli("seal-plan", "work")
        self.run_cli(
            "topology-select", "work", "--mode", "in-place",
            "--quote", "SYNTHETIC in-place acceptance fixture",
        )
        self.run_cli("advance", "work", "--to", "execute")
        self.run_cli("task-start", "work", "T01")

        (self.root / "app.py").write_text(
            "def accepted():\n    return True\n", encoding="utf-8",
        )
        (self.root / "test_acceptance.py").write_text(
            "import unittest\n"
            "from app import accepted\n"
            "class AcceptanceTests(unittest.TestCase):\n"
            "    def test_accepted(self):\n"
            "        self.assertTrue(accepted())\n",
            encoding="utf-8",
        )
        self.run_cli(
            "run", "work", "--task", "T01", "--kind", "tests", "--",
            sys.executable, "-m", "unittest", "-v", "test_acceptance.py",
        )

        worker_tree = self.root / ".gsp" / "worktrees" / "worker"
        self.git(
            "worktree", "add", "-q", "-b", "feature/controller-gate",
            str(worker_tree), "HEAD",
        )
        state = self.state()
        worker = session_binding(
            self.root, state["work_uuid"], "child", "codex", "worker-thread",
            capabilities={"claim", "report", "write"},
        )
        self.worker_context = self.root / worker.handle_path
        self.run_cli(
            "session-register", "work", "--session", "S02",
            "--role", "worker", "--host", "codex", "--thread", "worker-thread",
            "--task", "T01", "--branch", "feature/controller-gate",
            "--worktree", str(worker_tree), "--write-mode", "write",
            "--path", "src", "--context", str(self.worker_context),
        )

        state = self.state()
        spare = session_binding(
            self.root, state["work_uuid"], "main", "codex", "spare-main",
        )
        self.spare_main_context = self.root / spare.handle_path

        self.card = self.root / ".gsp" / "fixture-acceptance-card.json"
        self.card.write_text(json.dumps({
            "id": "AC01",
            "kind": "test",
            "required": True,
            "status": "PASS",
            "steps": ["Run the focused unittest"],
            "expected": "The focused unittest passes",
            "observed": "The focused unittest passed",
            "evidence": ["test_acceptance.py"],
        }), encoding="utf-8")

    def git(self, *args):
        result = subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True,
            text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def cli_env(self, context):
        env = os.environ.copy()
        env.pop("GSP_SESSION_CONTEXT", None)
        env.update({
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "CODEX_HOME": str(self.home / ".codex"),
        })
        if context is not None:
            env["GSP_SESSION_CONTEXT"] = str(context)
        return env

    def invoke(self, context, *args):
        return run(
            [
                sys.executable, "-X", "utf8", "-B", str(CLI),
                "--project", str(self.root), *args,
            ],
            capture_output=True, text=True, encoding="utf-8",
            env=self.cli_env(context), cwd=self.root,
        )

    def run_cli(self, *args):
        result = self.invoke(getattr(self, "main_context", None), *args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def rejected_cli(self, context, *args):
        result = self.invoke(context, *args)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def state(self):
        return json.loads(
            (self.root / ".gsp/runs/work/state.json").read_text(encoding="utf-8")
        )

    def snapshot(self):
        paths = [
            self.root / ".gsp/runs/work/state.json",
            self.root / ".gsp/runs/work/events.jsonl",
            self.root / ".gsp/runs/work/sessions/index.json",
        ]
        acceptance = self.root / ".gsp/runs/work/acceptance"
        candidates = sorted(acceptance.rglob("*")) if acceptance.exists() else []
        for path in candidates:
            if path.is_file():
                paths.append(path)
        return {
            path.relative_to(self.root).as_posix(): (
                path.read_bytes() if path.is_file() else None
            )
            for path in paths
        }

    def assert_rejected_without_mutation(self, context, error, *args):
        before = self.snapshot()
        result = self.rejected_cli(context, *args)
        self.assertIn(error, result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_worker_and_spare_main_cannot_accept_then_current_main_completes(self):
        commands = (
            ("task-done", "work", "T01"),
            ("acceptance-record", "work", "T01", "--card", str(self.card)),
            ("ready", "work"),
            ("close", "work", "--reason", "Worker must not close the work"),
        )
        for command in commands:
            with self.subTest(worker=command[0]):
                self.assert_rejected_without_mutation(
                    self.worker_context, "CAPABILITY_REQUIRED accept", *command,
                )

        self.assert_rejected_without_mutation(
            self.spare_main_context,
            "SESSION_CONTROLLER_REQUIRED",
            "task-done", "work", "T01",
        )

        self.run_cli("task-done", "work", "T01")
        self.run_cli(
            "acceptance-record", "work", "T01", "--card", str(self.card),
        )
        self.run_cli(
            "run", "work", "--task", "all", "--kind", "tests", "--",
            sys.executable, "-m", "unittest", "-v", "test_acceptance.py",
        )

        review = self.root / self.state()["doc_dir"] / "review.md"
        project = Project(self.root)
        state = project.ledger_state('work')
        reviewer = session_binding(self.root, state['work_uuid'], kind='reviewer',
                                   thread='separate-acceptance-review', capabilities={'report'})
        proof = {'candidate_digest': fingerprint(project.verification_root(state)),
                 'contract_digest': contract_hash(project, state, project.paths('work')[1]),
                 'reviewer_context': reviewer.handle_path, 'findings': []}
        review.write_text(
            "# Controller acceptance review\n\n"
            "A separate reviewer checked the focused behavior and final run.\n"
            '<!-- gsp-review-evidence\n' + json.dumps(proof) + '\n-->\n',
            encoding="utf-8",
        )
        self.run_cli("advance", "work", "--to", "review")
        self.run_cli("review", "work", "--reviewer", "fresh-context",
                     "--blocking", "0")
        self.run_cli("advance", "work", "--to", "checking")
        self.run_cli("advance", "work", "--to", "ready")

        self.assert_rejected_without_mutation(
            self.worker_context, "CAPABILITY_REQUIRED accept", "ready", "work",
        )
        self.run_cli("ready", "work")

        self.assert_rejected_without_mutation(
            self.worker_context, "CAPABILITY_REQUIRED accept",
            "close", "work", "--reason", "Worker must not close the work",
        )
        state = self.state()
        self.run_cli(
            "work-stage", state["work_uuid"], "closed",
            "--reason", "SYNTHETIC closure condition",
            "--closure-condition", "Current controller accepted the fixture",
        )
        self.run_cli("close", "work", "--reason", "Current controller accepted")
        self.assertEqual(self.state()["status"], "closed")


class ControllerAcceptanceLegacyTests(unittest.TestCase):
    def test_legacy_single_main_keeps_controller_fence_without_new_capability(self):
        with tempfile.TemporaryDirectory(prefix="gsp-controller-legacy-") as temp:
            root = Path(temp)
            project = Project(root)
            project.initialize()
            dispatch(project, parser().parse_args([
                "new", "legacy", "--title", "Legacy main",
            ]))
            state = project.ledger_state("legacy")
            state["controller_required"] = False
            state["convergence_schema"] = None
            project.write(
                state, "fixture-legacy-controller", "legacy single-main fixture",
                action_uid="fixture-legacy-controller",
            )
            state = project.ledger_state("legacy")
            binding = session_binding(
                root, state["work_uuid"], "main", "codex", "legacy-main",
                capabilities={"plan", "write"},
            )
            self.assertIs(
                _require_controller_acceptance(project, state, binding),
                binding,
            )
            with self.assertRaisesRegex(
                WorkflowError, "CAPABILITY_REQUIRED accept",
            ):
                _require_controller_acceptance(
                    project, {**state, "controller_required": True}, binding,
                )


if __name__ == "__main__":
    unittest.main()
