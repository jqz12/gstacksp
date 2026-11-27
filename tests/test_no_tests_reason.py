"""Late no-tests reason: explicit, bounded, redacted, and never faked tests."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from gsp.runtime import Project, WorkflowError, dispatch, parser, read


class NoTestsReasonTests(unittest.TestCase):
    task = 'T1'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-no-tests-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.project = Project(self.root)
        self.project.initialize()
        self.cmd('new', 'work', '--title', 'No tests reason fixture')
        state = read(self.root / '.gsp/runs/work/state.json')
        # Preserve the legacy no-tests-reason contract independently of the
        # new task-classification gate covered by test_convergence_wiring.
        state.pop('convergence_schema', None)
        self.project.write(state, 'fixture', 'legacy no-tests reason',
                           action_uid='fixture:legacy-no-tests-reason')
        self.doc = self.root / state['doc_dir']
        self.project.document(state, 'design').write_text(
            '# Docs-only change with explicit verification.', encoding='utf-8')
        self.cmd('scope', 'work', '--off', '--quote', 'synthetic fixture')
        self.cmd('approve', 'work', '--quote', 'synthetic fixture approval')
        self.project.document(state, 'plan').write_text(
            '# Plan: a docs-only task completed with a recorded reason.', encoding='utf-8')
        self.cmd('task-add', 'work', self.task, '--title', 'Docs only',
                 '--acceptance', 'Docs verified by a check command')
        self.cmd('seal-plan', 'work')
        self.cmd('advance', 'work', '--to', 'execute')
        self.cmd('task-start', 'work', self.task)

    def cmd(self, *argv):
        values = list(argv)
        cut = values.index('--') if '--' in values else len(values)
        args = parser().parse_args(values[:cut])
        if cut < len(values):
            args.argv = values[cut + 1:]
        return dispatch(self.project, args)

    def state(self):
        return read(self.root / '.gsp/runs/work/state.json')

    def task_record(self):
        return next(row for row in self.state()['tasks'] if row['id'] == self.task)

    def check(self):
        return self.cmd('run', 'work', '--task', self.task, '--kind', 'check', '--',
                        sys.executable, '-c', 'print("document fixture checked")')

    def tests(self):
        return self.cmd('run', 'work', '--task', self.task, '--kind', 'tests', '--',
                        sys.executable, '-c', 'print("Ran 1 test in 0.0s")')

    def test_check_with_late_reason_completes(self):
        self.check()
        result, ok = self.cmd('task-done', 'work', self.task,
                              '--no-tests-reason', '纯文档更新，验证命令是 build 与链接检查')
        self.assertTrue(ok, result)
        task = self.task_record()
        self.assertEqual(task['status'], 'done')
        self.assertEqual(task['no_tests_reason'], '纯文档更新，验证命令是 build 与链接检查')
        history = task.get('history') or []
        self.assertTrue(any(row.get('source') == 'task-done' for row in history))

    def test_check_without_reason_still_requires_tests(self):
        self.check()
        with self.assertRaisesRegex(WorkflowError, 'TESTS_REQUIRED'):
            dispatch(self.project, parser().parse_args(
                ['task-done', 'work', self.task]))
        self.assertEqual(self.task_record()['status'], 'running')
        self.assertFalse(self.task_record().get('no_tests_reason'))

    def test_tests_with_reason_is_a_conflict(self):
        self.tests()
        with self.assertRaisesRegex(WorkflowError, 'NO_TESTS_REASON_CONFLICT'):
            dispatch(self.project, parser().parse_args(
                ['task-done', 'work', self.task, '--no-tests-reason',
                 '这段理由不应被接受，因为已经存在测试证据']))
        self.assertEqual(self.task_record()['status'], 'running')

    def test_tests_complete_without_reason(self):
        self.tests()
        result, ok = self.cmd('task-done', 'work', self.task)
        self.assertTrue(ok, result)
        self.assertEqual(self.task_record()['status'], 'done')

    def test_reason_length_and_blank_are_rejected(self):
        self.check()
        for bad in ('   ', 'short', 'x' * 301):
            with self.subTest(reason=bad):
                with self.assertRaisesRegex(WorkflowError, 'NO_TESTS_REASON_INVALID'):
                    dispatch(self.project, parser().parse_args(
                        ['task-done', 'work', self.task, '--no-tests-reason', bad]))
        self.assertEqual(self.task_record()['status'], 'running')

    def test_reason_is_redacted_before_storage(self):
        self.check()
        self.cmd('task-done', 'work', self.task, '--no-tests-reason',
                 'docs build verified; api_key=SECRETVALUE123 must never be stored')
        stored = self.task_record()['no_tests_reason']
        self.assertNotIn('SECRETVALUE123', stored)
        self.assertIn('<REDACTED>', stored)


if __name__ == '__main__':
    unittest.main()
