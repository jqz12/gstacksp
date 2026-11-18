import subprocess
import tempfile
import unittest
from pathlib import Path

from gsp.dispatch import (
    clean_session_worktree,
    dispatch_tasks,
    session_progress,
    watch_session_events,
)
from gsp.runtime import Project, WorkflowError, dispatch, parser, read
from gsp.sessions import load_registry, register_session


class DispatchControlPlaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-dispatch-')
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Dispatch Test')
        self.git('config', 'user.email', 'dispatch@example.invalid')
        (self.root / 'README.md').write_text('# dispatch\n', encoding='utf-8')
        self.git('add', 'README.md')
        self.git('commit', '-qm', 'chore: baseline')
        self.project = Project(self.root)
        self.project.initialize()
        self.cmd('new', 'work', '--title', '跨平台并行派发', '--intent', 'cross-session')
        self.cmd('task-add', 'work', 'T01', '--title', '数据层', '--acceptance', '数据层测试')
        self.cmd('task-add', 'work', 'T02', '--title', '接口层', '--acceptance', '接口层测试')
        self.cmd('task-add', 'work', 'T03', '--title', '集成', '--acceptance', '集成测试', '--depends', 'T01,T02')
        self.state = read(self.root / '.gsp/runs/work/state.json')
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'main-thread', '--write-mode', 'write', '--path', '.',
        ]))

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        result = subprocess.run(['git', *args], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def cmd(self, *argv):
        return dispatch(self.project, parser().parse_args(list(argv)))[0]

    def args(self, *argv):
        return parser().parse_args(['session-dispatch', 'work', '--session', 'S01', *argv])

    def test_dispatches_independent_batch_with_platform_specific_requests(self):
        one = self.root / 'child-one'
        two = self.root / 'child-two'
        one.mkdir()
        two.mkdir()
        result = dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--task', 'T02',
            '--target', 'T01=codex', '--target', 'T02=manual',
            '--path', 'T01=src/data', '--path', 'T02=src/api',
            '--worktree', f'T01={one}', '--worktree', f'T02={two}',
            '--max-parallel', '2'))
        self.assertEqual(result['batch_size'], 2)
        self.assertEqual([row['status'] for row in result['dispatches']], ['adapter-ready', 'manual-required'])
        self.assertIn('session-report', (self.root / self.state['doc_dir'] / 'sessions' / 'T01-prompt.md').read_text(encoding='utf-8'))
        self.assertIn('T02', (self.root / self.state['doc_dir'] / 'sessions' / 'T02-prompt.md').read_text(encoding='utf-8'))

    def test_dependency_is_blocked_until_prerequisites_are_accepted(self):
        with self.assertRaisesRegex(WorkflowError, 'DISPATCH_DEPENDENCY_BLOCKED'):
            dispatch_tasks(self.project, self.state, self.args(
                '--task', 'T03', '--target', 'T03=codex', '--path', 'T03=src/integration'))

    def test_overlapping_paths_in_one_worktree_are_rejected(self):
        with self.assertRaisesRegex(WorkflowError, 'DISPATCH_PATH_CONFLICT'):
            dispatch_tasks(self.project, self.state, self.args(
                '--task', 'T01', '--task', 'T02', '--target', 'T01=codex', '--target', 'T02=codex',
                '--path', 'T01=src', '--path', 'T02=src/api', '--worktree', str(self.root),
                '--max-parallel', '2'))

    def test_duplicate_dispatch_is_rejected_without_retry(self):
        dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--target', 'T01=manual', '--path', 'T01=src/data'))
        with self.assertRaisesRegex(WorkflowError, 'DISPATCH_TASK_EXISTS'):
            dispatch_tasks(self.project, self.state, self.args(
                '--task', 'T01', '--target', 'T01=manual', '--path', 'T01=src/data'))

    def test_progress_separates_reported_accepted_and_cleaned_counts(self):
        dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--target', 'T01=manual', '--path', 'T01=src/data'))
        current = read(self.root / '.gsp/runs/work/state.json')
        current['tasks'][0]['status'] = 'done'
        self.project.write(current, 'test-accept', 'test fixture')
        progress = session_progress(self.project, current)
        self.assertEqual(progress['dispatches_total'], 1)
        self.assertEqual(progress['distinct_tasks'], 1)
        self.assertEqual(progress['accepted_tasks'], 1)
        self.assertEqual(progress['cleaned_worktrees'], 0)
        self.assertEqual(progress['overall_progress'], 33.3)

    def test_cleanup_requires_accepted_task_and_records_success(self):
        dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--target', 'T01=manual', '--path', 'T01=src/data'))
        clean_args = parser().parse_args([
            'session-clean', 'work', '--task', 'T01', '--result', 'archived',
            '--reason', '主会话已接受 report',
        ])
        with self.assertRaisesRegex(WorkflowError, 'CLEANUP_ACCEPTANCE_REQUIRED'):
            clean_session_worktree(self.project, self.state, clean_args)
        current = read(self.root / '.gsp/runs/work/state.json')
        current['tasks'][0]['status'] = 'done'
        self.project.write(current, 'test-accept', 'test fixture')
        result = clean_session_worktree(self.project, current, clean_args)
        self.assertEqual(result['cleanup']['result'], 'archived')
        self.assertEqual(session_progress(self.project, current)['cleaned_worktrees'], 1)

    def test_session_report_publishes_a_durable_outbox_event(self):
        child = self.root / 'child-report'
        child.mkdir()
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--session', 'S02', '--role', 'worker', '--host', 'manual',
            '--task', 'T01', '--worktree', str(child), '--write-mode', 'write', '--path', 'src/data',
        ]))
        report = parser().parse_args([
            'session-report', 'work', 'T01', '--session', 'S02', '--status', 'DONE',
            '--summary', '数据层完成', '--files', 'src/data.py', '--tests', 'unit: pass',
        ])
        result, success = dispatch(self.project, report)
        self.assertTrue(success)
        self.assertEqual(result['status'], 'DONE')
        registry = load_registry(self.project, self.state)
        events = [event for event in registry['outbox'] if event['task'] == 'T01']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['session'], 'S02')
        self.assertFalse(events[0]['delivered'])
        watch_args = parser().parse_args(['session-watch', 'work', '--adapter', 'manual', '--limit', '10'])
        delivered = watch_session_events(self.project, self.state, watch_args)
        self.assertEqual(delivered['delivered'], 1)
        self.assertEqual(watch_session_events(self.project, self.state, watch_args)['delivered'], 0)

    def test_watch_consumes_outbox_once(self):
        registry = load_registry(self.project, self.state)
        registry['outbox'].append({
            'id': 'event-1', 'task': 'T01', 'session': 'S02', 'status': 'DONE',
            'report': 'docs/work/report.md', 'delivered': False,
        })
        from gsp.sessions import _save
        _save(self.project, self.state, registry)
        watch_args = parser().parse_args(['session-watch', 'work', '--adapter', 'manual', '--limit', '10'])
        first = watch_session_events(self.project, self.state, watch_args)
        second = watch_session_events(self.project, self.state, watch_args)
        self.assertEqual(first['delivered'], 1)
        self.assertEqual(second['delivered'], 0)


if __name__ == '__main__':
    unittest.main()
