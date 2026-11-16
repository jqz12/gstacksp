import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "gsp.py"


class DualTrackRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-dual-track-")
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, ok=True):
        result = subprocess.run(
            [sys.executable, str(CLI), "--project", str(self.project), *args],
            text=True, encoding="utf-8", capture_output=True,
        )
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def prepare_task(self):
        self.cli("init")
        self.cli("new", "login", "--title", "Login")
        state_file = self.project / ".gsp/runs/login/state.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        design = self.project / state["documents"]["design"]
        design.write_text("# Design\nLogin returns a session.\n", encoding="utf-8")
        self.cli("approve", "login", "--quote", "approve fixture design")
        self.cli("task-add", "login", "T1", "--title", "Implement login", "--acceptance", "Unit tests pass")
        plan = self.project / state["documents"]["plan"]
        plan.write_text("# Plan\nT1: implement and test login.\n", encoding="utf-8")
        self.cli("seal-plan", "login")
        self.cli("scope", "login", "--off", "--quote", "no freeze fixture")
        self.cli("advance", "login", "--to", "execute")
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["tasks"][0]["acceptance_cards"] = [{
            "id": "behavior", "kind": "test", "required": True, "status": "NOT_CHECKED",
            "steps": ["run behavior"], "expected": "behavior works", "observed": "not run",
            "evidence": ["fixture-test"], "not_checked_reason": "fixture setup", "next_action": "record acceptance",
        }]
        state_file.write_text(json.dumps(state), encoding="utf-8")
        self.cli("task-start", "login", "T1")
        return state_file

    def test_task_done_does_not_close_acceptance_track(self):
        state_file = self.prepare_task()
        self.cli("run", "login", "--task", "T1", "--kind", "tests", "--", sys.executable, "-c", "print('Ran 1 test')")
        self.cli("task-done", "login", "T1")
        self.cli("run", "login", "--task", "all", "--kind", "tests", "--", sys.executable, "-c", "print('Ran 1 test')")
        state = json.loads(state_file.read_text(encoding="utf-8"))
        docs = self.project / state["doc_dir"] / "review.md"
        docs.write_text("# Review\nDual track fixture reviewed.\n", encoding="utf-8")
        self.cli("advance", "login", "--to", "review")
        self.cli("review", "login", "--reviewer", "self", "--blocking", "0")
        self.cli("advance", "login", "--to", "checking")
        blocked = self.cli("advance", "login", "--to", "ready", ok=False)
        self.assertIn("ACCEPTANCE_PENDING", blocked.stdout)
        self.cli("ready", "login", ok=False)
        state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["tasks"][0]["implementation"]["status"], "implementation_done")
        self.assertEqual(state["tasks"][0]["acceptance_track"]["status"], "acceptance_pending")
        state["tasks"][0]["acceptance_cards"][0].update(status="PASS", observed="accepted", evidence=["fixture-test"])
        state_file.write_text(json.dumps(state), encoding="utf-8")
        self.cli("advance", "login", "--to", "ready")
        self.cli("ready", "login")


if __name__ == "__main__":
    unittest.main()
