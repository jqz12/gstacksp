import json
import unittest
from pathlib import Path

from scripts.routing_registry import ROUTES, validate_generated_catalog, validate_routes


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


if __name__ == "__main__":
    unittest.main()
