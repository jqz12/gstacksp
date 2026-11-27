import json
import unittest

from gsp.review_evidence import evaluate_review, record_issue_decision, validate_reviewer
from gsp.orchestration_types import SessionBinding

FP = 'a' * 64
CONTRACT = 'b' * 64


class ReviewEvidenceTests(unittest.TestCase):
    def report(self, findings, **extra):
        value = {'candidate_digest': FP, 'contract_digest': CONTRACT, 'findings': findings, **extra}
        return '# Review\nIndependent specification and quality review.\n<!-- gsp-review-evidence\n' + json.dumps(value) + '\n-->\n'

    def finding(self, **extra):
        return {'confirmed': True, 'acceptance_key': 'permission-contract',
                'root_cause_key': 'wrong-controller', 'signature': 'controller:wrong-owner',
                'trigger': 'spare controller writes plan', 'kind': 'authorization_bypass',
                'severity': 'P1', **extra}

    def evaluate(self, report, previous=None):
        return evaluate_review(report, FP, CONTRACT, previous or {}, work_uid='work', task_uid='T01')

    def test_independent_followup_does_not_expand_current_acceptance(self):
        result = self.evaluate(self.report([self.finding(kind='historical-defect',
            independent_of_current_acceptance=True, follow_up_owner='T15')]))
        self.assertEqual(result['blocking'], 0)
        self.assertEqual(result['findings'][0]['classification'], 'correctness_defect')

    def test_introduced_unsafe_defect_blocks_even_when_labelled_outside(self):
        result = self.evaluate(self.report([self.finding(independent_of_current_acceptance=True, follow_up_owner='T15')]))
        self.assertEqual(result['blocking'], 1)
        issue = next(iter(result['issues'].values()))
        omitted = self.evaluate(self.report([]), {issue['uid']: issue})
        self.assertEqual(omitted['blocking'], 1)

    def test_same_issue_counter_survives_new_report_and_goes_to_sol(self):
        result = self.evaluate(self.report([self.finding()]))
        uid = next(iter(result['issues']))
        repaired = record_issue_decision(result['issues'], {'uid': uid, 'action': 'worker_repair'})
        result = self.evaluate(self.report([self.finding()]), repaired)
        self.assertEqual(result['issues'][uid]['repair_count'], 1)
        self.assertEqual(result['findings'][0]['owner'], 'sol')
        with self.assertRaisesRegex(ValueError, 'ISSUE_SOL_TAKEOVER_REQUIRED'):
            record_issue_decision(result['issues'], {'uid': uid, 'action': 'worker_repair'})
        taken = record_issue_decision(result['issues'], {'uid': uid, 'action': 'sol_takeover',
                                                       'controller_binding_uid': 'current-controller'})
        self.assertEqual(taken[uid]['repair_count'], 1)
        self.assertEqual(taken[uid]['current_owner'], 'sol')

    def test_drift_missing_proof_and_unproven_resolution_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'CANDIDATE_STALE'):
            self.evaluate(self.report([], candidate_digest='c' * 64))
        with self.assertRaisesRegex(ValueError, 'REVIEW_EVIDENCE_REQUIRED'):
            self.evaluate('# Review\nNo findings but no frozen candidate metadata.')
        result = self.evaluate(self.report([self.finding()]))
        uid = next(iter(result['issues']))
        with self.assertRaisesRegex(ValueError, 'RESOLUTION_EVIDENCE_REQUIRED'):
            record_issue_decision(result['issues'], {'uid': uid, 'action': 'resolve'})

    def test_open_required_issue_cannot_be_relabelled_as_historical(self):
        result = self.evaluate(self.report([self.finding()]))
        relabelled = self.evaluate(self.report([self.finding(kind='historical-defect',
            independent_of_current_acceptance=True, follow_up_owner='T15')]), result['issues'])
        self.assertEqual(relabelled['blocking'], 1)

    def test_independence_requires_a_verified_separate_reviewer_context(self):
        from dataclasses import replace
        binding = SessionBinding(binding_uid='reviewer', kind='reviewer', work_uid='work',
            host='codex', host_state='verified', host_thread_id='independent-thread',
            capabilities=frozenset({'report'}))
        proof = validate_reviewer(binding, {'work_uuid': 'work'}, {'controller', 'executor'})
        self.assertEqual(proof['binding_uid'], 'reviewer')
        with self.assertRaisesRegex(ValueError, 'INDEPENDENT_REVIEW_REQUIRED'):
            validate_reviewer(binding, {'work_uuid': 'work', 'cancel_generation': 2}, set())
        self.assertEqual(validate_reviewer(replace(binding, cancel_generation=2),
            {'work_uuid': 'work', 'cancel_generation': 2}, set())['binding_uid'], 'reviewer')
        for bad in (replace(binding, kind='main'), replace(binding, host='unknown'),
                    replace(binding, cancelled=True), replace(binding, work_uid='foreign'),
                    replace(binding, host_thread_id='executor')):
            with self.subTest(binding=bad), self.assertRaisesRegex(ValueError, 'INDEPENDENT_REVIEW_REQUIRED'):
                validate_reviewer(bad, {'work_uuid': 'work'}, {'controller', 'executor'})

    def test_renaming_acceptance_in_a_new_contract_does_not_reset_the_same_problem(self):
        first = self.evaluate(self.report([self.finding()]))
        uid = next(iter(first['issues']))
        issues = record_issue_decision(first['issues'], {'uid': uid, 'action': 'worker_repair'})
        new_contract = 'c' * 64
        text = self.report([self.finding(acceptance_key='renamed-acceptance')], contract_digest=new_contract)
        result = evaluate_review(text, FP, new_contract, issues, work_uid='work', task_uid='T02')
        self.assertEqual(result['findings'][0]['issue_uid'], uid)
        self.assertEqual(result['findings'][0]['owner'], 'sol')
