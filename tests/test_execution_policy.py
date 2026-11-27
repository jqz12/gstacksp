"""Pure convergence-policy tests for slices, findings and stop points."""

from datetime import datetime, timedelta, timezone
import unittest

from gsp.orchestration_types import IssueRef
from gsp.execution_policy import (
    checkpoint_decision,
    classify_review_finding,
    execution_decision,
    validate_task_slices,
)


NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
CONTRACT = "a" * 64
REVIEWED = "1" * 64
CURRENT = "1" * 64


class SlicePolicyTests(unittest.TestCase):
    def test_multiple_outcomes_require_slices(self):
        result = validate_task_slices({
            "id": "T01",
            "multiple_independent_outcomes": ["billing", "audit"],
        })
        self.assertEqual(result["status"], "needs_slices")
        self.assertFalse(result["complete"])

    def test_dependency_order_is_derived_without_loc_input(self):
        result = validate_task_slices({
            "id": "T01",
            "requires_slices": True,
            "slices": [
                {"id": "S2", "outcome": "deploy", "checks": ["smoke"], "deps": ["S1"]},
                {"id": "S1", "outcome": "build", "checks": ["unit"], "deps": []},
            ],
        })
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["order"], ["S1", "S2"])
        self.assertFalse(result["complete"])

    def test_duplicate_and_cycle_are_rejected(self):
        duplicate = validate_task_slices({
            "id": "T01",
            "requires_slices": True,
            "slices": [
                {"id": "S1", "outcome": "one", "checks": ["a"], "deps": []},
                {"id": "S1", "outcome": "two", "checks": ["b"], "deps": []},
            ],
        })
        cycle = validate_task_slices({
            "id": "T02",
            "requires_slices": True,
            "slices": [
                {"id": "S1", "outcome": "one", "checks": ["a"], "deps": ["S2"]},
                {"id": "S2", "outcome": "two", "checks": ["b"], "deps": ["S1"]},
            ],
        })
        self.assertEqual(duplicate["status"], "invalid")
        self.assertIn("duplicate_slice_id", [item["code"] for item in duplicate["errors"]])
        self.assertEqual(cycle["status"], "invalid")
        self.assertIn("slice_dependency_cycle", [item["code"] for item in cycle["errors"]])

    def test_legacy_unknown_is_triage_and_done_is_not_accepted(self):
        legacy = validate_task_slices({"id": "T03"})
        done = validate_task_slices({
            "id": "T04",
            "requires_slices": True,
            "slices": [
                {"id": "S1", "outcome": "one", "checks": ["a"], "deps": [], "status": "done"},
                {"id": "S2", "outcome": "two", "checks": ["b"], "deps": ["S1"], "status": "accepted"},
            ],
        })
        self.assertEqual(legacy["status"], "needs_triage")
        self.assertFalse(legacy["complete"])
        self.assertTrue(done["structure_ok"])
        self.assertFalse(done["complete"])


class FindingPolicyTests(unittest.TestCase):
    def test_introduced_critical_findings_block_even_outside_contract(self):
        for kind in ("introduced_regression", "permission_bypass", "data_corruption",
                     "current_prerequisite_failure"):
            with self.subTest(kind=kind):
                result = classify_review_finding({
                    "kind": kind, "confirmed": True,
                    "contract_digest": "b" * 64,
                    "reviewed_candidate_digest": REVIEWED,
                    "current_candidate_digest": CURRENT,
                    "trigger": "reproducer",
                }, CONTRACT)
                self.assertEqual(result["classification"], "blocking_current_acceptance")
                self.assertTrue(result["blocking_current_acceptance"])

    def test_independent_defect_needs_concrete_owner(self):
        base = {
            "kind": "correctness_defect", "confirmed": True,
            "independent_of_current_acceptance": True,
            "task_uid": "T09", "trigger": "confirmed edge case",
            "reviewed_candidate_digest": REVIEWED,
            "current_candidate_digest": CURRENT,
        }
        missing = classify_review_finding(base, CONTRACT)
        owned = classify_review_finding({**base, "follow_up_owner": "T12"}, CONTRACT)
        self.assertEqual(missing["classification"], "correctness_defect")
        self.assertFalse(missing["accepted"])
        self.assertTrue(missing["owner_required"])
        self.assertTrue(owned["accepted"])
        self.assertFalse(owned["blocking_current_acceptance"])

    def test_unknown_and_candidate_drift_cannot_be_accepted(self):
        unknown = classify_review_finding({"kind": "unknown", "confirmed": False}, CONTRACT)
        drift = classify_review_finding({
            "kind": "correctness_defect", "confirmed": True,
            "reviewed_candidate_digest": "1" * 64,
            "current_candidate_digest": "2" * 64,
        }, CONTRACT)
        self.assertEqual(unknown["status"], "needs_investigation")
        self.assertFalse(unknown["accepted"])
        self.assertEqual(drift["classification"], "blocking_current_acceptance")

    def test_formal_finding_requires_both_candidate_digests(self):
        base = {
            "kind": "correctness_defect",
            "confirmed": True,
            "independent_of_current_acceptance": True,
            "follow_up_owner": "T12",
        }
        for reviewed, current in (
            (None, CURRENT),
            (REVIEWED, None),
            ("not-a-digest", CURRENT),
            (REVIEWED, "1" * 63),
        ):
            with self.subTest(reviewed=reviewed, current=current):
                finding = dict(base)
                if reviewed is not None:
                    finding["reviewed_candidate_digest"] = reviewed
                if current is not None:
                    finding["current_candidate_digest"] = current
                result = classify_review_finding(finding, CONTRACT)
                self.assertEqual(result["status"], "needs_investigation")
                self.assertFalse(result["accepted"])
                self.assertTrue(result["blocking_current_acceptance"])

    def test_issue_identity_is_stable_and_second_repair_goes_to_sol(self):
        first = classify_review_finding({
            "kind": "introduced_regression", "confirmed": True,
            "work_uid": "W1", "task_uid": "T1",
            "acceptance_key": "wrong actor cannot write",
            "root_cause_key": "actor guard omitted",
            "signature": "path:line", "repair_count": 0,
            "reviewed_candidate_digest": REVIEWED,
            "current_candidate_digest": CURRENT,
        }, CONTRACT)
        second = classify_review_finding({
            "kind": "introduced_regression", "confirmed": True,
            "work_uid": "W1", "task_uid": "T1",
            "acceptance_key": "wrong actor cannot write",
            "root_cause_key": "actor guard omitted",
            "signature": "path:line", "repair_count": 1,
            "reviewed_candidate_digest": REVIEWED,
            "current_candidate_digest": CURRENT,
        }, CONTRACT)
        self.assertIsInstance(first["issue_ref"], IssueRef)
        self.assertEqual(first["issue_ref"].uid, second["issue_ref"].uid)
        self.assertEqual(second["issue_ref"].owner, "sol")
        self.assertTrue(second["requires_sol_takeover"])


class CheckpointPolicyTests(unittest.TestCase):
    def test_45_and_90_minute_boundaries(self):
        task = {
            "id": "T1", "original_started_at": NOW.isoformat(),
            "last_checkpoint_at": NOW.isoformat(),
        }
        at_44 = checkpoint_decision(task, NOW + timedelta(minutes=44))
        at_45 = checkpoint_decision(task, NOW + timedelta(minutes=45))
        at_90 = checkpoint_decision(task, NOW + timedelta(minutes=90))
        self.assertFalse(at_44["checkpoint_due"])
        self.assertTrue(at_45["checkpoint_due"])
        self.assertFalse(at_45["sol_decision_required"])
        self.assertTrue(at_90["sol_decision_required"])

    def test_retry_fields_do_not_reset_clock_and_acceptance_resets_progress_clock(self):
        task = {
            "id": "T2", "original_started_at": NOW.isoformat(),
            "started_at": (NOW + timedelta(minutes=80)).isoformat(),
            "attempt_started_at": (NOW + timedelta(minutes=89)).isoformat(),
            "contract_started_at": (NOW + timedelta(minutes=89)).isoformat(),
            "slices": [
                {"id": "S1", "status": "accepted",
                 "accepted_at": (NOW + timedelta(minutes=60)).isoformat()},
            ],
        }
        result = checkpoint_decision(task, NOW + timedelta(minutes=90))
        self.assertEqual(result["anchor"], "accepted_slice")
        self.assertEqual(result["last_accepted_at"], (NOW + timedelta(minutes=60)).isoformat())
        self.assertFalse(result["sol_decision_required"])


class ExecutionDecisionTests(unittest.TestCase):
    def test_future_acceptance_cannot_suppress_a_stop(self):
        from gsp.execution_policy import checkpoint_decision
        result = checkpoint_decision({'original_started_at': NOW.isoformat(),
            'last_accepted_at': (NOW + timedelta(days=1)).isoformat()}, NOW + timedelta(minutes=90))
        self.assertEqual(result['status'], 'needs_triage')

    def test_single_boundary_does_not_force_a_multi_slice_session(self):
        result = execution_decision({'estimated_minutes': 5, 'local_change': True,
                                     'integration_boundaries': ['one module']})
        self.assertEqual(result['mode'], 'local')

    def test_existing_issue_repair_count_survives_new_finding_envelope(self):
        from gsp.execution_policy import classify_review_finding
        result = classify_review_finding({
            'confirmed': True, 'kind': 'regression',
            'issue_ref': {'uid': 'stable-issue', 'repair_count': 1},
            'reviewed_candidate_digest': REVIEWED,
            'current_candidate_digest': CURRENT,
        }, 'next-contract')
        self.assertTrue(result['requires_sol_takeover'])
        self.assertEqual(result['issue_ref'].uid, 'stable-issue')

    def test_small_local_task_uses_no_new_session_or_tree(self):
        result = execution_decision({
            "id": "T1", "estimated_minutes": 15,
            "local_change": True, "independent": False,
        })
        self.assertEqual(result["mode"], "local")
        self.assertFalse(result["requires_session"])
        self.assertFalse(result["requires_worktree"])
        self.assertEqual(result["slot_limit"], 1)

    def test_complex_independent_task_uses_deepseek_max_and_three_slots(self):
        result = execution_decision({
            "id": "T2", "estimated_minutes": 16,
            "independent": True, "parallel_writable": True,
        })
        self.assertEqual(result["mode"], "deepseek_worker")
        self.assertEqual(result["model_alias"], "group/deepseek-2")
        self.assertEqual(result["effort"], "max")
        self.assertEqual(result["slot_limit"], 3)
        self.assertTrue(result["requires_session"])
        self.assertTrue(result["requires_worktree"])

    def test_unknown_execution_shape_is_not_dispatched(self):
        result = execution_decision({"id": "T3"})
        self.assertEqual(result["mode"], "needs_triage")
        self.assertFalse(result["requires_session"])


if __name__ == "__main__":
    unittest.main()
