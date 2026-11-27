import json
import unittest
from pathlib import Path

from scripts.routing_registry import ROUTES, route_for, validate_generated_catalog, validate_routes
from scripts import skill_catalog


ROOT = Path(__file__).resolve().parents[1]


class RoutingRegistryTests(unittest.TestCase):
    def test_registry_is_structurally_valid_against_catalog(self):
        self.assertEqual(validate_generated_catalog(ROOT), [])
        active = {
            skill_catalog.full_name(name)
            for name in (*skill_catalog.DEFAULT_SKILLS, *skill_catalog.ON_DEMAND_SKILLS)
        }
        for route in ROUTES:
            for target in (route.source, route.next_skill, route.return_to, route.unknown_fallback):
                self.assertIn(target, active, route.route_id)

    def test_internalized_routes_target_active_stage_owners(self):
        expected = {
            "execute.test-failure": "gsp-execute",
            "quality.failure": "gsp-execute",
            "quality.change-pass": "gsp-review",
            "review.verify": "gsp-ship",
            "verify.stale-evidence": "gsp-review",
            "restore.unknown": "gsp-workflow",
        }
        actual = {route.route_id: route.next_skill for route in ROUTES if route.route_id in expected}
        self.assertEqual(actual, expected)

    def test_route_ids_are_unique_and_have_fallbacks(self):
        self.assertEqual(len({route.route_id for route in ROUTES}), len(ROUTES))
        self.assertTrue(all(route.unknown_fallback == "gsp-workflow" for route in ROUTES))

    def test_decision_routes_have_decision_kind(self):
        self.assertEqual(validate_routes(), [])
        self.assertTrue(all(route.decision_kind for route in ROUTES if route.user_decision_required))

    def test_reference_documents_route_to_registry(self):
        text = (ROOT / "skills/gsp-workflow/references/routing.md").read_text(encoding="utf-8")
        self.assertIn("scripts/routing_registry.py", text)
        self.assertIn("unknown", text)

    def test_git_routes_resolve_the_internal_git_toolkit_to_ship(self):
        self.assertEqual(route_for("dirty_or_untracked").next_skill, "gsp-ship")
        self.assertEqual(route_for("clean_and_active_task").next_skill, "gsp-workflow")

    def test_detached_route_is_explicitly_blocking_branch_decision(self):
        route = next(route for route in ROUTES if route.route_id == "git.detached")
        self.assertTrue(route.blocking)
        self.assertEqual(route.decision_kind, "branch")

    def test_completion_route_requires_final_regression_before_review(self):
        route = next(route for route in ROUTES if route.route_id == "complete.review")
        self.assertEqual(route.condition, "all_tasks_done_and_regression_passed")
        self.assertEqual(route.next_skill, "gsp-review")

    def test_dirty_and_detached_routes_are_distinct(self):
        conditions = {route.condition for route in ROUTES}
        self.assertIn("dirty_or_untracked", conditions)
        self.assertIn("detached_head", conditions)
        self.assertNotIn("dirty_or_untracked_or_detached", conditions)


if __name__ == "__main__":
    unittest.main()
