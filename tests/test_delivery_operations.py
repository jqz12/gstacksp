import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from gsp.command_policies import parser
from gsp.core import WorkflowError
from tests import test_topology_lifecycle as lifecycle
from tests.process_helpers import public_main_binding, temporary_env


class DeliveryParserTests(unittest.TestCase):
    def test_delivery_status_has_a_normal_readonly_entrypoint(self):
        args=parser().parse_args(['delivery','work','status','--request-uid','request-1'])
        self.assertEqual('delivery',args.command)
        self.assertEqual('status',args.action)


class DeliveryEntryTests(unittest.TestCase):
    def setUp(self):
        self.fixture=lifecycle.TopologyLifecycleTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f=self.fixture
        f.setup_git()
        self.base=f.git('rev-parse','HEAD')
        f.prepare()
        f.cmd('topology-select','work','--mode','branch','--quote','SYNTHETIC source branch')
        f.cmd('advance','work','--to','execute')
        f.cmd('task-start','work','T01')
        self.binding=public_main_binding(f.root,f.state()['work_uuid'])

    def invoke(self,*args):
        from gsp.workflow_operations import dispatch
        parsed=parser().parse_args(['delivery','work',*args])
        with temporary_env({'GSP_SESSION_CONTEXT':str(self.fixture.root/self.binding.handle_path)}):
            return dispatch(self.fixture.project,parsed,enforce_context=True)

    def test_unauthorized_prepare_has_zero_landing_or_git_changes(self):
        f=self.fixture
        before=f.git('show-ref')
        with self.assertRaisesRegex(WorkflowError,'AUTHORIZATION_MISSING'):
            self.invoke('prepare','--request-uid','denied','--target','refs/heads/main',
                        '--expected-target',self.base)
        self.assertEqual(before,f.git('show-ref'))
        self.assertFalse((f.root/'.gsp/landing').exists())

    def test_ship_push_authorization_is_required_before_any_landing_effect(self):
        f=self.fixture
        f.cmd('authorize','work','merge','--scope','refs/heads/main',
              '--quote','SYNTHETIC local merge only')
        before=f.git('show-ref')
        with self.assertRaisesRegex(WorkflowError,'AUTHORIZATION_MISSING push'):
            self.invoke('ship','--request-uid','denied-push','--target','refs/heads/main',
                        '--expected-target',self.base,'--push')
        self.assertEqual(before,f.git('show-ref'))
        self.assertFalse((f.root/'.gsp/landing').exists())

    def test_real_public_prepare_land_and_replay_after_existing_quality(self):
        f=self.fixture
        f.cmd('authorize','work','merge','--scope','refs/heads/main','--quote','SYNTHETIC fixture merge authorized')
        f.check()
        f.cmd('task-done','work','T01')
        f.check('all')
        f.cmd('advance','work','--to','review')
        (f.project.paths('work')[1]/'review.md').write_text(
            '# Review\nFixture implementation reviewed; no blocking findings.',encoding='utf-8')
        f.cmd('review','work','--reviewer','self','--blocking','0')
        f.cmd('advance','work','--to','checking')
        f.cmd('advance','work','--to','ready')
        f.cmd('ready','work')
        f.git('add','--all')
        f.git('commit','-qm','reviewed fixture candidate')
        f.cmd('observe-head','work','--reason','SYNTHETIC reviewed candidate committed')
        prepared,ok=self.invoke('prepare','--request-uid','public-landing','--target','refs/heads/main',
                                 '--expected-target',self.base)
        self.assertTrue(ok,prepared)
        self.assertEqual('prepared',prepared['state'])
        result,ok=self.invoke('land','--request-uid','public-landing')
        self.assertTrue(ok,result)
        self.assertEqual(f.git('rev-parse','HEAD'),f.git('rev-parse','main'))
        replay,ok=self.invoke('recover','--request-uid','public-landing')
        self.assertTrue(ok,replay)
        self.assertEqual(result['proof_uid'],replay['proof_uid'])
        # Optional push is an independently authorized, durable stage. Use a
        # local bare fixture; replay must not start another push process.
        remote_fixture=tempfile.TemporaryDirectory(prefix='gsp-delivery-remote-')
        self.addCleanup(remote_fixture.cleanup)
        remote=Path(remote_fixture.name)/'repo.git'
        f.git('init','--bare','--quiet',str(remote))
        f.git('remote','add','origin',str(remote))
        f.cmd('authorize','work','push','--scope','origin:refs/heads/main',
              '--quote','SYNTHETIC bare fixture push authorized')
        from gsp.git_operations import _git_run
        calls=[]
        def trace(root,*argv):
            if argv and argv[0]=='push':
                calls.append(argv)
            return _git_run(root,*argv)
        with patch('gsp.delivery_operations._git_run',side_effect=trace):
            for _ in range(2):
                shipped,ok=self.invoke('ship','--request-uid','public-landing',
                    '--target','refs/heads/main','--expected-target',self.base,'--push')
                self.assertTrue(ok,shipped)
        self.assertEqual(1,len(calls))
        from gsp.landing import load_landing
        self.assertEqual('pushed',load_landing(f.root,'public-landing')['delivery_push']['state'])


if __name__=='__main__':
    unittest.main()
