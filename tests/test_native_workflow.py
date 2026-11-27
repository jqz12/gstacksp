import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from gsp.codex_workspace import CodexWorkspaceAdapter
from gsp.orchestration_types import RequestRef,WorktreeRequest,digest_value
from gsp.core import WorkflowError
from gsp.workflow_operations import dispatch
from gsp.command_policies import parser
from gsp.workflow_support import contract_hash
from tests import test_topology_lifecycle as lifecycle
from tests import test_codex_workspace_lifecycle as host
from tests.process_helpers import public_main_binding,temporary_env


class NativeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture=lifecycle.TopologyLifecycleTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f=self.fixture
        f.setup_git()
        f.prepare()
        self.actor=public_main_binding(f.root,f.state()['work_uuid'])
        self.native_area=tempfile.TemporaryDirectory(prefix='gsp-native-checkout-')
        self.addCleanup(self.native_area.cleanup)
        self.native=Path(self.native_area.name)/'checkout'
        self.addCleanup(lambda:f.git('worktree','remove','--force',str(self.native))
                        if self.native.exists() else None)
        self.counter=0
        self.receipts=SimpleNamespace(owner='local',control=f.root,
            native_payload=host.CodexWorkspaceLifecycleTests.native_payload)

    def command(self,*argv):
        with temporary_env({'GSP_SESSION_CONTEXT':str(self.fixture.root/self.actor.handle_path)}):
            return dispatch(self.fixture.project,parser().parse_args(list(argv)),enforce_context=True)

    def resource(self,action,uid,payload):
        self.counter+=1
        path=self.fixture.root/'.gsp'/('host-input-'+str(self.counter)+'.json')
        path.write_text(json.dumps(payload),encoding='utf-8')
        return self.command('native-workspace','work',action,'--request-uid',uid,'--payload',str(path))

    def test_native_plan_adoption_quality_archive_and_missing_tree_close(self):
        f=self.fixture
        before=f.git('worktree','list','--porcelain')
        planned,ok=self.command('topology-select','work','--mode','worktree','--quote','SYNTHETIC native source')
        self.assertTrue(ok,planned)
        self.assertEqual('create_worktree',planned['pending_action']['tool'])
        self.assertEqual(before,f.git('worktree','list','--porcelain'))
        self.assertFalse((f.root/'.gsp/worktrees').exists())
        raw=planned['create_intent']['request']
        create=WorktreeRequest(RequestRef.from_dict(raw['request']),raw['owner_adapter'],raw['source_root'],
                               raw['base_sha'],raw['branch'],raw['path_hint'])
        f.git('worktree','add','--detach',str(self.native),create.base_sha)
        receipt=host.CodexWorkspaceLifecycleTests.create_receipt(self.receipts,create,self.native)
        confirmed,ok=self.resource('confirm',planned['request_uid'],{
            'receipt':receipt,'source_root':str(self.native)})
        self.assertTrue(ok,confirmed)
        state=f.state()
        self.assertEqual(str(self.native),str(f.project.workspace_root(state)))
        self.assertEqual('archive',state['git_topology']['cleanup_action'])
        self.assertFalse(state['git_topology']['managed_worktree'])
        replay,ok=self.command('topology-select','work','--mode','worktree','--quote','SYNTHETIC native source')
        self.assertTrue(ok,replay)
        self.assertTrue(replay['replayed'])
        f.cmd('advance','work','--to','execute')
        f.cmd('task-start','work','T01')
        f.check()
        f.cmd('task-done','work','T01')
        f.check('all')
        f.cmd('advance','work','--to','review')
        (f.project.paths('work')[1]/'review.md').write_text(
            '# Review\nSynthetic native source checked without blocking findings.',encoding='utf-8')
        f.cmd('review','work','--reviewer','self','--blocking','0')
        f.cmd('advance','work','--to','checking')
        f.cmd('advance','work','--to','ready')
        f.cmd('ready','work')
        state=f.state()
        f.cmd('work-stage',state['work_uuid'],'delivered','--reason','SYNTHETIC ready delivery',
              '--closure-condition','SYNTHETIC all checks and archive proof preserved')
        uid='native-archive-main'
        contract=contract_hash(f.project,state,f.project.paths('work')[1])
        ref=RequestRef(uid,uid,digest_value({'archive':uid}),self.actor.project_uid,state['work_uuid'],
            None,'native-worktree-archive',self.actor.controller_generation,self.actor.cancel_generation,
            contract_digest=contract)
        request=WorktreeRequest(ref,'codex-native-worktree',str(f.root),create.base_sha,create.branch,str(self.native))
        archive,ok=self.resource('archive',uid,{'request':ref.to_dict(),'worktree':request.to_dict(),
            'owner':'local','workspace':confirmed})
        self.assertTrue(ok,archive)
        adapter=CodexWorkspaceAdapter(f.root,state['project_id'])
        intent=adapter.host_action(uid)
        f.git('worktree','remove',str(self.native))
        receipt=host.CodexWorkspaceLifecycleTests.action_receipt(self.receipts,intent,
            result_type='archived',state='archived')
        archived,ok=self.resource('confirm',uid,{'intent_key':uid,'receipt':receipt,'owner':'local'})
        self.assertTrue(ok,archived)
        self.assertFalse(self.native.exists())
        closed,ok=self.command('close','work','--cleanup','--reason','SYNTHETIC verified native archive')
        self.assertTrue(ok,closed)
        self.assertEqual('closed',f.state()['status'])
        replay,ok=self.command('close','work','--cleanup','--reason','SYNTHETIC verified native archive')
        self.assertTrue(ok,replay)
        self.assertTrue(replay['replayed'])


if __name__=='__main__':
    unittest.main()
