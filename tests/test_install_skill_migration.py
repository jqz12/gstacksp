import json
from pathlib import Path
import tempfile
import unittest

from gsp.install import install
from gsp.runtime import WorkflowError, digest


ROOT = Path(__file__).resolve().parents[1]


class InstallSkillMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.base = self.project / ".agents/skills"
        self.manifest = self.project / ".gsp/install-codex.json"
        self.catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))

    def _legacy_files(self, names):
        files = {}
        for name in names:
            path = self.base / name / "SKILL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"legacy {name}\n", encoding="utf-8")
            files[path.relative_to(self.project).as_posix()] = digest(path)
        return files

    def test_upgrade_removes_all_managed_retired_entries_and_keeps_backup(self):
        retired = [item["name"] for item in self.catalog["retired"]]
        old_files = self._legacy_files(retired)
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "host": "codex",
                    "files": old_files,
                    "skills": retired,
                }
            ),
            encoding="utf-8",
        )
        learnings = self.project / "docs/gsp/learnings.jsonl"
        learnings.parent.mkdir(parents=True)
        learnings.write_text('{"personal":true}\n', encoding="utf-8")
        gbrain = self.project / ".gbrain/config.json"
        gbrain.parent.mkdir()
        gbrain.write_text('{"personal":true}\n', encoding="utf-8")

        result = install(
            self.project,
            "codex",
            [],
            source_root=ROOT,
        )

        record = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(record["schema"], 2)
        self.assertEqual(record["skills"], sorted(record["skills"]))
        self.assertEqual(len(record["skills"]), 6)
        self.assertEqual(record["requested_optional"], [])
        self.assertFalse(set(retired) & set(record["skills"]))
        for name in retired:
            self.assertFalse((self.base / name).exists(), name)
        self.assertTrue(learnings.is_file())
        self.assertTrue(gbrain.is_file())
        self.assertTrue(result["installed"][0]["backup"])
        backups = list((self.project / ".gsp/install-backups").glob("*/operation.json"))
        self.assertEqual(len(backups), 1)

    def test_unknown_retired_entry_blocks_without_mutation(self):
        name = self.catalog["retired"][0]["name"]
        unknown = self.base / name / "SKILL.md"
        unknown.parent.mkdir(parents=True)
        unknown.write_text("user-owned\n", encoding="utf-8")

        with self.assertRaises(WorkflowError) as raised:
            install(self.project, "codex", [], source_root=ROOT)

        self.assertIn("INSTALL_CONFLICT", str(raised.exception))
        self.assertEqual(unknown.read_text(encoding="utf-8"), "user-owned\n")
        self.assertFalse(self.manifest.exists())

    def test_install_uses_canonical_manifest_and_publishes_one_projection(self):
        from gsp import skill_catalog
        from gsp.runtime import sha

        result = install(self.project, "codex", ["health"], source_root=ROOT)

        record = json.loads(self.manifest.read_text(encoding="utf-8"))
        canonical = ROOT / skill_catalog.MANIFEST_REL
        installed = self.base / "gsp-workflow/scripts/skill-manifest.json"
        self.assertEqual(installed.read_bytes(), canonical.read_bytes())
        installed_manifest = skill_catalog.load_manifest(installed.parent)
        self.assertEqual(len(installed_manifest["entries"]), 50)
        self.assertEqual(len(skill_catalog.public_entries(installed_manifest)), 10)
        self.assertEqual(
            record["skill_manifest_sha256"],
            sha(canonical.read_bytes()),
        )
        self.assertEqual(record["requested_optional"], ["gsp-health"])
        self.assertEqual(
            sorted(path.name for path in self.base.glob("gsp-*") if path.is_dir()),
            sorted([
                "gsp-brainstorming",
                "gsp-execute",
                "gsp-health",
                "gsp-review",
                "gsp-ship",
                "gsp-workflow",
                "gsp-writing-plans",
            ]),
        )
        methods = self.base / "gsp-workflow/methods"
        self.assertEqual(len(list(methods.glob("*/METHOD.md"))), 26)
        self.assertFalse((self.base / "gsp-context-save").exists())
        self.assertFalse((self.base / "gsp-context-restore").exists())
        self.assertTrue(result["installed"])

    def test_edited_managed_retired_entry_blocks_before_any_workflow_file(self):
        name = self.catalog["retired"][0]["name"]
        path = self.base / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("original\n", encoding="utf-8")
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "host": "codex",
                    "files": {path.relative_to(self.project).as_posix(): digest(path)},
                    "skills": [name],
                }
            ),
            encoding="utf-8",
        )
        path.write_text("user-edited\n", encoding="utf-8")
        manifest_before = self.manifest.read_bytes()

        with self.assertRaises(WorkflowError) as raised:
            install(self.project, "codex", [], source_root=ROOT)

        self.assertIn("INSTALL_CONFLICT", str(raised.exception))
        self.assertEqual(path.read_text(encoding="utf-8"), "user-edited\n")
        self.assertEqual(self.manifest.read_bytes(), manifest_before)
        self.assertFalse((self.project / "AGENTS.md").exists())


if __name__ == "__main__":
    unittest.main()
