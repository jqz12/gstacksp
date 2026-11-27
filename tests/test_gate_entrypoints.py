"""Required task inputs must fail before the real execution side effect."""
import sys
import unittest
from unittest.mock import patch

from gsp.core import WorkflowError
from tests import test_topology_lifecycle as lifecycle


class EffectGateEntryTests(unittest.TestCase):
    def setUp(self):
        self.fixture=lifecycle.TopologyLifecycleTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setup_git()
        self.fixture.prepare()
        self.fixture.cmd('topology-select','work','--mode','in-place','--quote','SYNTHETIC fixture')
        self.fixture.cmd('advance','work','--to','execute')
        self.fixture.cmd('task-start','work','T01')

    def test_required_contract_missing_rejects_before_run(self):
        state=self.fixture.state()
        state.pop('task_contracts',None)
        state['architecture_policy']={'required':True,'baseline':'.gsp/architecture/baseline.json'}
        self.fixture.project.write(state,'fixture','require explicit contract',
                                   action_uid='fixture:require-contract')
        state_file=self.fixture.root/'.gsp/runs/work/state.json'
        before=state_file.read_bytes()
        marker=self.fixture.root/'.gsp/child-started'
        code=f"from pathlib import Path;Path({str(marker)!r}).write_text('started');print('Ran 1 test')"
        with self.assertRaisesRegex(WorkflowError,'TASK_CONTRACT_REQUIRED'):
            self.fixture.cmd('run','work','--task','T01','--',sys.executable,'-c',code)
        self.assertFalse(marker.exists())
        self.assertEqual(before,state_file.read_bytes())

    def test_unresolved_required_issue_blocks_delivery_but_not_diagnostics(self):
        from gsp.gate_rules import guard_inputs
        state=self.fixture.state()
        state['issues']={'one':{'status':'open','blocking_current_acceptance':True}}
        with self.assertRaisesRegex(WorkflowError,'UNRESOLVED_ISSUES'):
            guard_inputs(self.fixture.project,state,phase='accept')
        self.assertTrue(guard_inputs(self.fixture.project,state,phase='execute')['allowed'])
        state['issues']['one']['status']='resolved'
        self.assertTrue(guard_inputs(self.fixture.project,state,phase='accept')['allowed'])

    def test_other_work_claim_rejected_before_execution(self):
        from gsp.gate_rules import guard_inputs
        state=self.fixture.state()
        with patch('gsp.resource_registry.ResourceRegistry.conflicts',return_value=[{
                'owner':{'work_uid':'foreign-work','session_uid':'foreign','generation':1}}]):
            with self.assertRaisesRegex(WorkflowError,'RESOURCE_CONFLICT'):
                guard_inputs(self.fixture.project,state,'T01',phase='execute')


if __name__=='__main__':
    unittest.main()
