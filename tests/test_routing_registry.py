import json
import unittest
from pathlib import Path

from scripts.routing_registry import ROUTES, route_for, validate_generated_catalog, validate_routes
from gsp.document_health import git_route


ROOT = Path(__file__).resolve().parents[1]


class RoutingRegistryTests(unittest.TestCase):
    def test_registry_is_structurally_valid_against_catalog(self):
        self.assertEqual(validate_generated_catalog(ROOT), [])

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

    def test_runtime_git_route_uses_registry_metadata_for_clean_repo(self):
        result = git_route(ROOT)
        expected = route_for("dirty_or_untracked" if result["changed_paths"] else "clean_and_active_task")
        self.assertEqual(result["route_id"], expected.route_id)
        self.assertEqual(result["blocking"], expected.blocking)
        self.assertEqual(result["next_skill"], expected.next_skill)

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
