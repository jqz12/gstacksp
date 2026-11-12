import unittest
from pathlib import Path
import tempfile
import json

from gsp.project_adapter import load_adapter, validate_adapter


class ProjectAdapterTests(unittest.TestCase):
    def test_supported_language_declarations_validate(self):
        for language, test_cmd, build_cmd in (
            ("rust", ["cargo", "test"], ["cargo", "build"]),
            ("python", ["pytest"], ["python", "-m", "build"]),
            ("node", ["npm", "test"], ["npm", "run", "build"]),
            ("go", ["go", "test", "./..."], ["go", "build", "./..."]),
            ("java", ["mvn", "test"], ["mvn", "package"]),
        ):
            value = {"language": language, "commands": {"test": test_cmd, "build": build_cmd},
                     "ui": {"type": "none"}, "parser": language}
            validate_adapter(value)

    def test_no_ui_project_loads_without_ui_requirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".gsp").mkdir()
            (root / ".gsp/adapter.json").write_text(json.dumps({
                "language": "python", "commands": {"test": ["pytest"]},
                "ui": {"type": "none"}, "parser": "pytest",
            }), encoding="utf-8")
            result = load_adapter(root)
            self.assertEqual(result["ui"]["type"], "none")
            self.assertEqual(result["status"], "declared")

    def test_missing_adapter_is_not_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = load_adapter(Path(tmp))
            self.assertEqual(result, {"status": "not_checked", "reason": "adapter not declared"})

    def test_unknown_fields_and_ui_types_are_rejected(self):
        base = {"language": "python", "commands": {"test": ["pytest"]},
                "ui": {"type": "none"}, "parser": "pytest"}
        with self.assertRaises(ValueError):
            validate_adapter(dict(base, extra=True))
        with self.assertRaises(ValueError):
            validate_adapter(dict(base, ui={"type": "mobile"}))


if __name__ == "__main__":
    unittest.main()
