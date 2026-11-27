import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

from gsp import skill_catalog
from scripts import build_skills
from scripts import package


ROOT = Path(__file__).resolve().parents[1]


class SkillPackagingTests(unittest.TestCase):
    def _snapshot(self, root):
        return {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*")
            if path.is_file()
        }

    def test_build_removes_managed_retired_entries_and_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="gsp-skill-build-") as folder:
            root = Path(folder)
            shutil.copytree(ROOT / "sources", root / "sources")
            shutil.copytree(ROOT / "skills/gsp-workflow", root / "skills/gsp-workflow")
            manifest = skill_catalog.load_manifest(ROOT)
            for name in skill_catalog.names_for_class(manifest, "retired", "internal"):
                orphan = root / "skills" / skill_catalog.full_name(name) / "SKILL.md"
                orphan.parent.mkdir(parents=True, exist_ok=True)
                orphan.write_text("stale discoverable entry", encoding="utf-8")
            shutil.copy2(ROOT / "skills/skill-manifest.json", root / "skills/skill-manifest.json")

            build_skills.build(root)
            first = self._snapshot(root / "skills")
            build_skills.build(root)
            second = self._snapshot(root / "skills")
            self.assertEqual(first, second)

            active = {
                path.parent.name
                for path in (root / "skills").glob("gsp-*/SKILL.md")
            }
            expected = {entry.name for entry in skill_catalog.public_entries(manifest)}
            self.assertEqual(active, expected)
            for name in skill_catalog.names_for_class(manifest, "retired", "internal"):
                self.assertFalse(
                    (root / "skills" / skill_catalog.full_name(name) / "SKILL.md").exists()
                )

            catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(catalog["schema"], 2)
            self.assertEqual(
                [item["name"] for item in catalog["skills"]],
                [entry.name for entry in skill_catalog.public_entries(manifest)],
            )
            physical = {row["path"] for row in catalog["methods"]}
            self.assertEqual(len(catalog["methods"]), 28)
            self.assertEqual(len(physical), 27)
            self.assertEqual(len(catalog["retired"]), 40)
            self.assertFalse(
                (root / "skills/gsp-workflow/references/learnings.md").exists()
            )

    def test_package_uses_active_catalog_and_never_reintroduces_retired_skill_files(self):
        with tempfile.TemporaryDirectory(prefix="gsp-skill-package-") as folder:
            root = Path(folder)
            shutil.copytree(ROOT / "sources", root / "sources")
            shutil.copytree(ROOT / "skills/gsp-workflow", root / "skills/gsp-workflow")
            manifest = skill_catalog.load_manifest(ROOT)
            shutil.copy2(ROOT / "skills/skill-manifest.json", root / "skills/skill-manifest.json")
            for name in skill_catalog.names_for_class(manifest, "retired"):
                stale = root / "skills" / skill_catalog.full_name(name) / "SKILL.md"
                stale.parent.mkdir(parents=True, exist_ok=True)
                stale.write_text("must not enter a bundle", encoding="utf-8")
            build_skills.build(root)
            for name in skill_catalog.names_for_class(manifest, "retired", "internal"):
                stale = root / "skills" / skill_catalog.full_name(name) / "SKILL.md"
                stale.parent.mkdir(parents=True, exist_ok=True)
                stale.write_text("reintroduced after build", encoding="utf-8")

            files = {path.relative_to(root).as_posix() for path in package.collect_files(root)}
            for name in skill_catalog.names_for_class(manifest, "retired", "internal"):
                self.assertFalse(
                    any(
                        path.startswith(f"skills/{skill_catalog.full_name(name)}/")
                        for path in files
                    ),
                    name,
                )
            self.assertIn("skills/gsp-workflow/references/methods.md", files)
            self.assertIn(
                f"skills/gsp-workflow/methods/{skill_catalog.full_name('test-driven-development')}/METHOD.md",
                files,
            )
            output = package.package(root, source_check=False)
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
            for name in skill_catalog.names_for_class(manifest, "retired", "internal"):
                self.assertFalse(
                    any(
                        f"/skills/{skill_catalog.full_name(name)}/" in archive_name
                        for archive_name in names
                    ),
                    name,
                )
            self.assertTrue(
                any("/skills/gsp-workflow/methods/gsp-test-driven-development/METHOD.md" in name for name in names)
            )
            self.assertFalse(
                any("/skills/gsp-workflow/methods/gsp-context-save/" in name for name in names)
            )
            self.assertFalse(
                any("/skills/gsp-workflow/methods/gsp-context-restore/" in name for name in names)
            )
            self.assertIn(
                "GstackSP/skills/gsp-workflow/references/contract.md",
                names,
            )
            self.assertNotIn(
                "GstackSP/skills/gsp-workflow/references/learnings.md",
                names,
            )
            method_paths = {
                name
                for name in names
                if "/skills/gsp-workflow/methods/" in name and name.endswith("/METHOD.md")
            }
            self.assertEqual(len(method_paths), 26)

    def test_build_generates_lf_text_on_windows(self):
        with tempfile.TemporaryDirectory(prefix="gsp-skill-lf-") as folder:
            root = Path(folder)
            shutil.copytree(ROOT / "sources", root / "sources")
            shutil.copytree(ROOT / "skills/gsp-workflow", root / "skills/gsp-workflow")
            shutil.copy2(ROOT / "skills/skill-manifest.json", root / "skills/skill-manifest.json")
            build_skills.build(root)

            generated = [root / "catalog.json"]
            manifest = skill_catalog.load_manifest(root)
            for entry in skill_catalog.public_entries(manifest):
                directory = root / Path(entry.public_paths[0]).parent
                generated.append(directory / "SKILL.md")
                if entry.name != "gsp-workflow":
                    generated.append(directory / "agents/openai.yaml")
            for method in skill_catalog.stage_methods(manifest):
                if not method.shared_artifact:
                    generated.append(root / method.internal_method_path)
            references = root / "skills/gsp-workflow/references"
            generated.extend(
                (
                    references / "methods.md",
                    references / "cross-session.md",
                    references / "stage-transitions.md",
                    references / "convergence.md",
                )
            )

            crlf = [
                path.relative_to(root).as_posix()
                for path in generated
                if b"\r" in path.read_bytes()
            ]
            self.assertEqual(crlf, [])

    def test_source_package_rejects_reintroduced_orphan_skill(self):
        with tempfile.TemporaryDirectory(prefix="gsp-skill-orphan-") as folder:
            root = Path(folder)
            shutil.copytree(ROOT / "sources", root / "sources")
            shutil.copytree(ROOT / "skills/gsp-workflow", root / "skills/gsp-workflow")
            shutil.copy2(ROOT / "skills/skill-manifest.json", root / "skills/skill-manifest.json")
            build_skills.build(root)
            orphan = root / "skills/gsp-learn/SKILL.md"
            orphan.parent.mkdir(parents=True)
            orphan.write_text("unlisted\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                package.package(root)


if __name__ == "__main__":
    unittest.main()
