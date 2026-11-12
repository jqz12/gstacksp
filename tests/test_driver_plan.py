"""Candidate acceptance tests: actual controller/state, no model or daemon."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_driver as driver_tests
from test_plan_quality import complete
from process_helpers import document
from gsp.driver import Driver
from gsp.runtime import Project, WorkflowError


class DriverPlanTests(unittest.TestCase):
    setUp = driver_tests.DriverTests.setUp
    cli = driver_tests.DriverTests.cli
    prepare = driver_tests.DriverTests.prepare
    status = driver_tests.DriverTests.status

    def start(self, fixes=2):
        self.prepare(fixes=fixes)
        self.driver = Driver(Project(self.root), 'work')
        return self.driver.next()['job']

    def response(self, job, text=None):
        result = dict(job_id=job['id'], outcome='completed', summary='synthetic plan',
                      question_kind='technical', question='', recommendation='', findings=[],
                      plan={'text': complete() if text is None else text, 'tasks': [
                          dict(id='T1',title='Label',acceptance='correct label',depends=[],no_tests_reason='') ]})
        Path(job['result']).write_text(json.dumps(result),encoding='utf-8')
        return result

    def test_reject_before_any_plan_or_task_mutation(self):
        job=self.start(); self.response(job, '# Short plan\nT1 write code and test.\n')
        plan=document(self.root,'work','plan'); state=self.root/'.gsp/runs/work/state.json'
        before=(plan.read_bytes(),state.read_bytes())
        result=self.driver.submit(job['id'],job['result'])
        self.assertEqual((plan.read_bytes(),state.read_bytes()),before)
        self.assertEqual(result['phase'],'plan')
        folder=Path(job['result']).parent
        self.assertFalse((folder/'accepted.json').exists())
        rejected=json.loads((folder/'rejected.json').read_text())
        self.assertFalse(rejected['quality']['structural_ok'])
        self.assertEqual(rejected['quality']['semantic_review'],'NOT_CHECKED')
        self.assertIsNone(self.driver.load()['pending'])
        with self.assertRaisesRegex(WorkflowError,'JOB_MISMATCH'):
            self.driver.submit(job['id'],job['result'])

    def test_restart_preserves_budget_and_feedback(self):
        job=self.start(fixes=1); self.response(job,'# Missing plan structure entirely')
        self.driver.submit(job['id'],job['result'])
        fresh=Driver(Project(self.root),'work'); second=fresh.next()['job']
        self.assertEqual(second['phase'],'plan')
        self.assertTrue(second['plan_findings'])
        self.assertEqual(fresh.load()['fix_rounds'],{'plan':1})
        deadline=fresh.data['deadline']
        self.response(second,'# Missing plan structure again')
        fresh.submit(second['id'],second['result'])
        fresh=Driver(Project(self.root),'work')
        self.assertEqual(fresh.next()['action'],'blocked')
        self.assertEqual(fresh.load()['reason'],'FIX_LIMIT')
        self.assertEqual(fresh.data['dispatches'],2)
        self.assertEqual(fresh.data['deadline'],deadline)

    def test_valid_repair_clears_pending_fix_before_execution(self):
        job=self.start(); self.response(job,'# Bad structure to fix')
        self.driver.submit(job['id'],job['result'])
        second=self.driver.next()['job']; self.response(second)
        self.driver.submit(second['id'],second['result'])
        d=self.driver.load()
        self.assertEqual(d['phase'],'execute'); self.assertIsNone(d['pending_fix'])
        self.assertEqual(d.get('plan_findings'),[])
        self.assertEqual(d['fix_rounds'],{'plan':1})
        next_job=self.driver.next()['job']
        self.assertEqual(next_job['phase'],'execute')
        self.assertEqual(self.driver.load()['fix_rounds'],{'plan':1})
        self.assertTrue(self.cli('plan-check','work')['structural_ok'])

    def test_oversize_candidate_rejected_without_writing(self):
        job=self.start(); self.response(job,complete()+'x'*1048576)
        plan=document(self.root,'work','plan'); before=plan.read_bytes()
        self.driver.submit(job['id'],job['result'])
        self.assertEqual(plan.read_bytes(),before)
        r=json.loads((Path(job['result']).parent/'rejected.json').read_text())
        self.assertIn('PLAN_TOO_LARGE',[x['code'] for x in r['quality']['findings']])

    def test_direct_apply_rejects_missing_structure_and_fake_headings(self):
        job=self.start(); state=self.cli('status','work')
        for text in ('# incomplete plan contents','```markdown\n'+complete()+'\n```'):
            result=self.response(job,text)
            before=document(self.root,'work','plan').read_bytes()
            with self.assertRaisesRegex(WorkflowError,'PLAN_STRUCTURE_INVALID'):
                self.driver.apply_plan(state,result['plan'])
            self.assertEqual(document(self.root,'work','plan').read_bytes(),before)
            self.assertEqual(self.cli('status','work')['tasks'],[])

    def test_preexisting_tasks_are_not_overwritten(self):
        job=self.start(); result=self.response(job)
        self.cli('task-add','work','OLD','--title','existing','--acceptance','preserve')
        state=self.cli('status','work'); before=document(self.root,'work','plan').read_bytes()
        with self.assertRaisesRegex(WorkflowError,'PLAN_RECONCILIATION_REQUIRED'):
            self.driver.apply_plan(state,result['plan'])
        self.assertEqual(document(self.root,'work','plan').read_bytes(),before)
        self.assertEqual([x['id'] for x in self.cli('status','work')['tasks']],['OLD'])

    def test_accepted_partial_write_is_not_replayed(self):
        job=self.start(); self.response(job)
        from gsp.driver import atomic as real_atomic
        plan=document(self.root,'work','plan')
        def fail_plan(path,value):
            if Path(path)==plan: raise PermissionError('synthetic disk failure')
            return real_atomic(path,value)
        with patch('gsp.driver.atomic',side_effect=fail_plan):
            with self.assertRaises(PermissionError): self.driver.submit(job['id'],job['result'])
        self.assertTrue((Path(job['result']).parent/'accepted.json').exists())
        with self.assertRaisesRegex(WorkflowError,'RESULT_ALREADY_ACCEPTED'):
            self.driver.submit(job['id'],job['result'])
        self.assertEqual(self.cli('status','work')['tasks'],[])

    def test_rejected_marker_interruption_is_not_replayed(self):
        job=self.start(); self.response(job,'# Missing plan entirely')
        from gsp.driver import atomic as real_atomic
        def fail_driver_state(path,value):
            if Path(path)==self.driver.file: raise PermissionError('synthetic state failure')
            return real_atomic(path,value)
        with patch('gsp.driver.atomic',side_effect=fail_driver_state):
            with self.assertRaises(PermissionError): self.driver.submit(job['id'],job['result'])
        self.assertTrue((Path(job['result']).parent/'rejected.json').exists())
        with self.assertRaisesRegex(WorkflowError,'RESULT_ALREADY_REJECTED'):
            self.driver.submit(job['id'],job['result'])
        self.assertEqual(self.driver.load()['dispatches'],1)

    def test_bad_schema_is_unknown_not_plan_repair(self):
        job=self.start(); Path(job['result']).write_text('{}')
        self.driver.submit(job['id'],job['result'])
        self.assertEqual(self.driver.load()['reason'],'EXECUTION_UNKNOWN')
        self.assertEqual(self.driver.data['fix_rounds'],{})

    def recover(self):
        report=self.root/'.gsp/recovery.md'
        report.write_text('SYNTHETIC TEST: no worker; inspected interrupted plan state.')
        return self.cli('drive-recover','work','--evidence',str(report),'--reason','SYNTHETIC TEST investigation')

    def test_recover_after_seal_clears_plan_retry_not_budget(self):
        job=self.start(fixes=1);self.response(job,'# Incomplete candidate plan')
        self.driver.submit(job['id'],job['result'])
        job=self.driver.next()['job'];self.response(job)
        original=self.driver.save
        def fail_submit(event):
            if event=='submitted': raise PermissionError('synthetic final-state write failure')
            return original(event)
        with patch.object(self.driver,'save',side_effect=fail_submit):
            with self.assertRaises(PermissionError):self.driver.submit(job['id'],job['result'])
        self.recover()
        self.recover()  # A second investigation must not resurrect the plan retry.
        fresh=Driver(Project(self.root),'work');result=fresh.next()
        self.assertEqual(result['action'],'dispatch')
        self.assertEqual(result['job']['phase'],'execute')
        self.assertEqual(fresh.load()['fix_rounds'],{'plan':1})
        self.assertIsNone(fresh.data['pending_fix']);self.assertEqual(fresh.data['plan_findings'],[])

    def interrupt_rejection(self):
        job=self.start(fixes=0);self.response(job,'# Incomplete candidate plan')
        from gsp.driver import atomic as real_atomic
        def fail_driver_state(path,value):
            if Path(path)==self.driver.file:raise PermissionError('synthetic state failure')
            return real_atomic(path,value)
        with patch('gsp.driver.atomic',side_effect=fail_driver_state):
            with self.assertRaises(PermissionError):self.driver.submit(job['id'],job['result'])
        return job

    def test_recover_rejection_restores_feedback_and_exhausted_budget(self):
        self.interrupt_rejection();self.recover()
        self.recover()  # Recovery does not grant another initial planning attempt.
        fresh=Driver(Project(self.root),'work');data=fresh.load()
        self.assertEqual(data['pending_fix'],'plan');self.assertTrue(data['plan_findings'])
        self.assertEqual(fresh.next()['action'],'blocked')
        self.assertEqual(fresh.data['reason'],'FIX_LIMIT');self.assertEqual(fresh.data['dispatches'],1)

    def test_recovery_refuses_changed_rejected_response_without_state_mutation(self):
        job=self.interrupt_rejection();self.response(job)
        state=self.root/'.gsp/runs/work/state.json';before=state.read_bytes()
        report=self.root/'.gsp/recovery.md';report.write_text('SYNTHETIC TEST investigation')
        out=self.cli('drive-recover','work','--evidence',str(report),'--reason','fixture',ok=False)
        self.assertIn('PLAN_REJECTION_MISMATCH',out)
        self.assertEqual(state.read_bytes(),before)

    def test_process_fixture_repairs_plan_then_completes(self):
        self.prepare('plan-fail-once',fixes=1)
        self.cli('drive-run','work')
        d=self.status()
        self.assertEqual(d['status'],'completed')
        self.assertEqual(d['fix_rounds'],{'plan':1})
        self.assertEqual(d['dispatches'],4)
        self.assertEqual(self.cli('status','work')['status'],'ready')

if __name__=='__main__': unittest.main()
