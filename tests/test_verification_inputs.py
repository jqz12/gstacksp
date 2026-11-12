import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import subprocess
import time

from gsp.runtime import snapshot, WorkflowError
from process_helpers import run


class VerificationInputsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def put(self,rel,text='data'):
        p=self.root/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text);return p

    def test_git_tracks_ignored_tracked_and_new_source_but_not_build_cache(self):
        run(['git','init',str(self.root)],capture_output=True,check=True)
        self.put('.gitignore','target/\ncustom-cache/\nsecret.toml\ntracked.txt\n')
        self.put('tracked.txt');run(['git','-C',str(self.root),'add','-f','tracked.txt'],capture_output=True,check=True)
        self.put('src/new.rs');self.put('target/large.bin');self.put('custom-cache/cache.bin');self.put('secret.toml')
        rows=snapshot(self.root)
        self.assertIn('tracked.txt',rows);self.assertIn('src/new.rs',rows)
        self.assertNotIn('target/large.bin',rows);self.assertNotIn('custom-cache/cache.bin',rows)
        self.assertNotIn('secret.toml',rows)

    def test_ignored_necessary_input_can_be_explicitly_included(self):
        run(['git','init',str(self.root)],capture_output=True,check=True)
        self.put('.gitignore','config.toml\n');self.put('config.toml')
        self.put('.gsp/verification-inputs.json','{"include":["config.toml"]}')
        self.assertIn('config.toml',snapshot(self.root))

    def test_non_git_fallback_excludes_known_build_dirs_not_source(self):
        self.put('src/a.rs');self.put('target/debug/big');self.put('build/cache');self.put('dist/a.exe')
        rows=snapshot(self.root)
        self.assertIn('src/a.rs',rows);self.assertNotIn('target/debug/big',rows)
        self.assertNotIn('build/cache',rows);self.assertNotIn('dist/a.exe',rows)

    def test_deadline_interrupts_chunked_hash(self):
        from gsp.verification_inputs import scan
        self.put('large.bin','x'*100)
        with self.assertRaisesRegex(WorkflowError,'SCAN_TIMEOUT'):
            scan(self.root,deadline=time.monotonic()-1)

    def test_exclude_policy_cannot_hide_tracked_source(self):
        run(['git','init',str(self.root)],capture_output=True,check=True)
        self.put('src/a.rs');run(['git','-C',str(self.root),'add','.'],capture_output=True,check=True)
        self.put('.gsp/verification-inputs.json','{"exclude":["src/**"]}')
        with self.assertRaisesRegex(WorkflowError,'TRACKED_INPUT_EXCLUDED'):
            snapshot(self.root)

    def test_run_has_two_scans_and_post_failure_keeps_command_result(self):
        from gsp import runtime
        import argparse,json,sys
        project=runtime.Project(self.root)
        state={'id':'scan-test','status':'execute','scope':{'mode':'off'},'tasks':[{'id':'T1','status':'running'}],
               'evidence':[],'documents':{'design':'docs/spec.md','plan':'docs/plan.md'}}
        self.put('docs/spec.md','spec');self.put('docs/plan.md','plan')
        runfolder=self.root/'.gsp/runs/scan-test';runfolder.mkdir(parents=True)
        args=argparse.Namespace(task='T1',argv=[sys.executable,'-c','pass'],timeout=10,kind='check',runner=lambda a,t:subprocess.CompletedProcess(a,0,'',''))
        with patch.object(runtime,'snapshot',return_value={'input':'hash'}) as calls,patch.object(project,'write'),patch.object(runtime,'contract_hash',return_value='contract'):
            receipt,ok=runtime.run_check(project,state,runfolder,args)
        self.assertTrue(ok);self.assertEqual(calls.call_count,2)
        self.assertEqual(receipt['fingerprint'],runtime.sha(__import__('json').dumps({'input':'hash'},sort_keys=True).encode()))
        with patch.object(runtime,'snapshot',side_effect=[{'input':'hash'},WorkflowError('SCAN_TIMEOUT')]),patch.object(project,'write'):
            with self.assertRaisesRegex(WorkflowError,'SCAN_TIMEOUT'):runtime.run_check(project,state,runfolder,args)
        folder=runfolder/'evidence'/state['inflight']['id']
        self.assertTrue((folder/'command-result.json').exists())
        self.assertFalse((folder/'receipt.json').exists())

    def test_pre_scan_failure_does_not_execute_command_or_leave_unknown_execution(self):
        from gsp import runtime
        import argparse
        project=runtime.Project(self.root)
        state={'id':'test','tasks':[{'id':'T1','status':'running'}]}
        args=argparse.Namespace(task='T1',argv=['not-to-run'],timeout=1,kind='check')
        with patch.object(runtime,'snapshot',side_effect=WorkflowError('SCAN_TIMEOUT')),patch.object(project,'write'),patch.object(runtime.subprocess,'run') as command:
            with self.assertRaisesRegex(WorkflowError,'SCAN_TIMEOUT'):runtime.run_check(project,state,self.root/'.gsp/runs/test',args)
        command.assert_not_called();self.assertNotIn('inflight',state)

    def test_expired_budget_after_scan_never_starts_command(self):
        from gsp import runtime
        import argparse
        project=runtime.Project(self.root);state={'id':'test','tasks':[{'id':'T1','status':'running'}]}
        args=argparse.Namespace(task='T1',argv=['not-to-run'],timeout=1,kind='check')
        with patch.object(runtime,'snapshot',return_value={}),patch.object(project,'write'),patch.object(runtime.time,'monotonic',side_effect=[0,2]),patch.object(runtime.subprocess,'run') as command:
            with self.assertRaisesRegex(WorkflowError,'SCAN_TIMEOUT'):runtime.run_check(project,state,self.root/'.gsp/runs/test',args)
        command.assert_not_called();self.assertNotIn('inflight',state)

    def test_corrupt_policy_is_workflow_error(self):
        self.put('.gsp/verification-inputs.json','{bad')
        with self.assertRaises(WorkflowError):snapshot(self.root)

    def test_git_detection_failure_cannot_drop_tracked_build_source(self):
        from gsp.verification_inputs import scan
        run(['git','init',str(self.root)],capture_output=True,check=True)
        self.put('build/source.txt');run(['git','-C',str(self.root),'add','.'],capture_output=True,check=True)
        self.assertIn('build/source.txt',scan(self.root))
        with patch('gsp.verification_inputs.subprocess.run',return_value=subprocess.CompletedProcess([],128,b'',b'fatal: detected dubious ownership')):
            with self.assertRaisesRegex(WorkflowError,'SCAN_GIT'):scan(self.root)
