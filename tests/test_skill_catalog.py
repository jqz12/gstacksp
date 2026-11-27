import copy
import json
from pathlib import Path
import unittest

from gsp import skill_catalog


ROOT = Path(__file__).resolve().parents[1]
CONTEXT_PATH = "skills/gsp-workflow/references/contract.md"


class SkillCatalogTests(unittest.TestCase):
    EXPECTED_DEFAULT = {
        "workflow",
        "brainstorming",
        "writing-plans",
        "execute",
        "review",
        "ship",
    }
    EXPECTED_ON_DEMAND = {
        "health",
        "document-governance",
        "writing-skills",
        "diagnosing-superpowers",
    }
    EXPECTED_DELETED = {
        "learn",
        "eval-plan",
        "diagram",
        "make-pdf",
        "test-audit",
        "cso",
        "captain",
        "setup-gbrain",
        "sync-gbrain",
        "codex",
        "benchmark-models",
        "freeze",
    }
    EXPECTED_INTERNAL = {
        "design-consultation",
        "design-shotgun",
        "autoplan",
        "plan-tune",
        "plan-design-review",
        "plan-devex-review",
        "test-driven-development",
        "systematic-debugging",
        "code-quality",
        "browse",
        "qa",
        "open-gstack-browser",
        "setup-browser-cookies",
        "benchmark",
        "document-generate",
        "verify",
        "design-review",
        "devex-review",
        "git",
        "land-and-deploy",
        "setup-deploy",
        "landing-report",
        "canary",
        "modernize",
        "context-save",
        "context-restore",
        "scrape",
        "skillify",
    }

    def test_manifest_is_an_exact_disjoint_12_6_4_28_partition(self):
        manifest = skill_catalog.load_manifest(ROOT)
        self.assertEqual(
            {
                skill_catalog.short_name(name)
                for name in skill_catalog.names_for_class(manifest, "default")
            },
            self.EXPECTED_DEFAULT,
        )
        self.assertEqual(
            {
                skill_catalog.short_name(name)
                for name in skill_catalog.names_for_class(manifest, "on_demand")
            },
            self.EXPECTED_ON_DEMAND,
        )
        self.assertEqual(
            {
                skill_catalog.short_name(name)
                for name in skill_catalog.names_for_class(manifest, "retired")
            },
            self.EXPECTED_DELETED,
        )
        self.assertEqual(
            {
                skill_catalog.short_name(name)
                for name in skill_catalog.names_for_class(manifest, "internal")
            },
            self.EXPECTED_INTERNAL,
        )
        groups = (
            set(skill_catalog.names_for_class(manifest, "default")),
            set(skill_catalog.names_for_class(manifest, "on_demand")),
            set(skill_catalog.names_for_class(manifest, "retired")),
            set(skill_catalog.names_for_class(manifest, "internal")),
        )
        self.assertEqual(sum(map(len, groups)), 50)
        self.assertEqual(len(set.union(*groups)), 50)
        self.assertEqual(
            set(skill_catalog.names_for_class(manifest)),
            set.union(*groups),
        )

    def test_internal_sources_map_to_26_methods_and_one_shared_resource(self):
        manifest = skill_catalog.load_manifest(ROOT)
        internal = skill_catalog.stage_methods(manifest)
        self.assertEqual(len(internal), 28)
        paths = [entry.internal_method_path for entry in internal]
        self.assertEqual(len(skill_catalog.physical_resource_paths(manifest, methods_only=True)), 26)
        self.assertEqual(len(set(paths)), 27)
        self.assertEqual(
            [entry.name for entry in internal if entry.shared_artifact],
            ["gsp-context-save", "gsp-context-restore"],
        )

    def test_context_sources_share_one_digest_without_sharing_provenance(self):
        manifest = skill_catalog.load_manifest(ROOT)
        contexts = [
            entry for entry in skill_catalog.stage_methods(manifest)
            if entry.name in ("gsp-context-save", "gsp-context-restore")
        ]
        self.assertEqual({entry.internal_method_path for entry in contexts}, {CONTEXT_PATH})
        self.assertEqual({entry.resource_digest for entry in contexts}, {contexts[0].resource_digest})
        self.assertNotEqual(contexts[0].source_digest, contexts[1].source_digest)
        self.assertNotEqual(contexts[0].source_provenance, contexts[1].source_provenance)
        self.assertNotEqual(contexts[0].source_digest, contexts[0].resource_digest)

    def test_script_provider_is_only_a_compatibility_forwarder(self):
        from scripts import skill_catalog as compatibility

        self.assertIs(compatibility.provider, skill_catalog)
        self.assertEqual(compatibility.MANIFEST_REL, skill_catalog.MANIFEST_REL)

    def test_source_definitions_cover_manifest_active_and_internal_methods(self):
        from scripts import build_skills

        manifest = skill_catalog.load_manifest(ROOT)
        names = {row[0] for row in build_skills.DEFINITIONS} | {"workflow"}
        expected = {
            skill_catalog.short_name(entry["original_name"])
            for entry in manifest["entries"]
            if entry["class"] in ("default", "on_demand", "internal")
        }
        self.assertEqual(names & expected, expected)

    def test_validation_rejects_context_path_drift(self):
        manifest = skill_catalog.load_manifest(ROOT)
        changed = copy.deepcopy(manifest)
        context = next(
            entry for entry in changed["entries"]
            if entry["original_name"] == "gsp-context-save"
        )
        context["internal_method_path"] = "skills/gsp-workflow/methods/gsp-context-save/METHOD.md"
        errors = skill_catalog.validate_manifest(changed)
        self.assertTrue(
            any("context" in error and "contract.md" in error for error in errors),
            errors,
        )

    def test_validation_rejects_duplicate_source_identity_and_invalid_counts(self):
        manifest = skill_catalog.load_manifest(ROOT)
        changed = copy.deepcopy(manifest)
        changed["entries"][1]["original_name"] = changed["entries"][0]["original_name"]
        changed["entries"].pop()
        errors = skill_catalog.validate_manifest(changed)
        self.assertTrue(any("unique" in error for error in errors), errors)
        self.assertTrue(any("50" in error for error in errors), errors)

    def test_manifest_records_all_100_original_input_paths(self):
        manifest = skill_catalog.load_manifest(ROOT)
        paths = [
            source["path"]
            for entry in manifest["entries"]
            for source in entry["source_files"]
        ]
        self.assertEqual(len(paths), 100)
        self.assertEqual(len(paths), len(set(paths)))
        expected = {
            f"skills/{entry['original_name']}/{suffix}"
            for entry in manifest["entries"]
            for suffix in ("SKILL.md", "agents/openai.yaml")
        }
        self.assertEqual(set(paths), expected)

    def test_public_docs_distinguish_source_duties_methods_and_context_resource(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        for phrase in (
            "6 个默认入口和 4 个按需入口",
            "28 项内部来源职责",
            "26 个独立 `METHOD.md`",
            "1 份共享的 workflow 业务恢复说明",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_public_docs_list_the_exact_public_entries(self):
        manifest = skill_catalog.load_manifest(ROOT)
        public = skill_catalog.public_entries(manifest)
        for relative in (
            "README.md",
            "docs/技能目录.md",
            "docs/安装与使用.md",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            for entry in public:
                with self.subTest(document=relative, entry=entry.name):
                    self.assertIn(f"`{entry.name}`", text)
            for retired in (
                "gsp-context-save",
                "gsp-context-restore",
                "gsp-freeze",
                "gsp-learn",
            ):
                with self.subTest(document=relative, retired=retired):
                    self.assertNotIn(f"| {retired} |", text)

    def test_retired_learnings_reference_cannot_reenter_generation_or_package(self):
        reference = ROOT / "skills/gsp-workflow/references/learnings.md"
        self.assertFalse(reference.exists())
        files = {
            path.relative_to(ROOT).as_posix()
            for path in skill_catalog.package_files(ROOT)
        }
        self.assertNotIn("skills/gsp-workflow/references/learnings.md", files)
        source = (ROOT / "scripts/build_skills.py").read_text(encoding="utf-8")
        self.assertNotIn("references/learnings.md", source)


if __name__ == "__main__":
    unittest.main()
