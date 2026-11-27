from pathlib import Path
import unittest

from gsp import skill_catalog


ROOT = Path(__file__).resolve().parents[1]


class SkillMethodPreservationTests(unittest.TestCase):
    def test_sp_methods_without_public_entry_keep_complete_bodies(self):
        manifest = skill_catalog.load_manifest(ROOT)
        cases = {
            "test-driven-development": ("## The Iron Law", "## Red-Green-Refactor"),
            "systematic-debugging": ("## The Iron Law", "## Phase 1"),
            "verify": ("## The Iron Law", "# Verification Before Completion"),
        }
        for name, markers in cases.items():
            with self.subTest(method=name):
                text = (ROOT / skill_catalog.entry_for(manifest, name).internal_method_path).read_text(encoding="utf-8")
                for marker in markers:
                    self.assertIn(marker, text)
                self.assertIn("<!-- sp-preserved-body:begin -->", text)
                self.assertFalse((ROOT / "skills" / skill_catalog.full_name(name) / "SKILL.md").exists())

    def test_all_private_methods_carry_explicit_identity_and_owner(self):
        manifest = skill_catalog.load_manifest(ROOT)
        for method in skill_catalog.stage_methods(manifest):
            if method.shared_artifact:
                continue
            with self.subTest(method=method.name):
                text = (ROOT / method.internal_method_path).read_text(encoding="utf-8")
                self.assertIn(f"<!-- internal-method: {method.name} -->", text)
                self.assertIn(f"Owner: `{skill_catalog.full_name(method.stage_membership[0])}`", text)

    def test_generic_upstream_skills_never_become_discoverable_entries(self):
        upstream = ROOT / "skills/gsp-workflow/upstream"
        self.assertEqual(list(upstream.rglob("SKILL.md")), [])
        self.assertTrue(
            (upstream / "skills/test-driven-development/writing-good-tests.md").is_file()
        )


if __name__ == "__main__":
    unittest.main()
