"""Final candidate checks share one execution only when equivalence is declared."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import gsp.command_execution as commands
from gsp.code_quality import evaluate_required_quality
from gsp.quality_workflow import quality_build_context, run_work_quality
from gsp.runtime import Project, dispatch, fingerprint, parser, run_check


@contextmanager
def unlocked(state, run_id):
    yield


class RunQualityReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-final-reuse-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = Project(self.root)
        self.project.initialize()
        dispatch(self.project, parser().parse_args(['new', 'work', '--title', 'shared check']))
        self.argv = [sys.executable, '-B', '-m', 'unittest', '-v', 'test_example.py']
        (self.root / 'test_example.py').write_text(
            'import unittest\nclass Example(unittest.TestCase):\n'
            '    def test_add(self): self.assertEqual(1 + 1, 2)\n', encoding='utf-8')
        (self.root / '.gsp/build-policy.json').write_text(json.dumps({
            'version': 1, 'temp_isolation': False, 'worktree_build': 'off'}), encoding='utf-8')
        self.stack = {'schema': 1, 'checks': [{
            'id': 'unit', 'argv': self.argv, 'kind': 'test', 'modes': ['release'],
            'required': True, 'reuse_evidence': True, 'inputs': ['test_example.py']}]}
        self.write_stack()
        state = self.project.ledger_state('work')
        self.project.document(state, 'plan').write_text(
            '# Plan\nOne addition result.\n\n## Task T01\n'
            'Verify the addition result using the configured unit command.\n'
            '**Writes:** {"paths":["test_example.py"]}\n', encoding='utf-8')
        state.pop('convergence_schema', None)
        state['tasks'] = [{'id': 'T01', 'title': 'addition', 'acceptance': 'addition works',
                           'status': 'done', 'depends': []}]
        state['git_topology'] = {'enforce': False}
        from gsp.contract_store import publish_contracts
        publish_contracts(self.project, state)
        self.assertEqual(2, state['operation_schema'])
        self.project.write(state, 'fixture', 'final candidate')

    def write_stack(self):
        (self.root / '.gsp/quality.json').write_text(json.dumps(self.stack), encoding='utf-8')

    def check(self):
        state = self.project.ledger_state('work')
        args = parser().parse_args(['run', 'work', '--task', 'all', '--kind', 'tests'])
        args.argv = self.argv
        receipt, ok = run_check(self.project, state, self.project.paths('work')[0], args)
        self.assertTrue(ok, receipt)
        return receipt

    def quality(self, run_id):
        result = run_work_quality(self.project, parser().parse_args([
            'quality', '--mode', 'release', '--task', 'work', '--run-id', run_id]),
            measure=fingerprint, unlocked=unlocked)
        self.assertEqual(result['status'], 'pass', result)
        return result

    def test_run_then_quality_then_repeat_starts_one_process(self):
        with patch('gsp.command_execution.execute_command', wraps=commands.execute_command) as execute, \
                patch('gsp.code_quality.execute_command', side_effect=lambda *a, **kw: execute(*a, **kw)):
            first = self.check()
            quality = self.quality('after-run')
            repeated = self.check()
        self.assertEqual(execute.call_count, 1)
        self.assertTrue(quality['checks'][0]['reused'])
        self.assertTrue(repeated['reused'])
        self.assertNotEqual(first['id'], repeated['id'])
        _, env = quality_build_context(self.project, self.project.ledger_state('work'))
        self.assertEqual(evaluate_required_quality(self.project, self.project.ledger_state('work'),
                                                  fingerprint(self.root), env=env), [])

    def test_quality_then_final_run_reuses_verified_original_logs(self):
        with patch('gsp.command_execution.execute_command', wraps=commands.execute_command) as execute, \
                patch('gsp.code_quality.execute_command', side_effect=lambda *a, **kw: execute(*a, **kw)):
            original = self.quality('before-run')
            receipt = self.check()
        self.assertEqual(execute.call_count, 1)
        self.assertTrue(receipt['reused'])
        self.assertEqual(receipt['execution_result'], original['checks'][0]['execution_result'])

    def test_unknown_closure_does_not_reuse_even_with_a_source_fingerprint(self):
        self.stack['checks'][0]['reuse_evidence'] = False
        self.write_stack()
        with patch('gsp.command_execution.execute_command', wraps=commands.execute_command) as execute, \
                patch('gsp.code_quality.execute_command', side_effect=lambda *a, **kw: execute(*a, **kw)):
            self.check()
            self.quality('unknown-one')
            self.quality('unknown-two')
        self.assertEqual(execute.call_count, 3)

    def test_precompletion_quality_is_not_a_final_candidate_execution(self):
        state = self.project.ledger_state('work')
        state['tasks'][0]['status'] = 'running'
        self.project.write(state, 'fixture', 'task still running', action_uid='fixture:running')
        with patch('gsp.command_execution.execute_command', wraps=commands.execute_command) as execute, \
                patch('gsp.code_quality.execute_command', side_effect=lambda *a, **kw: execute(*a, **kw)):
            self.quality('task-phase')
            state = self.project.ledger_state('work')
            state['tasks'][0]['status'] = 'done'
            self.project.write(state, 'fixture', 'task complete', action_uid='fixture:done')
            final = self.check()
        self.assertEqual(execute.call_count, 2)
        self.assertFalse(final['reused'])

    def test_explicit_metadata_input_changes_identity_without_changing_source_fingerprint(self):
        from gsp.evidence import declared_inputs
        folder = self.root / '.gsp/fixtures'
        folder.mkdir()
        fixture = folder / 'value.json'
        fixture.write_text('{"value": 1}', encoding='utf-8')
        fp = fingerprint(self.root)
        for declaration in (['test_example.py', '.gsp/fixtures/value.json'],
                            ['test_example.py', '.gsp/fixtures']):
            before = declared_inputs(self.root, declaration, self.argv)
            self.assertIn('.gsp/fixtures/value.json', before)
            fixture.write_text('{"value": 2}', encoding='utf-8')
            self.assertNotEqual(before, declared_inputs(self.root, declaration, self.argv))
            self.assertEqual(fp, fingerprint(self.root))
            fixture.write_text('{"value": 1}', encoding='utf-8')

    def test_run_publication_failure_retains_inflight_and_blocks_delivery(self):
        from gsp.evidence import EvidenceCache
        from gsp.core import WorkflowError
        with patch.object(EvidenceCache, 'publish', return_value=False):
            with self.assertRaisesRegex(WorkflowError, 'PERSISTENCE_UNKNOWN'):
                self.check()
        self.assertIn('inflight', self.project.ledger_state('work'))

    def test_direct_git_metadata_argument_refuses_a_closed_input_claim(self):
        from gsp.evidence import declared_inputs
        (self.root / '.git').mkdir()
        head = self.root / '.git/HEAD'
        for value in ('ref: refs/heads/main', 'ref: refs/heads/other'):
            head.write_text(value, encoding='utf-8')
            self.assertIsNone(declared_inputs(self.root, ['test_example.py'],
                [sys.executable, 'test_example.py', '.git/HEAD']))

    def test_directory_link_in_explicit_inputs_is_not_a_closed_input_set(self):
        from gsp.evidence import declared_inputs
        folder = self.root / 'fixtures'
        folder.mkdir()
        try:
            (folder / 'alias').symlink_to(self.root, target_is_directory=True)
        except OSError as exc:
            self.skipTest('OS cannot create the fixture link: ' + str(exc))
        self.assertIsNone(declared_inputs(self.root, ['test_example.py', 'fixtures']))


if __name__ == '__main__':
    unittest.main()
