from datetime import datetime, timedelta, timezone
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from gsp.convergence import (
    apply_decision,
    complete_guard,
    plan_policies,
    seal_policies,
    start_guard,
)
from tests.process_helpers import document, run, session_binding

NOW = datetime(2026, 11, 27, 0, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts' / 'gsp.py'


class ConvergenceWiringTests(unittest.TestCase):
    def task(self):
        return {'id': 'T04', 'status': 'running', 'original_started_at': NOW.isoformat(),
                'execution_policy': {'requires_slices': True, 'slices': [
                    {'id': 'T04-G1', 'outcome': 'binding', 'checks': ['binding checks']},
                    {'id': 'T04-G2', 'outcome': 'recovery', 'checks': ['recovery checks'], 'deps': ['T04-G1']}]}}

    def test_plan_cannot_turn_declared_slice_into_completed_work(self):
        with self.assertRaisesRegex(ValueError, 'CANNOT_ACCEPT'):
            plan_policies('## T04: task\nConvergence: {"slices":[{"id":"G1","status":"accepted"}]}\n')

    def test_explicit_no_slices_requires_single_outcome_or_small_mode(self):
        with self.assertRaisesRegex(ValueError, 'single_outcome_required'):
            plan_policies(
                '## T04: task\nConvergence: {"requires_slices":false}\n',
            )
        policy = plan_policies(
            '## T04: task\nConvergence: {"requires_slices":false,"small":true}\n',
        )['T04']
        self.assertFalse(policy['requires_slices'])

    def test_plan_quality_only_requires_declarations_for_new_schema(self):
        from gsp.plan_quality import check_plan

        plan = '# Plan\n\n## T04: task\nOutcome: one result.\n'
        new_codes = {
            row['code'] for row in check_plan(
                plan, ['T04'], convergence_schema=1,
            )['findings']
        }
        legacy_codes = {
            row['code'] for row in check_plan(
                plan, ['T04'], convergence_schema=None,
            )['findings']
        }
        self.assertIn('CONVERGENCE_TRIAGE_REQUIRED', new_codes)
        self.assertNotIn('CONVERGENCE_TRIAGE_REQUIRED', legacy_codes)

    def test_stop_requires_bounded_controller_decision_and_does_not_reset_clock(self):
        task = self.task()
        at = (NOW + timedelta(minutes=90)).isoformat()
        with self.assertRaisesRegex(ValueError, 'SOL_CONVERGENCE'):
            start_guard(task, at)
        updated = apply_decision(task, {'action': 'continue', 'minutes': 10,
                                       'reason': 'exact crash window isolated', 'outcome': 'one recovery probe'}, at)
        verdict = start_guard(updated, at)
        self.assertEqual(verdict['minutes_since_accepted'], 90)
        with self.assertRaisesRegex(ValueError, 'SOL_CONVERGENCE'):
            start_guard(updated, (NOW + timedelta(minutes=101)).isoformat())
        self.assertEqual(updated['original_started_at'], task['original_started_at'])

    def test_checkpoint_cannot_reset_no_delivery_clock(self):
        at = (NOW + timedelta(minutes=89)).isoformat()
        updated = apply_decision(self.task(), {'action': 'checkpoint', 'summary': 'one known blocker'}, at)
        with self.assertRaisesRegex(ValueError, 'SOL_CONVERGENCE'):
            start_guard(updated, (NOW + timedelta(minutes=90)).isoformat())

    def test_internal_acceptance_keeps_parent_running_and_requires_dependencies(self):
        task = self.task()
        value = {'action': 'accept_slice', 'slice': 'T04-G2', 'candidate_digest': 'frozen',
                 'spec_review': 'spec.md', 'quality_review': 'quality.md'}
        with self.assertRaisesRegex(ValueError, 'DEPENDENCIES'):
            apply_decision(task, value, NOW.isoformat(), candidate_digest='frozen', evidence='receipt')
        value['slice'] = 'T04-G1'
        updated = apply_decision(task, value, NOW.isoformat(), candidate_digest='frozen', evidence='receipt')
        self.assertEqual(updated['status'], 'running')
        with self.assertRaisesRegex(ValueError, 'SLICES_INCOMPLETE'):
            complete_guard(updated)
        value['slice'] = 'T04-G2'
        complete_guard(apply_decision(updated, value, NOW.isoformat(), candidate_digest='frozen', evidence='receipt'))

    def test_candidate_drift_cannot_accept_slice(self):
        with self.assertRaisesRegex(ValueError, 'EVIDENCE_REQUIRED'):
            apply_decision(self.task(), {'action': 'accept_slice', 'slice': 'T04-G1',
                'candidate_digest': 'old', 'spec_review': 'spec', 'quality_review': 'quality'},
                NOW.isoformat(), candidate_digest='new', evidence='receipt')

    def test_replayed_acceptance_does_not_invent_a_new_delivery(self):
        value = {'action': 'accept_slice', 'slice': 'T04-G1', 'candidate_digest': 'frozen',
                 'spec_review': 'spec.md', 'quality_review': 'quality.md'}
        first = apply_decision(self.task(), value, NOW.isoformat(), candidate_digest='frozen', evidence='receipt')
        replayed = apply_decision(first, value, (NOW + timedelta(minutes=89)).isoformat(),
                                  candidate_digest='frozen', evidence='receipt')
        self.assertEqual(replayed['last_accepted_at'], first['last_accepted_at'])
        with self.assertRaisesRegex(ValueError, 'SOL_CONVERGENCE'):
            start_guard(replayed, (NOW + timedelta(minutes=90)).isoformat())


class SealPolicyTests(unittest.TestCase):
    def state(self, schema=1):
        value = {
            'tasks': [{
                'id': 'T01',
                'status': 'pending',
                'original_started_at': NOW.isoformat(),
                'repair_count': 2,
                'history': [{'event': 'existing-history'}],
            }],
        }
        if schema is not None:
            value['convergence_schema'] = schema
        return value

    def test_new_schema_requires_declared_slices_or_single_outcome(self):
        state = self.state()
        summary = seal_policies(state, '## T01: task\nOutcome: one change\n')
        self.assertEqual(summary['needs_triage'], ['T01'])
        self.assertEqual(
            state['tasks'][0]['convergence_status'],
            {'status': 'needs_triage', 'validation': 'NOT_READY'},
        )
        with self.assertRaisesRegex(ValueError, 'CONVERGENCE_TRIAGE'):
            start_guard(state['tasks'][0], NOW.isoformat(), convergence_schema=1)
        with self.assertRaisesRegex(ValueError, 'CONVERGENCE_TRIAGE'):
            complete_guard(state['tasks'][0], convergence_schema=1)

    def test_explicit_single_outcome_needs_no_child_session(self):
        state = self.state()
        summary = seal_policies(
            state,
            '## T01: task\n'
            'Convergence: {"requires_slices":false,"single_outcome":"one visible result"}\n',
        )
        self.assertEqual(summary['needs_triage'], [])
        self.assertEqual(state['tasks'][0]['convergence_status']['status'], 'declared')
        start_guard(state['tasks'][0], NOW.isoformat(), convergence_schema=1)
        complete_guard(state['tasks'][0], convergence_schema=1)

    def test_reseal_invalidates_only_superseded_acceptance(self):
        state = self.state()
        task = state['tasks'][0]
        task['execution_policy'] = {
            'requires_slices': True,
            'slices': [{'id': 'G1', 'outcome': 'old', 'checks': ['old']}],
        }
        task['accepted_slices'] = {
            'G1': {
                'candidate_digest': '1' * 64,
                'evidence': 'old-proof',
                'spec_review': 'spec',
                'quality_review': 'quality',
                'accepted_at': NOW.isoformat(),
            },
        }
        task['last_accepted_at'] = NOW.isoformat()
        original_started = task['original_started_at']
        original_history = list(task['history'])

        summary = seal_policies(
            state,
            '## T01: task\n'
            'Convergence: {"requires_slices":false,"single_outcome":"new result"}\n',
        )

        self.assertEqual(summary['invalidated'], ['T01'])
        self.assertEqual(task['accepted_slices'], {})
        self.assertNotIn('last_accepted_at', task)
        self.assertEqual(task['original_started_at'], original_started)
        self.assertEqual(task['history'], original_history)
        self.assertEqual(task['repair_count'], 2)
        self.assertEqual(task['convergence_history'][0]['accepted_slices']['G1']['evidence'],
                         'old-proof')

    def test_unchanged_policy_preserves_acceptance(self):
        state = self.state()
        task = state['tasks'][0]
        policy = {
            'requires_slices': True,
            'slices': [
                {'id': 'G1', 'outcome': 'old', 'checks': ['old']},
                {'id': 'G2', 'outcome': 'new', 'checks': ['new'], 'deps': ['G1']},
            ],
        }
        task['execution_policy'] = copy.deepcopy(policy)
        task['accepted_slices'] = {
            'G1': {'evidence': 'kept-1'},
            'G2': {'evidence': 'kept-2'},
        }
        task['last_accepted_at'] = NOW.isoformat()
        summary = seal_policies(
            state,
            '## T01: task\nConvergence: ' + json.dumps(policy, separators=(',', ':')) + '\n',
        )
        self.assertEqual(summary['invalidated'], [])
        self.assertEqual(
            task['accepted_slices'],
            {'G1': {'evidence': 'kept-1'}, 'G2': {'evidence': 'kept-2'}},
        )
        self.assertEqual(task['last_accepted_at'], NOW.isoformat())

    def test_legacy_without_schema_is_explicit_not_checked(self):
        state = self.state(schema=None)
        summary = seal_policies(state, '## T01: task\nOutcome: old behaviour\n')
        self.assertEqual(summary['tasks']['T01']['status'], 'legacy')
        self.assertEqual(
            state['tasks'][0]['convergence_status'],
            {'status': 'legacy', 'validation': 'NOT_CHECKED'},
        )
        start_guard(state['tasks'][0], NOW.isoformat())
        complete_guard(state['tasks'][0])

    def test_legacy_policy_removal_invalidates_acceptance_and_preserves_history(self):
        state = self.state(schema=None)
        task = state['tasks'][0]
        old_policy = {
            'requires_slices': True,
            'slices': [{'id': 'G1', 'outcome': 'old', 'checks': ['old']}],
        }
        task['execution_policy'] = copy.deepcopy(old_policy)
        task['accepted_slices'] = {
            'G1': {
                'candidate_digest': '1' * 64,
                'evidence': 'old-proof',
                'spec_review': 'spec',
                'quality_review': 'quality',
                'accepted_at': NOW.isoformat(),
            },
        }
        task['last_accepted_at'] = NOW.isoformat()
        task['convergence_decision'] = {'action': 'continue'}
        original_started = task['original_started_at']
        original_history = copy.deepcopy(task['history'])

        summary = seal_policies(state, '## T01: task\nOutcome: old behaviour\n')

        self.assertEqual(summary['invalidated'], ['T01'])
        self.assertNotIn('execution_policy', task)
        self.assertEqual(task['accepted_slices'], {})
        self.assertNotIn('last_accepted_at', task)
        self.assertNotIn('convergence_decision', task)
        self.assertEqual(
            task['convergence_status'],
            {'status': 'legacy', 'validation': 'NOT_CHECKED'},
        )
        self.assertEqual(task['original_started_at'], original_started)
        self.assertEqual(task['history'], original_history)
        self.assertEqual(task['repair_count'], 2)
        self.assertEqual(
            task['convergence_history'][0]['accepted_slices']['G1']['evidence'],
            'old-proof',
        )
        start_guard(task, NOW.isoformat())
        complete_guard(task)


class ConvergenceCliGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-convergence-cli-')
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()
        self.context = None

    def cli_env(self):
        env = os.environ.copy()
        env.pop('GSP_SESSION_CONTEXT', None)
        if self.context is not None:
            env['GSP_SESSION_CONTEXT'] = str(self.context)
        env.update({
            'HOME': self.tmp.name,
            'USERPROFILE': self.tmp.name,
            'CODEX_HOME': str(Path(self.tmp.name) / '.codex'),
        })
        return env

    def cli(self, *args, ok=True):
        result = run(
            [sys.executable, str(CLI), '--project', str(self.project), *args],
            text=True, encoding='utf-8', capture_output=True, env=self.cli_env(),
        )
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def prepare(self, schema, plan):
        self.cli('init')
        self.cli('new', 'login', '--title', 'Login')
        state_file = self.project / '.gsp/runs/login/state.json'
        state = json.loads(state_file.read_text(encoding='utf-8'))
        self.assertEqual(state.get('convergence_schema'), 1)
        binding = session_binding(
            self.project, state['work_uuid'], 'main', 'codex', 'convergence-main',
        )
        self.context = self.project / binding.handle_path
        state.pop('controller_required', None)
        state.pop('git_topology', None)
        state.pop('_command_commit', None)
        if schema is None:
            state.pop('convergence_schema', None)
        else:
            state['convergence_schema'] = schema
        state_file.write_text(json.dumps(state), encoding='utf-8')
        document(self.project, 'login', 'design').write_text(
            '# Design\nLogin returns one session.\n', encoding='utf-8',
        )
        self.cli('approve', 'login', '--quote', 'Approve fixture design')
        self.cli(
            'task-add', 'login', 'T1', '--title', 'Implement login',
            '--acceptance', 'Login tests pass',
        )
        document(self.project, 'login', 'plan').write_text(plan, encoding='utf-8')
        return state_file

    def task(self, state_file):
        state = json.loads(state_file.read_text(encoding='utf-8'))
        return state, state['tasks'][0]

    def test_new_schema_without_declaration_blocks_real_task_start(self):
        state_file = self.prepare(1, '# Plan\n\n## T1: Implement login\nOutcome: login works.\n')
        self.cli('seal-plan', 'login')
        state, task = self.task(state_file)
        self.assertEqual(task['execution_policy']['status'], 'needs_triage')
        before = state_file.read_bytes()
        result = self.cli('task-start', 'login', 'T1', ok=False)
        self.assertIn('CONVERGENCE_TRIAGE_REQUIRED', result.stderr)
        self.assertEqual(state_file.read_bytes(), before)
        self.assertEqual(task['status'], 'pending')

    def test_explicit_single_outcome_starts_without_child_session(self):
        state_file = self.prepare(
            1,
            '# Plan\n\n## T1: Implement login\n'
            'Convergence: {"requires_slices":false,"single_outcome":"login works"}\n',
        )
        self.cli('seal-plan', 'login')
        self.cli('task-start', 'login', 'T1')
        _, task = self.task(state_file)
        self.assertEqual(task['status'], 'running')
        self.assertEqual(task['convergence_status']['status'], 'declared')

    def test_legacy_ledger_is_labeled_not_checked_and_remains_runnable(self):
        state_file = self.prepare(
            None,
            '# Plan\n\n## T1: Implement login\nOutcome: legacy login works.\n',
        )
        self.cli('seal-plan', 'login')
        self.cli('task-start', 'login', 'T1')
        _, task = self.task(state_file)
        self.assertEqual(task['status'], 'running')
        self.assertEqual(
            task['convergence_status'],
            {'status': 'legacy', 'validation': 'NOT_CHECKED'},
        )

    def test_incomplete_slices_block_real_task_done_without_mutation(self):
        state_file = self.prepare(
            1,
            '# Plan\n\n## T1: Implement login\n'
            'Convergence: {"requires_slices":true,"slices":['
            '{"id":"G1","outcome":"binding","checks":["unit"]},'
            '{"id":"G2","outcome":"recovery","checks":["recovery"],"deps":["G1"]}]}\n',
        )
        self.cli('seal-plan', 'login')
        self.cli('task-start', 'login', 'T1')
        before = state_file.read_bytes()
        result = self.cli('task-done', 'login', 'T1', ok=False)
        self.assertIn('TASK_SLICES_INCOMPLETE', result.stderr)
        self.assertEqual(state_file.read_bytes(), before)
        _, task = self.task(state_file)
        self.assertEqual(task['status'], 'running')

    def test_review_scope_and_issue_count_are_enforced_by_real_cli(self):
        from gsp.runtime import Project, contract_hash, fingerprint
        state_file = self.prepare(1, '# Plan\n\n## T1: Implement login\n'
            'Convergence: {"requires_slices":false,"single_outcome":"login works"}\n')
        self.cli('seal-plan', 'login')
        project = Project(self.project)
        state = project.ledger_state('login')
        report = project.paths('login')[1] / 'review.md'
        candidate = fingerprint(project.verification_root(state))
        contract = contract_hash(project, state, project.paths('login')[1])
        finding = {'confirmed': True, 'kind': 'authorization_bypass',
                   'acceptance_key': 'login-owner', 'root_cause_key': 'spare-owner',
                   'signature': 'spare controller writes', 'severity': 'P1'}

        def write(findings):
            value = {'candidate_digest': candidate, 'contract_digest': contract, 'findings': findings}
            report.write_text('# Review\nIndependent specification and quality review.\n'
                '<!-- gsp-review-evidence\n' + json.dumps(value) + '\n-->\n', encoding='utf-8')

        write([finding])
        self.assertIn('REVIEW_BLOCKING_COUNT_MISMATCH', self.cli(
            'review', 'login', '--reviewer', 'self', '--blocking', '0', ok=False).stderr)
        self.cli('review', 'login', '--reviewer', 'self', '--blocking', '1')
        state = json.loads(state_file.read_text(encoding='utf-8'))
        uid = next(iter(state['issues']))
        self.cli('decision', 'login', '--source', 'agent', '--text', json.dumps({'issue': {'uid': uid, 'action': 'worker_repair'}}))
        write([])
        self.assertIn('REVIEW_BLOCKING_COUNT_MISMATCH', self.cli(
            'review', 'login', '--reviewer', 'self', '--blocking', '0', ok=False).stderr)
        state = json.loads(state_file.read_text(encoding='utf-8'))
        self.assertEqual(state['issues'][uid]['repair_count'], 1)

    def test_independent_label_requires_a_real_separate_reviewer_binding(self):
        from gsp.runtime import Project, contract_hash, fingerprint
        state_file = self.prepare(1, '# Plan\n\n## T1: Implement login\n'
            'Convergence: {"requires_slices":false,"single_outcome":"login works"}\n')
        self.cli('seal-plan', 'login')
        project = Project(self.project)
        state = project.ledger_state('login')
        report = project.paths('login')[1] / 'review.md'
        value = {'candidate_digest': fingerprint(project.verification_root(state)),
                 'contract_digest': contract_hash(project, state, project.paths('login')[1]), 'findings': []}

        def write():
            report.write_text('# Review\nIndependent specification and quality review.\n'
                '<!-- gsp-review-evidence\n' + json.dumps(value) + '\n-->\n', encoding='utf-8')

        write()
        before = state_file.read_bytes()
        result = self.cli('review', 'login', '--reviewer', 'independent', '--blocking', '0', ok=False)
        self.assertIn('INDEPENDENT_REVIEW_REQUIRED', result.stderr)
        self.assertEqual(state_file.read_bytes(), before)
        reviewer = session_binding(self.project, state['work_uuid'], kind='reviewer',
            thread='independent-reviewer-thread', capabilities={'report'})
        value['reviewer_context'] = reviewer.handle_path
        write()
        self.cli('review', 'login', '--reviewer', 'independent', '--blocking', '0')
        proof = json.loads(state_file.read_text(encoding='utf-8'))['review']['independent_proof']
        self.assertEqual(proof['binding_uid'], reviewer.binding_uid)
        self.assertEqual(proof['independence'], 'verified-context')
        state = project.ledger_state('login')
        state['cancel_generation'] = 2
        project.write(state, 'fixture', 'advance cancellation fence')
        before = state_file.read_bytes()
        result = self.cli('review', 'login', '--reviewer', 'independent', '--blocking', '0', ok=False)
        self.assertIn('INDEPENDENT_REVIEW_REQUIRED', result.stderr)
        self.assertEqual(state_file.read_bytes(), before)
