import unittest
import json
from types import SimpleNamespace
from pathlib import Path
from tests import test_orchestration_resources as resources
from gsp.resource_registry import ResourceRegistryError


class SchedulerAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.fixture=resources.ResourceRegistryTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.registry=self.fixture.registry

    def test_cursor_and_slot_grant_commit_together_and_replay_once(self):
        snapshot=self.registry.scheduler_snapshot()
        self.assertEqual(0,snapshot['revision'])
        token={'revision':0,'cursor':{'work_uid':'work-1'}}
        owner=self.fixture.owner()
        self.registry.reserve_execution_batch('batch-1',owner,[('path','src/a')],
            [('slot-1',owner)],scheduler=token)
        first=self.registry.scheduler_snapshot()
        self.assertEqual((1,1),(first['revision'],first['occupied']))
        self.assertEqual(token['cursor'],first['cursor'])
        self.registry.reserve_execution_batch('batch-1',owner,[('path','src/a')],
            [('slot-1',owner)],scheduler=token)
        self.assertEqual(first,self.registry.scheduler_snapshot())

    def test_scheduler_token_change_or_malformed_replay_fails_closed(self):
        owner=self.fixture.owner()
        token={'revision':0,'cursor':{'work_uid':'work-1'}}
        self.registry.reserve_execution_batch(
            'batch-token',owner,[('path','src/a')],[('slot-token',owner)],
            scheduler=token)
        before=self.registry.scheduler_snapshot()
        with self.assertRaisesRegex(
            ResourceRegistryError,'RESOURCE_REQUEST_INPUT_CHANGED'
        ):
            self.registry.reserve_execution_batch(
                'batch-token',owner,[('path','src/a')],[('slot-token',owner)],
                scheduler={'revision':1,'cursor':{'work_uid':'work-2'}})
        with self.assertRaisesRegex(
            ResourceRegistryError,'SCHEDULER_TOKEN_INVALID'
        ):
            self.registry.reserve_execution_batch(
                'batch-token',owner,[('path','src/a')],[('slot-token',owner)],
                scheduler={'revision':None,'cursor':{'work_uid':'work-1'}})
        self.assertEqual(before,self.registry.scheduler_snapshot())

    def test_scheduler_request_replay_cannot_change_owner(self):
        owner=self.fixture.owner()
        token={'revision':0,'cursor':{'work_uid':'work-1'}}
        self.registry.reserve_execution_batch(
            'batch-owner',owner,[('path','src/a')],[('slot-owner',owner)],
            scheduler=token)
        before=self.registry.scheduler_snapshot()
        with self.assertRaisesRegex(
            ResourceRegistryError,'RESOURCE_REQUEST_INPUT_CHANGED'
        ):
            self.registry.reserve_execution_batch(
                'batch-owner',self.fixture.owner(session='session-2'),
                [('path','src/a')],[('slot-owner',owner)],scheduler=token)
        self.assertEqual(before,self.registry.scheduler_snapshot())

    def test_stale_schedule_cannot_leave_partial_claims(self):
        owner=self.fixture.owner()
        token={'revision':0,'cursor':{'work_uid':'work-1'}}
        self.registry.reserve_execution_batch('first',owner,[('path','src/a')],
            [('first-slot',owner)],scheduler=token)
        with self.assertRaisesRegex(ResourceRegistryError,'SCHEDULER_VERSION_CHANGED'):
            self.registry.reserve_execution_batch('second',self.fixture.owner('work-2'),
                [('path','src/b')],[('second-slot',self.fixture.owner('work-2'))],scheduler=token)
        self.assertEqual([],self.registry.list_for_work('work-2'))
        self.assertEqual(1,self.registry.scheduler_snapshot()['occupied'])

    def test_review_without_worktree_shares_the_same_three_slots(self):
        owner=self.fixture.owner()
        for number in range(3):
            self.registry.reserve_execution_slot('review-'+str(number),owner)
        with self.assertRaisesRegex(ResourceRegistryError,'RESOURCE_SLOTS_EXHAUSTED'):
            self.registry.reserve_execution_slot('fourth',owner)
        self.assertEqual(3,self.registry.scheduler_snapshot()['occupied'])
        self.registry.release_execution_slot('review-0',owner)
        self.registry.release('review-0',owner)
        self.registry.reserve_execution_slot('replacement',owner)
        self.assertEqual(3,self.registry.scheduler_snapshot()['occupied'])

    def test_project_queue_reads_sealed_contracts_and_rotates_after_cas_grant(self):
        from gsp.contract_store import publish_contracts
        from gsp.project_scheduler import preview_schedule
        root=self.fixture.root
        project=SimpleNamespace(root=root,meta=root/'.gsp')
        project.paths=lambda ident:(root/'.gsp/runs'/ident,root/'documents'/ident)
        project.document=lambda state,kind:root/'documents'/state['id']/(kind+'.md')
        project.workspace_root=lambda state,**kwargs:root
        for ident in ['a','b']:
            folder=root/'documents'/ident
            folder.mkdir(parents=True)
            (folder/'design.md').write_text('# Design\nPreserve existing behavior.',encoding='utf-8')
            (folder/'plan.md').write_text('# Plan\n## Task T01\nImplement isolated module.\n'
                '**Writes:** '+json.dumps({'paths':['modules/'+ident]})+'\n',encoding='utf-8')
            state={'id':ident,'project_id':'resource-project','work_uuid':'work-'+ident,
                   'status':'execute','operation_schema':2,'tasks':[{'id':'T01','status':'pending',
                   'title':'isolated module','acceptance':'behavior passes','depends':[]}]}
            publish_contracts(project,state)
            file=project.paths(ident)[0]/'state.json'
            file.write_text(json.dumps(state),encoding='utf-8')
        first=preview_schedule(project,limit=1)
        self.assertEqual('work-a',first['batch'][0]['work_uid'])
        before={str(path.relative_to(root)):path.read_bytes() for path in root.rglob('*') if path.is_file()}
        self.assertEqual(first,preview_schedule(project,limit=1))
        after={str(path.relative_to(root)):path.read_bytes() for path in root.rglob('*') if path.is_file()}
        self.assertEqual(before,after)
        self.registry.reserve_execution_batch('fair-grant',self.fixture.owner('work-a'),
            [('path','modules/a')],[('fair-slot',self.fixture.owner('work-a'))],
            scheduler={'revision':first['revision'],'cursor':{'work_uid':'work-a'}})
        next_view=preview_schedule(project,limit=2)
        self.assertEqual('work-b',next_view['batch'][0]['work_uid'])


if __name__=='__main__':
    unittest.main()
