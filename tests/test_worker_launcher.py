import unittest
from gsp.worker_launcher import HostAdapterError, build_codex_worker_plan


class WorkerLauncherTests(unittest.TestCase):
    def setUp(self):
        self.host = {
            "providers": ["magpie"],
            "catalog_models": ["opencode-go/qwen4-max"],
            "profiles": ["worker-low-cost"],
            "cli_path": "codex",
            "cli_supports": {
                "exec": True,
                "profile": True,
                "model": True,
                "reasoning_effort": True,
            },
        }

    def test_builds_explicit_profile_model_and_effort_without_starting(self):
        plan = build_codex_worker_plan(
            {"provider": "magpie", "model_profile": "worker-low-cost", "model": "opencode-go/qwen4-max", "reasoning_effort": "low"},
            self.host, "E:/worker", "run assigned task", codex_home="C:/codex-worker")
        self.assertEqual(plan["argv"][:11], ["codex", "exec", "--profile", "worker-low-cost", "--model", "opencode-go/qwen4-max", "-c", 'model_reasoning_effort="low"', "-C", "E:\\worker", "-"])
        self.assertEqual(plan["stdin"], "run assigned task")
        self.assertEqual(plan["env"], {"CODEX_HOME": "C:\\codex-worker"})
        self.assertFalse(plan["started"])

    def test_unsupported_model_is_change_request(self):
        with self.assertRaisesRegex(HostAdapterError, "CHANGE_REQUEST"):
            build_codex_worker_plan({"provider": "magpie", "model": "opencode-go/missing"}, self.host, "E:/worker", "x")

    def test_rejects_a_route_when_the_codex_cli_lacks_the_required_option(self):
        self.host["cli_supports"]["profile"] = False
        with self.assertRaisesRegex(HostAdapterError, "cli_does_not_support_profile"):
            build_codex_worker_plan(
                {"provider": "magpie", "model_profile": "worker-low-cost", "model": "opencode-go/qwen4-max"},
                self.host, "E:/worker", "x")

    def test_does_not_accept_secret_like_route_fields(self):
        plan = build_codex_worker_plan({"provider": "magpie", "model": "opencode-go/qwen4-max", "token": "secret"}, self.host, "E:/worker", "x")
        self.assertNotIn("token", plan["route"])
        self.assertNotIn("secret", str(plan))


if __name__ == "__main__":
    unittest.main()
