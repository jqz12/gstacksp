from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

from gsp.codex_hook_install import HookInstall
from gsp.user_install import UserInstall


class HookMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = HookInstall(Path(self.temp.name))

    def test_missing_install_has_explicit_next_action(self):
        result = self.manager.status()
        self.assertEqual(result['maintenance']['state'], 'not-installed')
        self.assertIn('hooks-install', result['maintenance']['next_command'])

    def test_current_and_version_label_only(self):
        self.manager.apply('install')
        self.assertEqual(self.manager.status()['maintenance']['state'], 'current')
        files, groups, version = self.manager.payload()
        files = dict(files)
        files['gstacksp/hooks/gsp/__init__.py'] = b'__version__ = "old"\n'
        with patch.object(self.manager, 'payload', return_value=(files, groups, 'old')):
            self.manager.apply('upgrade')
        # A changed module body is a runtime change, even in __init__.py.
        self.assertEqual(self.manager.status()['maintenance']['state'], 'runtime-update')

    def test_version_only_runtime_registration_and_removed_file(self):
        files, groups, version = self.manager.payload()
        old_files = dict(files)
        key = 'gstacksp/hooks/gsp/__init__.py'
        old_files[key] = files[key].replace(version.encode(), b'0.0.0')
        with patch.object(self.manager, 'payload', return_value=(old_files, groups, '0.0.0')):
            self.manager.apply('install')
        result = self.manager.status()['maintenance']
        self.assertEqual(result['state'], 'version-label-update')
        self.assertFalse(result['registrations_changed'])
        extra = dict(files)
        extra['gstacksp/hooks/obsolete.py'] = b'# old\n'
        with patch.object(self.manager, 'payload', return_value=(extra, groups, version)):
            self.manager.apply('upgrade')
        result = self.manager.status()['maintenance']
        self.assertEqual(result['state'], 'runtime-update')
        self.assertIn('gstacksp/hooks/obsolete.py', result['removed_files'])
        changed_groups = dict(groups)
        changed_groups.pop(next(iter(groups)))
        with patch.object(self.manager, 'payload', return_value=(files, changed_groups, version)):
            self.assertEqual(self.manager.status()['maintenance']['state'], 'registration-update')

    def test_source_unavailable_and_corrupt_install_are_distinct(self):
        self.manager.apply('install')
        with patch.object(self.manager, 'payload', side_effect=OSError('missing source')):
            result = self.manager.status()
        self.assertTrue(result['integrity_ok'])
        self.assertEqual(result['maintenance']['state'], 'source-unavailable')
        (self.manager.root/'gstacksp/hooks/gsp/__init__.py').write_text('edited')
        result = self.manager.status()
        self.assertFalse(result['integrity_ok'])
        self.assertEqual(result['maintenance']['state'], 'repair-required')

    def test_skill_install_reports_hooks_without_creating_them(self):
        manager = UserInstall('codex', self.manager.root)
        result = manager.apply('install')
        self.assertEqual(result['hooks']['maintenance']['state'], 'not-installed')
        self.assertFalse(self.manager.manifest.exists())
        self.manager.apply('install')
        result = manager.apply('upgrade')
        self.assertEqual(result['hooks']['maintenance']['state'], 'current')
        self.assertNotIn('hooks', manager.apply('uninstall'))

    def test_missing_version_is_reported_without_blocking_skill_install(self):
        self.manager.manifest.parent.mkdir(parents=True)
        self.manager.manifest.write_text(json.dumps({
            'schema': 1, 'profile': str(self.manager.root), 'files': {}, 'groups': {}
        }))
        result = UserInstall('codex', self.manager.root).apply('install')
        self.assertEqual(result['hooks']['maintenance']['state'], 'repair-required')
        self.assertTrue((self.manager.root/'gstacksp/install.json').is_file())

    def test_pending_recovery_takes_priority_over_current_or_upgrade(self):
        self.manager.apply('install')
        self.manager.journal.write_text('{}')
        result = self.manager.status()['maintenance']
        self.assertEqual(result['state'], 'recovery-required')
        self.assertEqual(result['next_command'], 'hooks-recover')
        files, groups, version = self.manager.payload()
        changed = dict(files)
        changed['gstacksp/hooks/new.py'] = b'# source change\n'
        with patch.object(self.manager, 'payload', return_value=(changed, groups, version)):
            result = self.manager.status()['maintenance']
        self.assertEqual(result['state'], 'recovery-required')
        self.assertIn('gstacksp/hooks/new.py', result['changed_files'])

    def test_bundled_skill_cli_can_install_and_compare_hooks(self):
        from process_helpers import run
        profile = self.manager.root/'skills-profile'
        UserInstall('codex', profile).apply('install')
        script = profile/'skills/gsp-workflow/scripts/gsp.py'
        target = self.manager.root/'hooks-profile'
        installed = run([sys.executable, str(script), 'hooks-install', '--profile', str(target)],
                        cwd=self.manager.root, capture_output=True, text=True, timeout=60)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        status = run([sys.executable, str(script), 'hooks-status', '--profile', str(target)],
                     cwd=self.manager.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(status.returncode, 0, status.stderr)
        result = json.loads(status.stdout)
        self.assertTrue(result['integrity_ok'])
        self.assertEqual(result['maintenance']['state'], 'current')
