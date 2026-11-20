import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gsp.host_adapter import inspect_codex_host, normalize_route, route_health


class HostAdapterTests(unittest.TestCase):
    def test_normalize_route_defaults_provider_without_secret_fields(self):
        route = normalize_route({"model": "opencode-go/qwen4-max", "api_key": "secret"})
        self.assertEqual(route["provider"], "inherit")
        self.assertEqual(route["model"], "opencode-go/qwen4-max")
        self.assertNotIn("api_key", route)

    def test_inspect_reads_catalog_and_provider_names_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.toml").write_text(
                'model = "opencode-go/qwen4-max"\n'
                'model_catalog_json = "models.json"\n'
                '[model_providers.magpie]\n'
                'base_url = "https://secret.invalid"\n'
                'experimental_bearer_token = "secret"\n', encoding="utf-8")
            (home / "models.json").write_text(json.dumps({"models": [{"id": "opencode-go/qwen4-max"}]}), encoding="utf-8")
            (home / "worker-low-cost.config.toml").write_text('[model]\n', encoding="utf-8")
            result = inspect_codex_host(home, cli_path=home / "missing-codex")
            self.assertEqual(result["providers"], ["magpie"])
            self.assertEqual(result["catalog_models"], ["opencode-go/qwen4-max"])
            self.assertEqual(result["profiles"], ["worker-low-cost"])
            self.assertNotIn("secret", json.dumps(result))

    def test_route_health_separates_desktop_visibility_from_request_verification(self):
        host = {"providers": ["magpie"], "catalog_models": ["opencode-go/qwen4-max"], "profiles": ["worker-low-cost"]}
        result = route_health({"provider": "magpie", "model": "opencode-go/qwen4-max", "model_profile": "worker-low-cost"}, host)
        self.assertTrue(result["configured"])
        self.assertTrue(result["resolvable"])
        self.assertTrue(result["desktop_visible"])
        self.assertFalse(result["request_verified"])
        self.assertEqual(result["status"], "NOT_CHECKED")

    def test_route_health_reports_missing_model(self):
        result = route_health({"provider": "magpie", "model": "opencode-go/missing"}, {"providers": ["magpie"], "catalog_models": [], "profiles": []})
        self.assertFalse(result["resolvable"])
        self.assertIn("model_not_in_catalog", result["reasons"])


if __name__ == "__main__":
    unittest.main()
