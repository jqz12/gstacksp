"""Health reports GitHub user-install updates without mutating installations."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gsp.runtime import WorkflowError, _github_user_upgrade, health
from gsp.user_install import UserInstall


class HealthUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gsp-health-updates-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project, self.home = root / 'project', root / 'home'
        self.project.mkdir(); self.home.mkdir()
        self.profile = self.home / '.codex'
        skill = self.profile / 'skills/gsp-workflow/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('fixture installed skill', encoding='utf-8')
        manifest = self.profile / 'gstacksp/install.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({
            'schema': 2, 'scope': 'codex-user', 'host': 'codex',
            'source': 'E:/GstackSP', 'version': '0.8.10',
            'files': {'skills/gsp-workflow/SKILL.md': hashlib.sha256(skill.read_bytes()).hexdigest()},
        }), encoding='utf-8')
        self.env = patch.dict(os.environ, {'CODEX_HOME': str(self.profile)})
        self.env.start(); self.addCleanup(self.env.stop)

    def result(self, **values):
        row = {
            'schema': 1, 'source': 'https://github.com/jqz12/gstacksp.git',
            'checked_at': 100, 'status': 'ok', 'current': '0.8.10',
            'latest': 'v0.9.3', 'commit': 'a' * 40, 'available': True,
            'cached': False,
        }
        row.update(values)
        return row

    def test_health_reports_available_update_and_stays_read_only(self):
        before = (self.profile / 'gstacksp/install.json').read_bytes()
        with patch('gsp.update_check.check', return_value=self.result()):
            report = health(self.project, 'codex', force_update=True)
        updates = [r for r in report['checks'] if r['check'] == 'codex-user-update']
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]['status'], 'UPDATE_AVAILABLE')
        self.assertEqual(updates[0]['detail']['latest'], 'v0.9.3')
        self.assertTrue(report['static_ok'])
        self.assertEqual(before, (self.profile / 'gstacksp/install.json').read_bytes())

    def test_update_failure_is_not_a_health_failure(self):
        with patch('gsp.update_check.check', return_value=self.result(status='NOT_CHECKED', available=None, error='TimeoutExpired')):
            report = health(self.project, 'codex')
        row = next(r for r in report['checks'] if r['check'] == 'codex-user-update')
        self.assertEqual(row['status'], 'NOT_CHECKED')
        self.assertTrue(report['static_ok'])

    def test_confirmed_upgrade_resolves_published_tag_before_writing(self):
        profile = self.home / 'upgrade-profile'
        manager = UserInstall('codex', profile)
        manager.apply('install')
        update = self.result(current='0.8.10', latest='v0.9.4')
        resolved = {'source': 'https://github.com/jqz12/gstacksp.git',
                    'version': 'v0.9.4', 'commit': 'b' * 40,
                    'path': str(Path(__file__).resolve().parents[1])}
        with patch('gsp.update_check.check', return_value=update), \
             patch('gsp.github_source.resolve_source', return_value=resolved):
            result = _github_user_upgrade(manager, [], dry_run=False)
        self.assertEqual(result['update_check']['latest'], 'v0.9.4')
        installed = json.loads((profile / 'gstacksp/install.json').read_text(encoding='utf-8'))
        self.assertEqual(installed['source_version'], 'v0.9.4')
        self.assertEqual(installed['source_commit'], 'b' * 40)
        self.assertEqual(installed['source'], resolved['source'])

    def test_failed_github_check_leaves_the_existing_installation_untouched(self):
        profile = self.home / 'offline-profile'
        manager = UserInstall('codex', profile)
        manager.apply('install')
        before = (profile / 'gstacksp/install.json').read_bytes()
        with patch('gsp.update_check.check', return_value=self.result(status='NOT_CHECKED', available=None)):
            with self.assertRaisesRegex(WorkflowError, 'UPGRADE_SOURCE_UNAVAILABLE'):
                _github_user_upgrade(manager, [], dry_run=False)
        self.assertEqual(before, (profile / 'gstacksp/install.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
