from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gsp.runtime import WorkflowError, read


class UserInstallTests(unittest.TestCase):
    def test_routing_keeps_bom_and_crlf_personal_content(self):
        file=self.profile/'AGENTS.md'
        personal=b'\xef\xbb\xbfPersonal instructions\r\nSecond line\r\n'
        file.write_bytes(personal)
        self.manager.apply('install')
        installed=file.read_bytes()
        self.assertTrue(installed.startswith(personal))
        self.manager.apply('upgrade')
        self.assertEqual(file.read_bytes(),installed)
        self.manager.apply('uninstall')
        self.assertTrue(file.read_bytes().startswith(personal))

    def test_instruction_changes_during_preflight_are_not_overwritten(self):
        instruction=self.profile/'AGENTS.md'
        instruction.write_text('Original personal instructions',encoding='utf-8')
        original=self.manager.payload
        def changed(*args):
            result=original(*args)
            instruction.write_text('Human wrote new instructions',encoding='utf-8')
            return result
        with patch.object(self.manager,'payload',side_effect=changed):
            with self.assertRaises(WorkflowError): self.manager.apply('install')
        self.assertEqual(instruction.read_text(),'Human wrote new instructions')
        self.assertFalse(self.manager.manifest.exists())

    def test_legacy_manifest_adopts_block_hash_without_changing_personal_text(self):
        from gsp.runtime import atomic, digest
        self.manager.apply('install')
        record=read(self.manager.manifest)
        record.pop('instruction_block_sha256')
        record['schema']=1
        record['instruction_sha256']=digest(self.profile/'AGENTS.md')
        atomic(self.manager.manifest,record)
        before=(self.profile/'AGENTS.md').read_bytes()
        self.manager.apply('upgrade')
        self.assertEqual((self.profile/'AGENTS.md').read_bytes(),before)
        self.assertIn('instruction_block_sha256',read(self.manager.manifest))

    def test_recovery_does_not_remove_live_writer_lock(self):
        import os
        import gsp.user_install as module
        from gsp.runtime import atomic
        with patch.object(module,'publish',side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt): self.manager.apply('install')
        lock=self.profile/'.gsp/writer.lock'
        atomic(lock,{'pid':os.getpid()})
        with self.assertRaises(WorkflowError): self.manager.recover()
        self.assertTrue(lock.exists())
        lock.unlink()
        self.manager.recover()

    def test_real_dead_writer_can_be_recovered_without_stealing_live_lock(self):
        import sys
        from process_helpers import run
        # Keep the child source literal simple and independent of shell quoting.
        code = f'''import os
import gsp.user_install as m
original=m.publish
count=0
def interrupt(path,data):
 global count
 count+=1
 if count==3: os._exit(73)
 original(path,data)
m.publish=interrupt
m.UserInstall("codex",{str(self.profile)!r}).apply("install")
'''
        result=run([sys.executable,'-c',code],capture_output=True)
        self.assertEqual(result.returncode,73,result.stderr)
        self.assertTrue((self.profile/'.gsp/writer.lock').exists())
        self.manager.recover(dry_run=True)
        self.assertTrue(self.manager.journal.exists())
        self.manager.recover()
        self.assertFalse((self.profile/'.gsp/writer.lock').exists())
        self.assertFalse(self.manager.manifest.exists())

    def test_upgrade_publishes_new_bytes_and_keeps_previous_backup(self):
        self.manager.apply('install')
        files, skills, version = self.manager.payload([], read(self.manager.manifest))
        key = 'skills/gsp-workflow/SKILL.md'
        before = (self.profile/key).read_bytes()
        files[key] = before+b'\nUpdated fixture version\n'
        with patch.object(self.manager, 'payload', return_value=(files,skills,'fixture-next')):
            result = self.manager.apply('upgrade')
        self.assertEqual((self.profile/key).read_bytes(), files[key])
        self.assertEqual(read(self.manager.manifest)['version'],'fixture-next')
        backup = Path(result['backup'])
        journal = read(backup/'operation.json')
        item = next(row for row in journal['changes'] if row['path']==key)
        self.assertEqual((backup/item['backup']).read_bytes(), before)

    def test_interrupted_recovery_refuses_new_human_edits(self):
        import gsp.user_install as module
        original = module.publish
        completed = []
        def interrupt(path, value):
            if len(completed) == 2: raise KeyboardInterrupt()
            original(path,value); completed.append(Path(path))
        with patch.object(module,'publish',side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt): self.manager.apply('install')
        completed[0].write_text('User intervened',encoding='utf-8')
        with self.assertRaises(WorkflowError): self.manager.recover()
        self.assertTrue(self.manager.journal.exists())
        self.assertEqual(completed[0].read_text(),'User intervened')

    def test_cli_supports_separate_claude_profile_without_changing_other_host(self):
        import json, sys
        from process_helpers import run
        script = Path(__file__).resolve().parents[1]/'scripts/gsp.py'
        for action in ('install','upgrade','uninstall'):
            argv = [sys.executable,str(script),action,'--host','claude','--scope','user','--profile',str(self.profile)]
            if action == 'upgrade':
                argv.extend(['--source', 'local'])
            result = run(argv, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(json.loads(result.stdout)['scope'],'claude-user')
        self.assertFalse((self.profile/'AGENTS.md').exists())
        self.assertFalse((self.profile/'settings.json').exists())

    def setUp(self):
        from gsp.user_install import UserInstall
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root/'profile'
        self.profile.mkdir()
        self.manager = UserInstall('codex', self.profile)

    def test_install_upgrade_uninstall_preserves_unrelated_content(self):
        instruction = self.profile/'AGENTS.md'
        instruction.write_text('My personal instructions\n', encoding='utf-8')
        other = self.profile/'skills/other/SKILL.md'
        other.parent.mkdir(parents=True); other.write_text('Unrelated', encoding='utf-8')
        self.assertTrue(self.manager.apply('install', dry_run=True)['dry_run'])
        self.assertFalse(self.manager.manifest.exists())
        self.manager.apply('install')
        self.assertEqual(read(self.manager.manifest)['scope'], 'codex-user')
        with instruction.open('a', encoding='utf-8') as out: out.write('\nMy new instructions\n')
        self.manager.apply('upgrade')
        self.manager.apply('uninstall')
        self.assertEqual(other.read_text(), 'Unrelated')
        self.assertIn('My personal instructions', instruction.read_text())
        self.assertIn('My new instructions', instruction.read_text())
        self.assertNotIn('gstacksp:codex-user:begin', instruction.read_text())
        self.assertFalse(self.manager.manifest.exists())
        self.assertFalse((self.profile/'skills/gsp-workflow/SKILL.md').exists())

    def test_zcode_user_install_uses_global_zcode_profile(self):
        from gsp.user_install import UserInstall
        profile = self.root / 'zcode-profile'
        manager = UserInstall('zcode', profile)
        result = manager.apply('install')
        self.assertEqual(result['scope'], 'zcode-user')
        self.assertTrue((profile / 'skills/gsp-workflow/SKILL.md').is_file())
        self.assertTrue((profile / 'AGENTS.md').is_file())
        self.assertEqual(read(profile / 'gstacksp/install.json')['scope'], 'zcode-user')

    def test_user_edits_refuse_before_any_mutation(self):
        self.manager.apply('install')
        file = self.profile/'skills/gsp-execute/SKILL.md'
        file.write_text('User edited', encoding='utf-8')
        before = self.manager.manifest.read_bytes()
        for action in ('upgrade', 'uninstall'):
            with self.assertRaises(WorkflowError): self.manager.apply(action)
        self.assertEqual(file.read_text(), 'User edited')
        self.assertEqual(self.manager.manifest.read_bytes(), before)

    def test_requested_optional_persists_without_promoting_old_core_defaults(self):
        self.manager.apply(
            'install',
            optional=[
                'health',
                'writing-skills',
                'document-governance',
                'diagnosing-superpowers',
            ],
        )
        record = read(self.manager.manifest)
        self.assertEqual(
            record['requested_optional'],
            [
                'gsp-diagnosing-superpowers',
                'gsp-document-governance',
                'gsp-health',
                'gsp-writing-skills',
            ],
        )
        record.pop('requested_optional')
        from gsp.runtime import atomic
        atomic(self.manager.manifest, record)

        self.manager.apply('upgrade')

        migrated = read(self.manager.manifest)
        self.assertEqual(
            migrated['requested_optional'],
            ['gsp-diagnosing-superpowers', 'gsp-document-governance'],
        )
        self.assertIn('gsp-document-governance', migrated['skills'])
        self.assertIn('gsp-diagnosing-superpowers', migrated['skills'])
        self.assertNotIn('gsp-health', migrated['skills'])
        self.assertNotIn('gsp-writing-skills', migrated['skills'])
        self.assertFalse(
            (self.profile / 'skills/gsp-health/SKILL.md').exists()
        )
        self.assertFalse(
            (self.profile / 'skills/gsp-writing-skills/SKILL.md').exists()
        )

    def test_publication_failure_rolls_back_and_interruption_can_recover(self):
        import gsp.user_install as module
        original = module.publish
        calls = []
        def fail(path, value):
            calls.append(path)
            if len(calls) == 3: raise OSError('Injected failure')
            return original(path, value)
        with patch.object(module, 'publish', side_effect=fail):
            with self.assertRaises(OSError): self.manager.apply('install')
        self.assertFalse(self.manager.manifest.exists())
        self.assertFalse((self.profile/'skills/gsp-workflow/SKILL.md').exists())
        calls.clear()
        def interrupt(path, value):
            calls.append(path)
            if len(calls) == 3: raise KeyboardInterrupt()
            return original(path, value)
        with patch.object(module, 'publish', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt): self.manager.apply('install')
        self.assertTrue(self.manager.journal.exists())
        self.manager.recover()
        self.assertFalse(self.manager.journal.exists())
        self.assertFalse(self.manager.manifest.exists())

    def test_malicious_manifest_and_unknown_skill_collision_are_rejected(self):
        from gsp.runtime import atomic
        atomic(self.manager.manifest, {'scope':'codex-user','files':{'../outside':'fake'}})
        with self.assertRaises(WorkflowError): self.manager.apply('uninstall')
        self.manager.manifest.unlink()
        file = self.profile/'skills/gsp-workflow/SKILL.md'
        file.parent.mkdir(parents=True); file.write_text('Unknown owner')
        with self.assertRaises(WorkflowError): self.manager.apply('install')
