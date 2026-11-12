import unittest

from gsp.acceptance import acceptance_track_status, derive_project_status
from gsp.acceptance_migration import migrate_state
from gsp.project_adapter import load_adapter
from gsp.acceptance_reporting import build_acceptance_summary


class AcceptanceIntegrationTests(unittest.TestCase):
    def card(self, status="PASS", kind="test"):
        row = {"id": "behavior", "kind": kind, "required": True, "status": status,
               "steps": ["exercise"], "expected": "works", "observed": "works", "evidence": ["test"]}
        if status == "NOT_CHECKED":
            row.update(not_checked_reason="host unavailable", next_action="run on host")
        return row

    def test_implementation_only_stays_implementation_ready(self):
        tasks = [{"implementation": {"status": "implementation_done"},
                  "acceptance": {"status": "acceptance_pending"}}]
        self.assertEqual(derive_project_status(tasks, final_regression=True, review=True), "implementation_ready")

    def test_ui_acceptance_and_no_ui_are_independent(self):
        self.assertEqual(acceptance_track_status([self.card(kind="ui")]), "acceptance_verified")
        self.assertEqual(load_adapter(__import__('pathlib').Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "acceptance"),
                         {"status": "not_checked", "reason": "adapter not declared"})

    def test_blocked_and_interrupted_paths_do_not_verify(self):
        self.assertEqual(acceptance_track_status([self.card(status="BLOCKED")]), "acceptance_pending")
        migrated = migrate_state({"id": "interrupted", "tasks": [{"id": "T1", "status": "done"}], "inflight": {"id": "x"}})
        self.assertFalse(migrated["migration"]["acceptance_inferred"])
        summary = build_acceptance_summary([], required={("T1", "behavior")})
        self.assertEqual(summary["status"], "pending")


if __name__ == "__main__":
    unittest.main()
