import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gsp.host_adapter import inspect_codex_host, normalize_route, route_health
from gsp.runtime import main


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
                'model_provider = "magpie"\n'
                'model_catalog_json = "models.json"\n'
                '[model_providers.magpie]\n'
                'base_url = "https://secret.invalid"\n'
                'experimental_bearer_token = "secret"\n', encoding="utf-8")
            (home / "models.json").write_text(json.dumps({"models": [{"id": "opencode-go/qwen4-max"}]}), encoding="utf-8")
            (home / "worker-low-cost.config.toml").write_text('[model]\n', encoding="utf-8")
            result = inspect_codex_host(home, cli_path=home / "missing-codex")
            self.assertEqual(result["providers"], ["magpie"])
            self.assertEqual(result["configured_model_provider"], "magpie")
            self.assertEqual(result["catalog_models"], ["opencode-go/qwen4-max"])
            self.assertEqual(result["profiles"], ["worker-low-cost"])
            self.assertNotIn("secret", json.dumps(result))

    def test_catalog_membership_does_not_claim_desktop_visibility(self):
        host = {"providers": ["magpie"], "catalog_models": ["opencode-go/qwen4-max"], "profiles": ["worker-low-cost"]}
        result = route_health({"provider": "magpie", "model": "opencode-go/qwen4-max", "model_profile": "worker-low-cost"}, host)
        self.assertFalse(result["desktop_visible"])
        self.assertFalse(result["request_verified"])
        self.assertEqual(result["status"], "NOT_CHECKED")

    @patch('gsp.host_adapter.subprocess.run')
    def test_inspect_detects_exec_and_reasoning_config_support(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cli = home / 'codex.exe'
            cli.write_bytes(b'')
            (home / 'config.toml').write_text(
                'model = "gpt-6.1-sol"\n'
                'model_reasoning_effort = "max"\n', encoding='utf-8')
            run.return_value = SimpleNamespace(stdout=(
                'Usage: codex [OPTIONS] <COMMAND>\n'
                '  exec  Run Codex non-interactively\n'
                '  -c, --config <key=value>\n'))
            result = inspect_codex_host(home, cli_path=cli)
            self.assertTrue(result['cli_supports']['exec'])
            self.assertTrue(result['cli_supports']['reasoning_effort'])

    def test_route_health_separates_desktop_visibility_from_request_verification(self):
        host = {
            "providers": ["magpie"],
            "catalog_models": ["opencode-go/qwen4-max"],
            "profiles": ["worker-low-cost"],
            "desktop_visible_models": ["opencode-go/qwen4-max"],
        }
        result = route_health({"provider": "magpie", "model": "opencode-go/qwen4-max", "model_profile": "worker-low-cost"}, host)
        host = {"providers": ["magpie"], "catalog_models": ["opencode-go/qwen4-max"], "profiles": ["worker-low-cost"]}
        result = route_health({"provider": "magpie", "model": "opencode-go/qwen4-max", "model_profile": "worker-low-cost"}, host)
        self.assertFalse(result["desktop_visible"])
        self.assertFalse(result["request_verified"])
        self.assertEqual(result["status"], "NOT_CHECKED")

    def test_route_health_separates_desktop_visibility_from_request_verification(self):
        host = {
            "providers": ["magpie"],
            "catalog_models": ["opencode-go/qwen4-max"],
            "profiles": ["worker-low-cost"],
            "desktop_visible_models": ["opencode-go/qwen4-max"],
        }
        result = route_health({"provider": "magpie", "model": "opencode-go/qwen4-max", "model_profile": "worker-low-cost"}, host)
        self.assertTrue(result["configured"])
        self.assertTrue(result["resolvable"])
        self.assertTrue(result["desktop_visible"])
        self.assertFalse(result["request_verified"])
        self.assertEqual(result["status"], "NOT_CHECKED")

    def test_profile_catalog_and_provider_resolve_without_claiming_desktop_visibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.toml").write_text('model = "gpt-6.1-sol"\n', encoding="utf-8")
            (home / "magpie.config.toml").write_text(
                'model = "opencode-go/qwen4-max"\n'
                'model_provider = "magpie"\n'
                'model_catalog_json = "magpie-models.json"\n'
                '[model_providers.magpie]\n'
                'base_url = "http://127.0.0.1:3425/v1"\n', encoding="utf-8")
            (home / "magpie-models.json").write_text(
                json.dumps({"models": [{"slug": "opencode-go/qwen4-max"}]}), encoding="utf-8")
            host = inspect_codex_host(home, cli_path=home / "missing-codex")
            self.assertEqual(host["catalog_models"], [])
            self.assertEqual(host["profile_catalogs"]["magpie"]["catalog_models"], ["opencode-go/qwen4-max"])
            self.assertEqual(host["providers"], [])
            self.assertEqual(host["profile_providers"]["magpie"], ["magpie"])
            self.assertNotIn("127.0.0.1", json.dumps(host))
            result = route_health(
                {"provider": "magpie", "model_profile": "magpie", "model": "opencode-go/qwen4-max"},
                host,
            )
            self.assertTrue(result["configured"])
            self.assertTrue(result["resolvable"])
            self.assertFalse(result["desktop_visible"])
            self.assertNotIn("model_not_in_catalog", result["reasons"])

    def test_inherited_provider_still_requires_a_resolvable_model(self):
        result = route_health(
            {"provider": "inherit", "model": "definitely/missing"},
            {
                "providers": [],
                "catalog_models": ["real/model"],
                "configured_model": "real/model",
                "profiles": [],
            },
        )
        self.assertFalse(result["resolvable"])
        self.assertIn("model_not_in_catalog", result["reasons"])

    def test_custom_configured_provider_is_not_rejected_by_a_static_allowlist(self):
        route = normalize_route({"provider": "acme", "model": "acme/fast"})
        result = route_health(
            route,
            {
                "providers": ["acme"],
                "catalog_models": ["acme/fast"],
                "profiles": [],
            },
        )
        self.assertTrue(result["configured"])
        self.assertTrue(result["resolvable"])

    def test_config_comments_do_not_hide_safe_route_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.toml").write_text(
                'model = "acme/fast" # selected model\n'
                'model_provider = "acme" # selected provider\n'
                'model_catalog_json = "models.json" # catalog file\n'
                '[model_providers."acme"] # custom provider\n'
                'base_url = "https://secret.invalid"\n'
                'experimental_bearer_token = "secret"\n',
                encoding="utf-8",
            )
            (home / "models.json").write_text(
                json.dumps({"models": [{"id": "acme/fast"}]}),
                encoding="utf-8",
            )
            host = inspect_codex_host(home, cli_path=home / "missing-codex")
            self.assertEqual(host["configured_model"], "acme/fast")
            self.assertEqual(host["configured_model_provider"], "acme")
            self.assertEqual(host["providers"], ["acme"])
            self.assertEqual(host["catalog_models"], ["acme/fast"])
            self.assertNotIn("secret", json.dumps(host))

    def test_route_health_cli_is_read_only_and_keeps_live_states_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.toml").write_text(
                'model = "acme/fast"\n'
                'model_provider = "acme"\n'
                'model_catalog_json = "models.json"\n'
                '[model_providers.acme]\n'
                'base_url = "https://secret.invalid"\n',
                encoding="utf-8",
            )
            (home / "models.json").write_text(
                json.dumps({"models": [{"id": "acme/fast"}]}),
                encoding="utf-8",
            )
            (home / ".gsp").mkdir()
            (home / ".gsp" / "project.json").write_text(
                json.dumps({"schema": 1, "id": "read-only-test", "root": str(home.resolve())}),
                encoding="utf-8",
            )
            before = {
                item.relative_to(home).as_posix(): item.read_bytes()
                for item in home.rglob("*")
                if item.is_file()
            }
            for command in ("model-route-health", "host-adapter-status"):
                stdout = io.StringIO()
                stderr = io.StringIO()
                argv = [
                    "--project", str(home), command,
                    "--codex-home", str(home),
                    "--cli-path", str(home / "missing-codex"),
                    "--provider", "acme",
                    "--model", "acme/fast",
                ]
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(argv)
                self.assertEqual(code, 0, stderr.getvalue())
                result = json.loads(stdout.getvalue())
                self.assertTrue(result["health"]["configured"])
                self.assertTrue(result["health"]["resolvable"])
                self.assertFalse(result["health"]["desktop_visible"])
                self.assertFalse(result["health"]["request_verified"])
                self.assertEqual(result["health"]["status"], "NOT_CHECKED")
                self.assertNotIn("secret", stdout.getvalue())
            after = {
                item.relative_to(home).as_posix(): item.read_bytes()
                for item in home.rglob("*")
                if item.is_file()
            }
            self.assertEqual(after, before)

    def test_route_health_reports_missing_model(self):
        result = route_health({"provider": "magpie", "model": "opencode-go/missing"}, {"providers": ["magpie"], "catalog_models": [], "profiles": []})
        self.assertFalse(result["resolvable"])
        self.assertIn("model_not_in_catalog", result["reasons"])

    def test_route_health_reports_missing_provider(self):
        result = route_health({"provider": "magpie", "model": "opencode-go/qwen4-max"}, {"providers": [], "catalog_models": ["opencode-go/qwen4-max"], "profiles": []})
        self.assertFalse(result["configured"])
        self.assertTrue(result["resolvable"])
        self.assertIn("provider_not_configured", result["reasons"])


if __name__ == "__main__":
    unittest.main()
