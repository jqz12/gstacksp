import unittest

from gsp.acceptance import (
    AcceptanceCard,
    TrackState,
    acceptance_track_status,
    derive_project_status,
    validate_acceptance_card,
)


class AcceptanceSchemaTests(unittest.TestCase):
    def test_valid_card_serializes_stably(self):
        card = AcceptanceCard(
            id="search-refresh",
            kind="ui",
            required=True,
            status="PASS",
            steps=["open page", "type 张"],
            expected="visible rows refresh",
            observed="3 matching rows visible",
            evidence=[".gsp/runs/demo/evidence/ui-1/receipt.json"],
        )
        self.assertEqual(card.to_dict(), {
            "id": "search-refresh",
            "kind": "ui",
            "required": True,
            "status": "PASS",
            "steps": ["open page", "type 张"],
            "expected": "visible rows refresh",
            "observed": "3 matching rows visible",
            "evidence": [".gsp/runs/demo/evidence/ui-1/receipt.json"],
        })
        self.assertEqual(card.to_dict(), AcceptanceCard.from_dict(card.to_dict()).to_dict())

    def test_required_not_checked_needs_reason_and_next_action(self):
        with self.assertRaises(ValueError):
            validate_acceptance_card({
                "id": "desktop-walkthrough", "kind": "ui", "required": True,
                "status": "NOT_CHECKED", "steps": [], "expected": "open", "observed": "",
                "evidence": [],
            })
        card = AcceptanceCard.from_dict({
            "id": "desktop-walkthrough", "kind": "ui", "required": True,
            "status": "NOT_CHECKED", "steps": ["launch"], "expected": "window opens",
            "observed": "native host unavailable", "evidence": [],
            "not_checked_reason": "No desktop automation host", "next_action": "run on Windows host",
        })
        self.assertEqual(card.status, "NOT_CHECKED")
        validate_acceptance_card(card.to_dict())

    def test_unknown_kind_and_status_are_rejected(self):
        base = {"id": "x", "kind": "test", "required": True, "status": "PASS",
                "steps": [], "expected": "ok", "observed": "ok", "evidence": ["test"]}
        bad_kind = dict(base, kind="cargo")
        bad_status = dict(base, status="GREEN")
        with self.assertRaises(ValueError):
            validate_acceptance_card(bad_kind)
        with self.assertRaises(ValueError):
            validate_acceptance_card(bad_status)

    def test_project_status_keeps_implementation_and_acceptance_separate(self):
        tasks = [{
            "implementation": {"status": "implementation_done"},
            "acceptance": {"status": "acceptance_pending", "required_cards": 1, "completed_cards": 0},
        }]
        self.assertEqual(derive_project_status(tasks, final_regression=True, review=True), "implementation_ready")
        tasks[0]["acceptance"] = {"status": "acceptance_verified", "required_cards": 1, "completed_cards": 1}
        self.assertEqual(derive_project_status(tasks, final_regression=False, review=True), "acceptance_ready")
        self.assertEqual(derive_project_status(tasks, final_regression=True, review=True), "ready")

    def test_track_state_rejects_unknown_values(self):
        self.assertEqual(TrackState("implementation_pending").value, "implementation_pending")
        with self.assertRaises(ValueError):
            TrackState("done")

    def test_required_cards_control_acceptance_track(self):
        card = {"id": "behavior", "kind": "test", "required": True,
                "status": "PASS", "steps": [], "expected": "ok", "observed": "ok", "evidence": ["test"]}
        self.assertEqual(acceptance_track_status([card]), "acceptance_verified")
        pending = dict(card, status="NOT_CHECKED", not_checked_reason="host unavailable", next_action="run on host")
        self.assertEqual(acceptance_track_status([pending]), "acceptance_pending")


if __name__ == "__main__":
    unittest.main()
