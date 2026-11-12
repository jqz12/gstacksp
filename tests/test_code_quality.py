import json
from pathlib import Path
import sys
import tempfile
import unittest

from gsp.code_quality import load_stack, run_quality


class CodeQualityTests(unittest.TestCase):
    def make_project(self):
        root = Path(tempfile.mkdtemp(prefix="gsp-quality-"))
        (root / ".gsp").mkdir()
        return root

    def write_stack(self, root, checks):
        (root / ".gsp" / "quality.json").write_text(
            json.dumps({"schema": 1, "checks": checks}), encoding="utf-8"
        )

    def check(self, ident, code, kind="test", required=True, modes=None):
        return {
            "id": ident,
            "label": ident,
            "kind": kind,
            "argv": [sys.executable, "-c", code],
            "cwd": ".",
            "modes": modes or ["change", "baseline", "release"],
            "required": required,
            "timeout_s": 20,
        }

    def test_pass_and_report_are_bounded_and_routed(self):
        root = self.make_project()
        self.write_stack(root, [self.check("unit", "print('ok')")])
        result = run_quality(root, "change", "T1", "run-pass")
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["route"]["next_skill"], "gsp-review")
        self.assertTrue((root / result["report_json"]).is_file())
        self.assertTrue((root / result["report_markdown"]).is_file())
        self.assertTrue((root / ".gsp/runs/T1/quality/run-pass/commands.jsonl").is_file())

    def test_required_failure_routes_back_to_execute(self):
        root = self.make_project()
        self.write_stack(root, [self.check("unit", "raise SystemExit(3)")])
        result = run_quality(root, "release", "T2", "run-fail")
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["route"]["next_skill"], "gsp-execute")
        receipt = root / ".gsp/runs/T2/quality/run-fail/evidence/unit/receipt.json"
        self.assertEqual(json.loads(receipt.read_text())["exit"], 3)

    def test_missing_stack_is_not_checked_and_only_suggests(self):
        root = self.make_project()
        (root / "Cargo.toml").write_text("[package]\nname='fixture'\nversion='0.1.0'\n", encoding="utf-8")
        result = run_quality(root, "baseline", "baseline", "run-missing")
        self.assertEqual(result["status"], "not_checked")
        self.assertEqual(result["route"]["next_skill"], "gsp-workflow")
        self.assertTrue(result["suggestions"])
        self.assertIn("NOT_CHECKED", (root / result["report_markdown"]).read_text(encoding="utf-8"))

    def test_optional_failure_is_warning(self):
        root = self.make_project()
        self.write_stack(root, [self.check("optional", "raise SystemExit(2)", required=False)])
        result = run_quality(root, "change", "T3", "run-warn")
        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["route"]["next_skill"], "gsp-review")

    def test_config_rejects_absolute_cwd(self):
        root = self.make_project()
        self.write_stack(root, [dict(self.check("bad", "pass"), cwd=str(root))])
        with self.assertRaises(Exception):
            load_stack(root)


if __name__ == "__main__":
    unittest.main()
