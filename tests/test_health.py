"""Install-scope health checks must not hide one broken install with another."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    from process_helpers import environment
except ModuleNotFoundError:
    from tests.process_helpers import environment

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gsp.runtime import health


class HealthScopesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gsp-health-scopes-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project, self.home = root / 'project', root / 'home'
        self.project.mkdir(); self.home.mkdir()
        self.env = environment('CODEX_HOME', str(self.home / '.codex'))
        self.env.__enter__(); self.addCleanup(self.env.__exit__, None, None, None)

    def global_install(self):
        profile = self.home / '.codex'
        file = profile / 'skills/gsp-workflow/SKILL.md'
        file.parent.mkdir(parents=True)
        file.write_text('fixture installed skill', encoding='utf-8')
        manifest = profile / 'gstacksp/install.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({'files': {'skills/gsp-workflow/SKILL.md': hashlib.sha256(file.read_bytes()).hexdigest()}}), encoding='utf-8')
        return file

    def report(self):
        return health(self.project, 'codex')

    def test_user_only_install_is_recognized(self):
        self.global_install()
        self.assertTrue(self.report()['static_ok'])
        self.assertTrue(any(r.get('scope') == 'user' and r['status'] == 'PASS' for r in self.report()['checks']))

    def test_changed_user_install_fails_integrity(self):
        file = self.global_install()
        file.write_text('changed bytes', encoding='utf-8')
        self.assertFalse(self.report()['static_ok'])

    def test_good_user_install_does_not_hide_broken_project(self):
        self.global_install()
        meta = self.project / '.gsp'; meta.mkdir()
        (meta / 'install-codex.json').write_text(json.dumps({'files': {'.agents/skills/gsp-workflow/SKILL.md': 'missing'}}), encoding='utf-8')
        report = self.report()
        self.assertFalse(report['static_ok'])
        self.assertTrue(any(r.get('scope') == 'project' and r['status'] == 'FAIL' for r in report['checks']))

    def test_no_install_does_not_pass(self):
        self.assertFalse(self.report()['static_ok'])

    def test_manifest_cannot_escape_scope(self):
        self.global_install()
        manifest = self.home / '.codex/gstacksp/install.json'
        manifest.write_text(json.dumps({'files': {'../outside.txt': 'invalid'}}), encoding='utf-8')
        self.assertFalse(self.report()['static_ok'])


if __name__ == '__main__':
    unittest.main()
