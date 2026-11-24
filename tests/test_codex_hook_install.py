from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from gsp.codex_hook_install import HookInstall
from gsp.runtime import WorkflowError


class HookInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.notify={'hooks':[{'type':'command','command':'powershell notifier.ps1','timeout':10}]}
        self.config={'description':'personal','hooks':{'Stop':[self.notify]}}
        (self.root/'hooks.json').write_text(json.dumps(self.config))
        self.manager=HookInstall(self.root)

    def test_install_repeat_upgrade_uninstall_preserve_notifier(self):
        before=(self.root/'hooks.json').read_bytes()
        self.manager.apply('install',dry_run=True)
        self.assertEqual((self.root/'hooks.json').read_bytes(),before)
        self.assertFalse(self.manager.manifest.exists())
        self.manager.apply('install')
        self.assertEqual(json.loads((self.root/'hooks.json').read_text())['hooks']['Stop'][0],self.notify)
        self.assertTrue(self.manager.status()['integrity_ok'])
        config=(self.root/'hooks.json').read_bytes()
        self.manager.apply('install')
        self.assertEqual((self.root/'hooks.json').read_bytes(),config)
        self.manager.apply('upgrade')
        self.manager.apply('uninstall')
        self.assertEqual(json.loads((self.root/'hooks.json').read_text()),self.config)

    def test_edited_runtime_refused_without_config_loss(self):
        self.manager.apply('install')
        f=self.root/'gstacksp/hooks/gsp/codex_hooks.py'
        f.write_text('user changed')
        before=(self.root/'hooks.json').read_bytes()
        with self.assertRaises(WorkflowError):self.manager.apply('upgrade')
        with self.assertRaises(WorkflowError):self.manager.apply('uninstall')
        self.assertEqual((self.root/'hooks.json').read_bytes(),before)

    def test_corrupt_or_edited_registration_refused(self):
        (self.root/'hooks.json').write_text('{bad')
        with self.assertRaises(WorkflowError): self.manager.apply('install')
        (self.root/'hooks.json').write_text(json.dumps(self.config))
        self.manager.apply('install')
        d=json.loads((self.root/'hooks.json').read_text())
        d['hooks']['Stop'][-1]['hooks'][0]['timeout']=99
        (self.root/'hooks.json').write_text(json.dumps(d))
        with self.assertRaises(WorkflowError):self.manager.apply('upgrade')

    def test_interrupted_publication_rolls_back_and_recovery_preserves_later_edits(self):
        import gsp.codex_hook_install as module
        real=module.publish
        n=0
        def fail(path,data):
            nonlocal n
            n+=1
            if n==3:raise KeyboardInterrupt()
            real(path,data)
        with patch.object(module,'publish',side_effect=fail):
            with self.assertRaises(KeyboardInterrupt):self.manager.apply('install')
        self.assertTrue(self.manager.journal.exists())
        self.manager.recover()
        self.assertEqual(json.loads((self.root/'hooks.json').read_text()),self.config)
        self.assertFalse(self.manager.manifest.exists())

    def test_new_personal_hook_after_install_is_retained(self):
        self.manager.apply('install')
        d=json.loads((self.root/'hooks.json').read_text())
        d['hooks']['Stop'].insert(0,{'hooks':[{'type':'command','command':'another'}]})
        (self.root/'hooks.json').write_text(json.dumps(d))
        self.manager.apply('uninstall')
        self.assertEqual(len(json.loads((self.root/'hooks.json').read_text())['hooks']['Stop']),2)

    def test_recovery_refuses_later_user_edit(self):
        import gsp.codex_hook_install as module
        real=module.publish
        n=0
        def fail(path,data):
            nonlocal n
            n+=1
            if n==3:raise KeyboardInterrupt()
            real(path,data)
        with patch.object(module,'publish',side_effect=fail):
            with self.assertRaises(KeyboardInterrupt):self.manager.apply('install')
        journal=json.loads(self.manager.journal.read_text())
        target=self.root/journal['rows'][0]['path']
        target.write_text('later user edit')
        with self.assertRaises(WorkflowError):self.manager.recover()
        self.assertEqual(target.read_text(),'later user edit')

    def test_dead_installer_process_is_recoverable_without_stealing_live_lock(self):
        import sys
        from process_helpers import run
        code='''import os
import gsp.codex_hook_install as m
real=m.publish
n=0
def fail(path,data):
 global n
 n+=1
 if n==3:os._exit(73)
 real(path,data)
m.publish=fail
m.HookInstall(PROFILE).apply('install')
'''.replace('PROFILE',repr(str(self.root)))
        result=run([sys.executable,'-c',code],capture_output=True)
        self.assertEqual(result.returncode,73,result.stderr)
        self.manager.recover()
        self.assertEqual(json.loads((self.root/'hooks.json').read_text()),self.config)

    def test_fresh_profile_uninstall_removes_only_owned_config(self):
        with tempfile.TemporaryDirectory() as folder:
            manager=HookInstall(folder)
            manager.apply('install')
            manager.apply('uninstall')
            self.assertFalse((Path(folder)/'hooks.json').exists())

    def test_deployed_runtime_works_outside_source_checkout(self):
        import sys
        from process_helpers import run
        self.manager.apply('install')
        result=run([sys.executable,str(self.root/'gstacksp/hooks/scripts/gsp.py'),'codex-hook'],
                   input=json.dumps({'hook_event_name':'Stop','cwd':str(self.root),'session_id':'test'}),
                   text=True,capture_output=True,cwd=self.root,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),{})

    def test_deployed_runtime_wait_guard(self):
        import sys
        from process_helpers import run
        project=self.root/'project'
        (project/'.gsp').mkdir(parents=True)
        (project/'.gsp/project.json').write_text(json.dumps({'root':str(project)}))
        self.manager.apply('install')
        script=self.root/'gstacksp/hooks/scripts/gsp.py'

        def call():
            payload={
                'hook_event_name':'PreToolUse',
                'tool_name':'write_stdin',
                'cwd':str(project),
                'session_id':'deployed-outer-session',
                'tool_input':{
                    'session_id':'deployed-tool-session',
                    'chars':'',
                    'yield_time_ms':30000,
                    'max_output_tokens':20000,
                },
            }
            return run([sys.executable,str(script),'codex-hook'],
                       input=json.dumps(payload),text=True,capture_output=True,
                       cwd=project,timeout=20)

        responses=[json.loads(call().stdout) for _ in range(3)]
        self.assertNotIn('permissionDecision',responses[0].get('hookSpecificOutput',{}))
        self.assertNotIn('permissionDecision',responses[1].get('hookSpecificOutput',{}))
        self.assertEqual(responses[2]['hookSpecificOutput']['permissionDecision'],'deny')
        audit=''.join(path.read_text() for path in (project/'.gsp/audits/codex-hooks').rglob('*') if path.is_file())
        self.assertNotIn('deployed-outer-session',audit)
        self.assertNotIn('deployed-tool-session',audit)

    def test_deployed_runtime_wait_guard_accepts_bash_alias(self):
        import sys
        from process_helpers import run
        project=self.root/'project'
        (project/'.gsp').mkdir(parents=True)
        (project/'.gsp/project.json').write_text(json.dumps({'root':str(project)}))
        self.manager.apply('install')
        script=self.root/'gstacksp/hooks/scripts/gsp.py'

        def call():
            payload={
                'hook_event_name':'PreToolUse',
                'tool_name':'Bash',
                'cwd':str(project),
                'session_id':'deployed-outer-session',
                'tool_input':{
                    'session_id':74439,
                    'chars':'',
                    'yield_time_ms':1000,
                    'max_output_tokens':2000,
                },
            }
            return run([sys.executable,str(script),'codex-hook'],
                       input=json.dumps(payload),text=True,capture_output=True,
                       cwd=project,timeout=20)

        responses=[json.loads(call().stdout) for _ in range(3)]
        self.assertNotIn('permissionDecision',responses[0].get('hookSpecificOutput',{}))
        self.assertNotIn('permissionDecision',responses[1].get('hookSpecificOutput',{}))
        self.assertEqual(responses[2]['hookSpecificOutput']['permissionDecision'],'deny')
        audit=''.join(path.read_text() for path in (project/'.gsp/audits/codex-hooks').rglob('*') if path.is_file())
        self.assertNotIn('74439',audit)

    @unittest.skipUnless(__import__('os').name=='nt','Windows shell boundary')
    def test_actual_registered_command_runs_in_powershell_and_cmd(self):
        from process_helpers import run
        self.manager.apply('install')
        d=json.loads((self.root/'hooks.json').read_text())
        command=d['hooks']['PreToolUse'][0]['hooks'][0]['command']
        payload=json.dumps({'cwd':str(self.root),'hook_event_name':'Stop','session_id':'registered-command-fixture'})
        for argv in [['powershell','-NoProfile','-NonInteractive','-Command',command],'cmd.exe /d /s /c "'+command+'"']:
            r=run(argv,input=payload,text=True,encoding='utf-8',errors='replace',capture_output=True,timeout=20,cwd=self.root)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertEqual(json.loads(r.stdout),{})
        project=self.root/'project';(project/'.gsp').mkdir(parents=True)
        (project/'.gsp/project.json').write_text(json.dumps({'root':str(project)}))
        payload=json.dumps({'cwd':str(project),'hook_event_name':'UserPromptSubmit','session_id':'shell-fixture'})
        r=run(['powershell','-NoProfile','-NonInteractive','-Command',command],input=payload,text=True,encoding='utf-8',capture_output=True,timeout=20,cwd=project)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertTrue(list((project/'.gsp/audits/codex-hooks').rglob('events-*.jsonl')))

    @unittest.skipUnless(__import__('os').name=='nt','Windows Unicode launcher')
    def test_launcher_preserves_unicode_spaces_and_apostrophe(self):
        from process_helpers import run
        profile=self.root/"profile 中文 O'Brien"
        manager=HookInstall(profile);manager.apply('install')
        d=json.loads((profile/'hooks.json').read_text(encoding='utf-8'))
        command=d['hooks']['PreToolUse'][0]['hooks'][0]['command']
        r=run(['powershell','-NoProfile','-NonInteractive','-Command',command],input=json.dumps({'cwd':str(profile),'hook_event_name':'Stop'}),text=True,encoding='utf-8',capture_output=True,timeout=20,cwd=profile)
        self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(json.loads(r.stdout),{})
