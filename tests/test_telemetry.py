import json
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest

from gsp.telemetry import append_event, build_report, read_events, telemetry_path


class TelemetryTests(unittest.TestCase):
    def root(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / ".gsp").mkdir()
        (root / "docs/gsp").mkdir(parents=True)
        return root

    def test_append_redacts_sensitive_fields_and_keeps_route_summary(self):
        root = self.root()
        event = append_event(root, {
            "event": "health",
            "skill": "gsp-health",
            "observation_status": "observed",
            "source": "gsp-cli",
            "prompt": "do not persist",
            "observed": {"next_skill": "gsp-git", "status": "needs-decision"},
            "secret": "abc123",
        })
        self.assertEqual(event["observed"]["next_skill"], "gsp-git")
        self.assertEqual(event["observation_status"], "observed")
        self.assertEqual(event["source"], "gsp-cli")
        raw = telemetry_path(root).read_text(encoding="utf-8")
        self.assertNotIn("do not persist", raw)
        self.assertNotIn("abc123", raw)
        self.assertEqual(len(read_events(root)[0]), 1)

    def test_report_is_historical_and_marks_unknown_and_deviation(self):
        root = self.root()
        append_event(root, {"event": "skill_declared", "skill": "gsp-health", "status": "unknown", "observed": None})
        append_event(root, {"event": "route", "skill": "gsp-health", "status": "pass", "observed": {"next_skill": "gsp-workflow"}, "deviation": "expected gsp-git"})
        report = build_report(root, "sample-run")
        self.assertEqual(report["event_count"], 2)
        self.assertTrue(report["improvement_candidates"])
        self.assertTrue((root / "docs/gsp/reports/observability/sample-run/report.json").is_file())
        self.assertTrue((root / "docs/gsp/reports/observability/sample-run/report.md").is_file())
        self.assertIn("证据入口", (root / "docs/gsp/reports/observability/sample-run/report.md").read_text(encoding="utf-8"))

    def test_malformed_lines_are_isolated(self):
        root = self.root()
        path = telemetry_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"event":"ok"}\nnot-json\n', encoding="utf-8")
        events, malformed = read_events(root)
        self.assertEqual(len(events), 1)
        self.assertEqual(malformed, ["2"])

    def test_write_failure_is_non_fatal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "missing-parent"
            # A normal append creates its own directory and remains safe.
            self.assertIsNotNone(append_event(root, {"event": "test", "status": "pass"}))

    def test_cli_lifecycle_and_observe_report_are_recorded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            command = [sys.executable, str(Path(__file__).parents[1] / "scripts/gsp.py"), "--project", str(root)]
            init = subprocess.run(command + ["init"], capture_output=True, text=True)
            self.assertEqual(init.returncode, 0, init.stderr)
            report = subprocess.run(command + ["observe", "--run-id", "cli-test"], capture_output=True, text=True)
            self.assertEqual(report.returncode, 0, report.stderr)
            events, malformed = read_events(root)
            self.assertEqual(malformed, [])
            self.assertTrue(any(event["event"] == "init" for event in events))
            self.assertTrue((root / "docs/gsp/reports/observability/cli-test/report.md").is_file())


if __name__ == "__main__":
    unittest.main()
