"""Reopening writes a mechanical revalidation queue that revalidate consumes in order."""
import sys
import tempfile
import unittest
from pathlib import Path

from gsp.runtime import Project, WorkflowError, dispatch, parser, read, validate_delivery
from tests.process_helpers import run


class RevalidationQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-revalidation-')
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
        self.cmd('new', 'work', '--title', '机械重验队列', '--intent', 'single-task')
        state = self.state()
        # This published revalidation fixture predates the convergence schema.
        # Keep the product gate intact; only this synthetic work is legacy.
        state.pop('convergence_schema', None)
        self.project.write(
            state,
            'fixture-legacy-convergence',
            'legacy revalidation fixture created before convergence schema',
            action_uid='fixture:legacy-convergence:revalidation-queue',
        )
        self.project.document(state, 'design').write_text('# Design\nFixture revalidation queue.', encoding='utf-8')
        self.cmd('approve', 'work', '--quote', 'SYNTHETIC fixture approved')
        self.cmd('task-add', 'work', 'T01', '--title', 'Implementation T01', '--acceptance', 'Checks pass')
        self.cmd('task-add', 'work', 'T02', '--title', 'Implementation T02', '--acceptance', 'Checks pass',
                 '--depends', 'T01')
        self.project.document(state, 'plan').write_text('# Plan\nT01 and dependent T02 implementation and checks.', encoding='utf-8')
        self.cmd('seal-plan', 'work')
        self.cmd('scope', 'work', '--off', '--quote', 'SYNTHETIC no freeze')
        self.cmd('topology-select', 'work', '--mode', 'in-place', '--quote', 'SYNTHETIC control writer')
        self.cmd('advance', 'work', '--to', 'execute')
        return state

    def program(self):
        workspace = self.project.workspace_root(self.state())
        program = workspace / '.gsp/fixture_test.py'
        program.parent.mkdir(exist_ok=True)
        program.write_text('import unittest\nclass Check(unittest.TestCase):\n'
                           ' def test_arithmetic(self): self.assertEqual(2 + 2, 4)\n'
                           'unittest.main()\n', encoding='utf-8')
        return program

    def check(self, task='T01'):
        program = self.program()
        return self.cmd('run', 'work', '--task', task, '--kind', 'tests', '--', sys.executable, str(program))

    def done(self, task):
        self.cmd('task-start', 'work', task)
        self.check(task)
        self.cmd('task-done', 'work', task)

    def test_reopen_writes_queue(self):
        self.setup_git()
        self.prepare()
        self.done('T01')
        self.done('T02')
        self.cmd('task-reopen', 'work', 'T01', '--reason', 'fixture rework with queue')
        entries = self.state().get('revalidation') or []
        self.assertEqual([entry['task'] for entry in entries], ['T01', 'T02'])
        for entry in entries:
            self.assertEqual(entry['event'], 'task-reopen')
            self.assertEqual(entry['reason'], 'fixture rework with queue')
            self.assertTrue(entry['at'])

    def test_revalidate_executes_in_dependency_order(self):
        self.setup_git()
        self.prepare()
        self.done('T01')
        self.done('T02')
        self.cmd('task-reopen', 'work', 'T01', '--reason', 'fixture rework for revalidate')
        self.cmd('revalidate', 'work')
        state = self.state()
        self.assertEqual(state.get('revalidation') or [], [])
        statuses = {task['id']: task['status'] for task in state['tasks']}
        self.assertEqual(statuses, {'T01': 'done', 'T02': 'done'})

    def test_revalidate_failure_keeps_queue_and_ready_blocks(self):
        self.setup_git()
        self.prepare()
        self.done('T01')
        self.done('T02')
        self.cmd('task-reopen', 'work', 'T01', '--reason', 'fixture rework with failing replay')
        self.program().write_text("raise SystemExit('fixture intentional failure')\n", encoding='utf-8')
        args = parser().parse_args(['revalidate', 'work'])
        result, ok = dispatch(self.project, args)
        self.assertFalse(ok, result)
        state = self.state()
        self.assertEqual([entry['task'] for entry in state.get('revalidation') or []][0], 'T01')
        statuses = {task['id']: task['status'] for task in state['tasks']}
        self.assertNotEqual(statuses['T02'], 'done')
        doc = self.project.paths('work')[1]
        with self.assertRaisesRegex(WorkflowError, 'REVALIDATION_PENDING'):
            validate_delivery(self.project, state, doc)


if __name__ == '__main__':
    unittest.main()
