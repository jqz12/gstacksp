import json
from pathlib import Path
import tempfile
import unittest

from gsp.acceptance_reporting import record_acceptance_card, build_acceptance_summary


class AcceptanceReportingTests(unittest.TestCase):
    def test_task_card_receipt_is_scoped_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            card = {
                "id": "ui-search", "kind": "ui", "required": True, "status": "PASS",
                "steps": ["type 张"], "expected": "rows refresh", "observed": "token=secret-value rows refresh",
                "evidence": ["screenshots/search.png"],
            }
            rel = record_acceptance_card(root, "run-1", "T2", card)
            self.assertTrue(rel.startswith(".gsp/runs/run-1/acceptance/T2/"))
            saved = json.loads((root / rel).read_text(encoding="utf-8"))
            self.assertEqual(saved["task"], "T2")
            self.assertIn("<REDACTED>", saved["observed"])

    def test_shared_all_receipt_does_not_satisfy_task_card(self):
        summary = build_acceptance_summary([
            {"task": "T2", "card": "ui-search", "status": "PASS"},
            {"task": "all", "card": "regression", "status": "PASS"},
        ], required={("T2", "ui-search"), ("T3", "ui-launch")})
        self.assertEqual(summary["missing"], [("T3", "ui-launch")])
        self.assertEqual(summary["shared_regression"], ["regression"])
        self.assertEqual(summary["status"], "pending")

    def test_not_checked_requires_reason_in_summary(self):
        summary = build_acceptance_summary([
            {"task": "T2", "card": "ui-search", "status": "NOT_CHECKED",
             "not_checked_reason": "host unavailable", "next_action": "run on host"},
        ], required={("T2", "ui-search")})
        self.assertEqual(summary["status"], "pending")
        self.assertEqual(summary["not_checked"], ["T2:ui-search"])


if __name__ == "__main__":
    unittest.main()
