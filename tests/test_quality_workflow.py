"""Bound quality observes real source drift and retains unknown execution."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from gsp.core import WorkflowError
from gsp.runtime import Project, dispatch, fingerprint, parser
from gsp.quality_workflow import run_work_quality, quality_build_context, recover_work_quality
from gsp.code_quality import evaluate_required_quality


@contextmanager
def unlocked(state, run_id):
    yield


class BoundQualityWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gsp-bound-quality-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = Project(self.root)
        self.project.initialize()
        dispatch(self.project, parser().parse_args(['new', 'work', '--title', 'quality fixture']))
        state = self.project.ledger_state('work')
        self.project.document(state, 'plan').write_text('# Plan\nOne result.\n', encoding='utf-8')
        self.script = self.root / 'check.py'
        self.script.write_text("print('quality pass')\n", encoding='utf-8')
        (self.root / '.gsp/quality.json').write_text(json.dumps({'schema': 1, 'checks': [
            {'id': 'unit', 'argv': [sys.executable, '-B', 'check.py'], 'kind': 'build',
             'modes': ['release'], 'reuse_evidence': True, 'inputs': ['check.py']}
        ]}), encoding='utf-8')

    def run_quality(self, run_id):
        args = parser().parse_args(['quality', '--mode', 'release', '--task', 'work', '--run-id', run_id])
        return run_work_quality(self.project, args, measure=fingerprint, unlocked=unlocked)

    def test_release_report_is_current_and_consumed_by_ready_provider(self):
        result = self.run_quality('positive')
        self.assertEqual(result['status'], 'pass')
        state = self.project.ledger_state('work')
        self.assertNotIn('inflight', state)
        _, env = quality_build_context(self.project, state)
        self.assertEqual(evaluate_required_quality(self.project, state, fingerprint(self.root), env=env), [])

    def test_second_equivalent_request_reuses_result_with_a_distinct_journal_action(self):
        self.run_quality('first-request')
        with patch('gsp.code_quality.execute_command') as execute:
            result = self.run_quality('second-request')
        self.assertEqual(result['status'], 'pass')
        self.assertTrue(result['checks'][0]['reused'])
        execute.assert_not_called()
        self.assertNotIn('inflight', self.project.ledger_state('work'))

    def test_index_failure_keeps_failed_original_inflight_until_barrier_is_repaired(self):
        from gsp.evidence import EvidenceCache, QualityReportLocator
        config = self.root / '.gsp/quality.json'
        stack = json.loads(config.read_text(encoding='utf-8'))
        stack['checks'][0]['reuse_evidence'] = False
        config.write_text(json.dumps(stack), encoding='utf-8')
        self.script.write_text("from pathlib import Path\nimport sys\n"
            "sys.exit(1 if Path('.gsp/fail-next').exists() else 0)\n", encoding='utf-8')
        self.run_quality('original-pass')
        (self.root / '.gsp/fail-next').touch()
        with patch.object(EvidenceCache, 'publish', return_value=False), \
                patch.object(QualityReportLocator, 'publish', return_value=False):
            with self.assertRaisesRegex(WorkflowError, 'PERSISTENCE_UNKNOWN'):
                self.run_quality('later-failure')
        state = self.project.ledger_state('work')
        self.assertEqual(state['inflight']['id'], 'later-failure')
        _, env = quality_build_context(self.project, state)
        self.assertTrue(evaluate_required_quality(self.project, state, fingerprint(self.root), env=env))
        with patch('gsp.quality_workflow.run_quality') as execute:
            with patch.object(EvidenceCache, 'publish', return_value=False):
                with self.assertRaisesRegex(WorkflowError, 'PERSISTENCE_UNKNOWN'):
                    recover_work_quality(self.project, state, measure=fingerprint)
            self.assertIn('inflight', self.project.ledger_state('work'))
            recovered, ok = recover_work_quality(self.project, self.project.ledger_state('work'), measure=fingerprint)
        execute.assert_not_called()
        self.assertTrue(ok)
        self.assertFalse(recovered['quality_valid'])
        state = self.project.ledger_state('work')
        self.assertNotIn('inflight', state)
        self.assertEqual(state['status'], 'needs-verification')
        self.assertTrue(evaluate_required_quality(self.project, state, fingerprint(self.root), env=env))

    def test_process_source_mutation_invalidates_whole_bound_report(self):
        self.script.write_text("from pathlib import Path\nPath('new-source.py').write_text('changed')\n", encoding='utf-8')
        result = self.run_quality('mutates')
        self.assertEqual(result['status'], 'fail')
        self.assertEqual(result['binding_status'], 'invalidated')
        self.assertNotIn('inflight', self.project.ledger_state('work'))
        self.assertTrue(evaluate_required_quality(self.project, self.project.ledger_state('work'), fingerprint(self.root)))

    def test_unknown_execution_preserves_inflight_and_does_not_blindly_relaunch(self):
        with patch('gsp.quality_workflow.run_quality', return_value={
                'checks': [{'execution_result': None}], 'status': 'blocked'}):
            with self.assertRaisesRegex(WorkflowError, 'QUALITY_EXECUTION_UNKNOWN'):
                self.run_quality('unknown')
        before = (self.root / '.gsp/runs/work/state.json').read_bytes()
        self.assertEqual(self.project.ledger_state('work')['inflight']['id'], 'unknown')
        with patch('gsp.quality_workflow.run_quality') as execute:
            with self.assertRaisesRegex(WorkflowError, 'EXECUTION_UNKNOWN'):
                self.run_quality('second')
        execute.assert_not_called()
        self.assertEqual((self.root / '.gsp/runs/work/state.json').read_bytes(), before)
        with self.assertRaisesRegex(WorkflowError, 'QUALITY_EXECUTION_UNKNOWN'):
            recover_work_quality(self.project, self.project.ledger_state('work'), measure=fingerprint)
        self.assertEqual((self.root / '.gsp/runs/work/state.json').read_bytes(), before)

    def test_response_loss_claims_original_quality_without_rerunning_process(self):
        from gsp.code_quality import quality_contract
        self.run_quality('completed')
        state = self.project.ledger_state('work')
        state['inflight'] = {'id': 'completed', 'kind': 'quality', 'checks': ['unit'],
            'source_fingerprint': fingerprint(self.root), 'contract': quality_contract(self.project, state)}
        self.project.write(state, 'fixture', 'response lost')
        with patch('gsp.quality_workflow.run_quality') as execute:
            result, success = recover_work_quality(self.project, state, measure=fingerprint)
        execute.assert_not_called()
        self.assertTrue(success)
        self.assertTrue(result['quality_valid'])
        self.assertFalse(result['accepted'])
        self.assertNotIn('inflight', self.project.ledger_state('work'))

    def test_empty_quality_recovery_uses_canonical_not_checked_report(self):
        from gsp.code_quality import quality_contract
        (self.root / '.gsp/quality.json').write_text(
            json.dumps({'schema': 1, 'checks': []}),
            encoding='utf-8',
        )
        normal = self.run_quality('empty')
        self.assertEqual(normal['status'], 'not_checked')
        state = self.project.ledger_state('work')
        context, _ = quality_build_context(self.project, state)
        state['inflight'] = {
            'id': 'empty',
            'task': 'all',
            'kind': 'quality',
            'phase': 'command',
            'report': '.gsp/runs/work/quality/empty/report.json',
            'checks': [],
            'source_fingerprint': fingerprint(self.root),
            'contract': quality_contract(self.project, state),
            'mode': 'release',
            'config': None,
            'build_context': context,
        }
        self.project.write(state, 'fixture', 'empty response lost')
        with patch('gsp.quality_workflow.run_quality') as execute:
            result, success = recover_work_quality(
                self.project,
                self.project.ledger_state('work'),
                measure=fingerprint,
            )
        execute.assert_not_called()
        self.assertTrue(success)
        self.assertEqual(result['quality_status'], 'not_checked')
        self.assertFalse(result['quality_valid'])
        self.assertFalse(result['accepted'])
        self.assertEqual(result['evidence_class'], 'zero-execution')
        recovered = self.project.ledger_state('work')
        self.assertNotIn('inflight', recovered)
        self.assertEqual(
            recovered['last_recovery']['original_request']['checks'],
            [],
        )
        self.assertEqual(
            recovered['last_recovery']['evidence_class'],
            'zero-execution',
        )
        self.assertEqual(
            recovered['last_recovery']['quality_status'],
            'not_checked',
        )

    def test_empty_quality_without_canonical_report_remains_inflight(self):
        from gsp.code_quality import quality_contract
        state = self.project.ledger_state('work')
        state['inflight'] = {
            'id': 'missing-empty-report',
            'kind': 'quality',
            'checks': [],
            'source_fingerprint': fingerprint(self.root),
            'contract': quality_contract(self.project, state),
            'build_context': {},
        }
        self.project.write(state, 'fixture', 'empty report unknown')
        before = (self.root / '.gsp/runs/work/state.json').read_bytes()
        with self.assertRaisesRegex(
            WorkflowError,
            'zero-execution report is unavailable',
        ):
            recover_work_quality(
                self.project,
                self.project.ledger_state('work'),
                measure=fingerprint,
            )
        self.assertEqual(
            (self.root / '.gsp/runs/work/state.json').read_bytes(),
            before,
        )

    def test_quality_recovery_distinguishes_missing_or_non_list_checks(self):
        for checks, run_id in ((None, 'missing-checks'), ('unit', 'bad-checks')):
            state = self.project.ledger_state('work')
            state.pop('inflight', None)
            pending = {'id': run_id, 'kind': 'quality'}
            if checks is not None:
                pending['checks'] = checks
            state['inflight'] = pending
            self.project.write(state, run_id, run_id)
            before = (self.root / '.gsp/runs/work/state.json').read_bytes()
            with self.subTest(checks=checks):
                with self.assertRaisesRegex(
                    WorkflowError,
                    'original command set is unavailable',
                ):
                    recover_work_quality(
                        self.project,
                        self.project.ledger_state('work'),
                        measure=fingerprint,
                    )
                self.assertEqual(
                    (self.root / '.gsp/runs/work/state.json').read_bytes(),
                    before,
                )

    def test_quality_recovery_rejects_incomplete_lease_identities(self):
        from gsp.code_quality import quality_contract
        (self.root / '.gsp/quality.json').write_text(
            json.dumps({'schema': 1, 'checks': []}),
            encoding='utf-8',
        )
        self.run_quality('incomplete-lease')
        state = self.project.ledger_state('work')
        lease_root = self.root / '.gsp/build/quality-recovery-lease'
        context = {'root': str(lease_root)}
        lease = lease_root / '.gsp-build-lease.json'
        lease.parent.mkdir(parents=True, exist_ok=True)
        lease.write_text(json.dumps({
            'evidence_id': 'incomplete-lease',
            'phase': 'running',
            'controller': {},
            'child': {},
        }), encoding='utf-8')
        state['inflight'] = {
            'id': 'incomplete-lease',
            'task': 'all',
            'kind': 'quality',
            'report': '.gsp/runs/work/quality/incomplete-lease/report.json',
            'checks': [],
            'source_fingerprint': fingerprint(self.root),
            'contract': quality_contract(self.project, state),
            'mode': 'release',
            'build_context': context,
        }
        self.project.write(state, 'fixture', 'incomplete lease')
        before = (self.root / '.gsp/runs/work/state.json').read_bytes()
        with self.assertRaisesRegex(
            WorkflowError,
            'controller identity is unavailable',
        ):
            recover_work_quality(
                self.project,
                self.project.ledger_state('work'),
                measure=fingerprint,
            )
        self.assertEqual(
            (self.root / '.gsp/runs/work/state.json').read_bytes(),
            before,
        )
        self.assertTrue(lease.is_file())
