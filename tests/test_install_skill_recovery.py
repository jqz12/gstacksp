import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gsp.install as install_module
from gsp.install import install
from gsp.runtime import digest


ROOT = Path(__file__).resolve().parents[1]


class InstallSkillRecoveryTests(unittest.TestCase):
    def test_publication_failure_restores_retired_files_and_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder) / "project"
            project.mkdir()
            retired = project / ".agents/skills/gsp-learn/SKILL.md"
            retired.parent.mkdir(parents=True)
            retired.write_text("legacy\n", encoding="utf-8")
            manifest = project / ".gsp/install-codex.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "host": "codex",
                        "files": {
                            retired.relative_to(project).as_posix(): digest(retired)
                        },
                        "skills": ["gsp-learn"],
                    }
                ),
                encoding="utf-8",
            )
            manifest_before = manifest.read_bytes()
            original = install_module._publish
            calls = {"count": 0}

            def fail_once(path, data):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("injected publication failure")
                return original(path, data)

            with patch.object(install_module, "_publish", side_effect=fail_once):
                with self.assertRaises(OSError):
                    install(project, "codex", [], source_root=ROOT)

            self.assertEqual(retired.read_text(encoding="utf-8"), "legacy\n")
            self.assertEqual(manifest.read_bytes(), manifest_before)
            self.assertFalse((project / "AGENTS.md").exists())
            self.assertFalse(
                (project / ".gsp/install-codex.transaction.json").exists()
            )
            self.assertEqual(
                len(list((project / ".gsp/install-backups").glob("*/operation.json"))),
                1,
            )


if __name__ == "__main__":
    unittest.main()
