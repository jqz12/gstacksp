"""Evidence validity is content-addressed; verification_epoch is a quarantine boundary."""
import sys
import tempfile
import unittest
from pathlib import Path

from gsp.runtime import Project, WorkflowError, dispatch, evidence_for, parser, read
from tests.process_helpers import run


class EvidenceContentValidityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-evidence-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = Project(self.root)
        self.project.initialize()

    def cmd(self, *argv):
        args = parser().parse_args(list(argv[:argv.index('--')])) if '--' in argv else parser().parse_args(list(argv))
        if '--' in argv:
            args.argv = list(argv[argv.index('--') + 1:])
        result, ok = dispatch(self.project, args)
        self.assertTrue(ok, result)
        return result

    def git(self, *argv):
        result = run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                      '-c', 'core.hooksPath=', *argv], cwd=self.root,
                     capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def state(self):
        return read(self.root / '.gsp/runs/work/state.json')

    def setup_git(self):
        self.git('init', '-q', '-b', 'main')
        (self.root / 'source.txt').write_text('baseline', encoding='utf-8')
        self.git('add', 'source.txt', '.gitignore')
        self.git('commit', '-qm', 'baseline')

    def prepare(self):
        self.cmd('new', 'work', '--title', '证据内容自证', '--intent', 'single-task')
        state = self.state()
        # This published evidence-validity fixture predates the convergence schema.
        # Keep the product gate intact; only this synthetic work is legacy.
        state.pop('convergence_schema', None)
        self.project.write(
            state,
            'fixture-legacy-convergence',
            'legacy evidence-validity fixture created before convergence schema',
            action_uid='fixture:legacy-convergence:evidence-content-validity',
        )
        self.project.document(state, 'design').write_text('# Design\nFixture evidence content validity.', encoding='utf-8')
        self.cmd('approve', 'work', '--quote', 'SYNTHETIC fixture approved')
        self.cmd('task-add', 'work', 'T01', '--title', 'Implementation T01', '--acceptance', 'Checks pass')
        self.cmd('task-add', 'work', 'T02', '--title', 'Implementation T02', '--acceptance', 'Checks pass')
        self.project.document(state, 'plan').write_text('# Plan\nT01 and T02 implementation and checks.', encoding='utf-8')
        self.cmd('seal-plan', 'work')
        self.cmd('scope', 'work', '--off', '--quote', 'SYNTHETIC no freeze')
        self.cmd('topology-select', 'work', '--mode', 'in-place', '--quote', 'SYNTHETIC control writer')
        self.cmd('advance', 'work', '--to', 'execute')
        return state

    def check(self, task='T01'):
        workspace = self.project.workspace_root(self.state(), task_id=task)
        program = workspace / '.gsp/fixture_test.py'
        program.parent.mkdir(exist_ok=True)
        program.write_text('import unittest\nclass Check(unittest.TestCase):\n'
                           ' def test_arithmetic(self): self.assertEqual(2 + 2, 4)\n'
                           'unittest.main()\n', encoding='utf-8')
        return self.cmd('run', 'work', '--task', task, '--kind', 'tests', '--', sys.executable, str(program))

    def done(self, task):
        self.cmd('task-start', 'work', task)
        self.check(task)
        self.cmd('task-done', 'work', task)

    def test_reopen_keeps_sibling_receipt(self):
        self.setup_git()
        self.prepare()
        self.done('T01')
        self.done('T02')
        self.cmd('task-reopen', 'work', 'T02', '--reason', 'fixture rework of the sibling task')
        state = self.state()
        evidence_for(self.project, state, 'T01')
        self.assertEqual(state.get('verification_epoch', 0), 0)

    def test_recover_quarantine_and_recovery(self):
        self.setup_git()
        self.prepare()
        self.done('T01')
        state = self.state()
        state['inflight'] = {'id': 'fixture-run', 'kind': 'check', 'task': 'T01', 'at': '2026-11-22T00:00:00+00:00'}
        # Simulate a durable unknown execution without bypassing the journal.
        self.project.write(
            state,
            'fixture-unknown-execution',
            'preserve an unknown execution for recovery',
            action_uid='fixture:evidence-content-validity:unknown-execution',
        )
        report = self.project.paths('work')[1] / 'recovery-fixture.md'
        report.write_text('# Recovery\nFixture unknown execution investigated as failed.', encoding='utf-8')
        self.cmd('recover-run', 'work', '--evidence', str(report), '--outcome', 'failed')
        state = self.state()
        self.assertEqual(state.get('verification_epoch', 0), 1)
        with self.assertRaisesRegex(WorkflowError, 'EVIDENCE_BEFORE_RECOVERY'):
            evidence_for(self.project, state, 'T01')
        self.cmd('task-reopen', 'work', 'T01', '--reason', 'fixture re-verify after unknown execution')
        self.cmd('task-start', 'work', 'T01')
        self.check('T01')
        self.cmd('task-done', 'work', 'T01')
        evidence_for(self.project, self.state(), 'T01')

    def test_documents_adopt_invalidates_by_contract(self):
        self.setup_git()
        self.prepare()
        self.done('T01')
        alt = self.root / 'docs' / 'work' / 'alt'
        alt.mkdir(parents=True)
        (alt / 'design.md').write_text('# Design\nAlternate binding for adopt fixture.', encoding='utf-8')
        (alt / 'plan.md').write_text('# Plan\nAlternate plan for adopt fixture.', encoding='utf-8')
        self.cmd('documents-adopt', 'work', '--spec', 'docs/work/alt/design.md', '--plan', 'docs/work/alt/plan.md',
                 '--reason', 'fixture adopt alternate documents')
        state = self.state()
        self.assertEqual(state.get('verification_epoch', 0), 0)
        with self.assertRaisesRegex(WorkflowError, 'EVIDENCE_CONTRACT_CHANGED'):
            evidence_for(self.project, state, 'T01')

    def test_topology_rebind_invalidates_by_workspace(self):
        self.setup_git()
        self.prepare()
        self.done('T01')
        branch = 'feature/' + self.state()['number'] + '-rebind-alt'
        self.cmd('topology-select', 'work', '--mode', 'worktree', '--branch', branch, '--rebind',
                 '--quote', 'SYNTHETIC rebind fixture')
        state = self.state()
        self.assertEqual(state.get('verification_epoch', 0), 0)
        with self.assertRaisesRegex(WorkflowError, 'EVIDENCE_WORKSPACE_CHANGED'):
            evidence_for(self.project, state, 'T01')


if __name__ == '__main__':
    unittest.main()
