import tempfile
import unittest
from pathlib import Path

from gsp.worker_launcher import HostAdapterError, build_codex_worker_plan


class WorkerLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-worker-launcher-")
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.worktree = base / "worker"
        self.worktree.mkdir()
        self.codex_home = base / "codex-home"
        self.codex_home.mkdir()
        self.host = {
            "providers": ["magpie"],
            "catalog_models": ["opencode-go/qwen4-max"],
            "profiles": ["worker-low-cost"],
            "profile_catalogs": {
                "worker-low-cost": {
                    "catalog_models": ["opencode-go/qwen4-max"],
                    "configured_model": "opencode-go/qwen4-max",
                }
            },
            "profile_providers": {"worker-low-cost": ["magpie"]},
            "profile_model_providers": {"worker-low-cost": "magpie"},
            "cli_path": "codex",
            "cli_supports": {
                "exec": True,
                "profile": True,
                "model": True,
                "reasoning_effort": True,
                "config_override": True,
                "workdir": True,
            },
        }

    def test_builds_explicit_provider_profile_model_and_effort_without_starting(self):
        plan = build_codex_worker_plan(
            {
                "provider": "magpie",
                "model_profile": "worker-low-cost",
                "model": "opencode-go/qwen4-max",
                "reasoning_effort": "low",
            },
            self.host,
            self.worktree,
            "run assigned task",
            codex_home=self.codex_home,
        )
        self.assertEqual(
            plan["argv"],
            [
                "codex", "exec", "-c", 'model_provider="magpie"',
                "--profile", "worker-low-cost",
                "--model", "opencode-go/qwen4-max",
                "-c", 'model_reasoning_effort="low"',
                "-C", str(self.worktree.resolve()), "-",
            ],
        )
        self.assertEqual(plan["stdin"], "run assigned task")
        self.assertEqual(plan["env"], {"CODEX_HOME": str(self.codex_home.resolve())})
        self.assertFalse(plan["started"])
        self.assertFalse(plan["request_verified"])

    def test_unsupported_model_is_change_request(self):
        with self.assertRaisesRegex(HostAdapterError, "CHANGE_REQUEST"):
            build_codex_worker_plan(
                {"provider": "magpie", "model": "opencode-go/missing"},
                self.host,
                self.worktree,
                "x",
            )

    def test_profile_provider_mismatch_is_change_request(self):
        self.host["providers"].append("acme")
        with self.assertRaisesRegex(HostAdapterError, "profile_provider_mismatch"):
            build_codex_worker_plan(
                {
                    "provider": "acme",
                    "model_profile": "worker-low-cost",
                    "model": "opencode-go/qwen4-max",
                },
                self.host,
                self.worktree,
                "x",
            )

    def test_rejects_a_route_when_the_codex_cli_lacks_the_required_option(self):
        self.host["cli_supports"]["profile"] = False
        with self.assertRaisesRegex(HostAdapterError, "cli_does_not_support_profile"):
            build_codex_worker_plan(
                {
                    "provider": "magpie",
                    "model_profile": "worker-low-cost",
                    "model": "opencode-go/qwen4-max",
                },
                self.host,
                self.worktree,
                "x",
            )

    def test_rejects_missing_config_override_support_before_launch(self):
        self.host["cli_supports"]["config_override"] = False
        with self.assertRaisesRegex(HostAdapterError, "cli_does_not_support_config_override"):
            build_codex_worker_plan(
                {"provider": "magpie", "model": "opencode-go/qwen4-max"},
                self.host,
                self.worktree,
                "x",
            )

    def test_rejects_missing_cd_support_before_launch(self):
        self.host["cli_supports"]["workdir"] = False
        with self.assertRaisesRegex(HostAdapterError, "cli_does_not_support_workdir"):
            build_codex_worker_plan(
                {"provider": "magpie", "model": "opencode-go/qwen4-max"},
                self.host,
                self.worktree,
                "x",
            )

    def test_does_not_accept_secret_like_route_fields(self):
        plan = build_codex_worker_plan(
            {"provider": "magpie", "model": "opencode-go/qwen4-max", "token": "secret"},
            self.host,
            self.worktree,
            "x",
        )
        self.assertNotIn("token", plan["route"])
        self.assertNotIn("secret", str(plan))


if __name__ == "__main__":
    unittest.main()
