import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gsp.dispatch import (
    _save_dispatch,
    clean_session_worktree,
    dispatch_tasks,
    load_dispatch,
    record_launch_receipt,
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

    def routed_codex_dispatch(self, name='launch-receipt-child'):
        host = {
            'codex_home': 'C:/Users/example/.codex',
            'providers': ['magpie'],
            'catalog_models': ['opencode-go/deepseek-v4.1-flash'],
            'profiles': ['magpie'],
            'profile_catalogs': {'magpie': {'catalog_models': ['opencode-go/deepseek-v4.1-flash']}},
            'profile_providers': {'magpie': ['magpie']},
            'profile_model_providers': {'magpie': 'magpie'},
            'cli_path': 'codex',
            'cli_supports': {
                'exec': True, 'profile': True, 'model': True,
                'reasoning_effort': True, 'config_override': True, 'workdir': True,
            },
        }
        child = self.root / name
        child.mkdir()
        with patch('gsp.dispatch.inspect_codex_host', return_value=host):
            return dispatch_tasks(self.project, self.state, self.args(
                '--task', 'T01', '--target', 'T01=codex', '--path', 'T01=src/data',
                '--worktree', f'T01={child}', '--provider', 'T01=magpie',
                '--model', 'T01=opencode-go/deepseek-v4.1-flash'))['dispatches'][0]

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

    @patch('gsp.dispatch.inspect_codex_host')
    def test_codex_dispatch_route_builds_and_records_a_launch_plan(self, inspect_host):
        inspect_host.return_value = {
            'codex_home': 'C:/Users/example/.codex',
            'providers': ['magpie'],
            'catalog_models': [],
            'profiles': ['magpie'],
            'profile_catalogs': {
                'magpie': {'catalog_models': ['opencode-go/deepseek-v4.1-flash']},
            },
            'profile_providers': {'magpie': ['magpie']},
            'profile_model_providers': {'magpie': 'magpie'},
            'cli_path': 'codex',
            'cli_supports': {
                'exec': True,
                'profile': True,
                'model': True,
                'reasoning_effort': True,
                'config_override': True,
                'workdir': True,
            },
        }
        child = self.root / 'child-routed'
        child.mkdir()
        result = dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--target', 'T01=codex', '--path', 'T01=src/data',
            '--worktree', f'T01={child}',
            '--provider', 'T01=magpie', '--model-profile', 'T01=magpie',
            '--model', 'T01=opencode-go/deepseek-v4.1-flash',
            '--reasoning-effort', 'T01=max'))
        row = result['dispatches'][0]
        self.assertEqual(row['model_route'], {
            'provider': 'magpie',
            'model_profile': 'magpie',
            'model': 'opencode-go/deepseek-v4.1-flash',
            'reasoning_effort': 'max',
        })
        self.assertEqual(row['launch_plan']['argv'], [
            'codex', 'exec', '-c', 'model_provider="magpie"',
            '--profile', 'magpie',
            '--model', 'opencode-go/deepseek-v4.1-flash',
            '-c', 'model_reasoning_effort="max"',
            '-C', str(child.resolve()), '-',
        ])
        self.assertEqual(row['launch_plan']['env'], {'CODEX_HOME': 'C:\\Users\\example\\.codex'})
        self.assertIn('# T01 子会话启动提示词', row['launch_plan']['stdin'])
        self.assertFalse(row['launch_plan']['started'])
        self.assertEqual(row['launch_plan']['status'], 'NOT_CHECKED')
        prompt = (self.root / row['prompt']).read_text(encoding='utf-8')
        self.assertIn('opencode-go/deepseek-v4.1-flash', prompt)
        self.assertIn('NOT_CHECKED', prompt)

    @patch('gsp.dispatch.inspect_codex_host')
    def test_launch_receipt_records_blocking_without_claiming_a_request(self, inspect_host):
        inspect_host.return_value = {
            'codex_home': 'C:/Users/example/.codex',
            'providers': ['magpie'],
            'catalog_models': ['opencode-go/deepseek-v4.1-flash'],
            'profiles': ['magpie'],
            'profile_catalogs': {'magpie': {'catalog_models': ['opencode-go/deepseek-v4.1-flash']}},
            'profile_providers': {'magpie': ['magpie']},
            'profile_model_providers': {'magpie': 'magpie'},
            'cli_path': 'codex',
            'cli_supports': {
                'exec': True, 'profile': True, 'model': True,
                'reasoning_effort': True, 'config_override': True, 'workdir': True,
            },
        }
        child = self.root / 'launch-receipt-child'
        child.mkdir()
        row = dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--target', 'T01=codex', '--path', 'T01=src/data',
            '--worktree', f'T01={child}', '--provider', 'T01=magpie',
            '--model', 'T01=opencode-go/deepseek-v4.1-flash'))['dispatches'][0]
        receipt_args = parser().parse_args([
            'session-launch-receipt', 'work', '--task', 'T01', '--dispatch', row['id'],
            '--session', 'S01', '--status', 'blocked',
            '--reason', 'paid model request was not authorized for this automated verification',
        ])
        receipt = record_launch_receipt(self.project, self.state, receipt_args)
        self.assertEqual(receipt['status'], 'BLOCKED')
        current = load_dispatch(self.project, self.state)
        stored = next(item for item in current['dispatches'] if item['id'] == row['id'])
        self.assertEqual(stored['launch_receipt']['status'], 'BLOCKED')
        self.assertIn('not authorized', stored['launch_receipt']['reason'])
        self.assertFalse(stored['launch_plan']['started'])
        self.assertFalse(stored['launch_plan']['request_verified'])
        self.assertEqual(stored['status'], 'blocked')
        progress = session_progress(self.project, self.state)
        self.assertEqual(progress['blocked_tasks'], 1)
        self.assertEqual(progress['running_sessions'], 0)

        verified_args = parser().parse_args([
            'session-launch-receipt', 'work', '--task', 'T01', '--dispatch', row['id'],
            '--session', 'S01', '--status', 'request-verified', '--reason', 'worker replied',
        ])
        with self.assertRaisesRegex(WorkflowError, 'WORKER_LAUNCH_TRANSITION_INVALID'):
            record_launch_receipt(self.project, self.state, verified_args)

    def test_launch_receipt_enforces_evidence_and_valid_state_transitions(self):
        row = self.routed_codex_dispatch('launch-state-child')

        def receipt(status, *, evidence=None, reason='observed worker state'):
            argv = [
                'session-launch-receipt', 'work', '--task', 'T01',
                '--dispatch', row['id'], '--session', 'S01',
                '--status', status, '--reason', reason,
            ]
            for item in evidence or []:
                argv += ['--evidence', item]
            return parser().parse_args(argv)

        with self.assertRaisesRegex(WorkflowError, 'WORKER_LAUNCH_EVIDENCE_REQUIRED'):
            record_launch_receipt(
                self.project, self.state,
                receipt('started', evidence=['   ']),
            )
        with self.assertRaisesRegex(WorkflowError, 'WORKER_LAUNCH_TRANSITION_INVALID'):
            record_launch_receipt(
                self.project, self.state,
                receipt('request-verified', evidence=['response:one']),
            )

        started = record_launch_receipt(
            self.project, self.state,
            receipt('started', evidence=['thread:S02', '   ']),
        )
        self.assertEqual(started['status'], 'STARTED')
        self.assertEqual(started['evidence'], ['thread:S02'])
        current = load_dispatch(self.project, self.state)
        stored = next(item for item in current['dispatches'] if item['id'] == row['id'])
        self.assertEqual(stored['status'], 'running')
        self.assertTrue(stored['launch_plan']['started'])
        self.assertFalse(stored['launch_plan']['request_verified'])
        self.assertEqual(session_progress(self.project, self.state)['running_sessions'], 1)
        with self.assertRaisesRegex(WorkflowError, 'WORKER_LAUNCH_EVIDENCE_REQUIRED'):
            record_launch_receipt(
                self.project, self.state,
                receipt('request-verified'),
            )

        blocked = record_launch_receipt(
            self.project, self.state,
            receipt('blocked', reason='worker process exited before report'),
        )
        self.assertEqual(blocked['status'], 'BLOCKED')
        current = load_dispatch(self.project, self.state)
        stored = next(item for item in current['dispatches'] if item['id'] == row['id'])
        self.assertEqual(stored['status'], 'blocked')
        self.assertEqual(stored['launch_plan']['status'], 'BLOCKED')
        self.assertTrue(stored['launch_plan']['started'])
        self.assertFalse(stored['launch_plan']['request_verified'])
        progress = session_progress(self.project, self.state)
        self.assertEqual(progress['blocked_tasks'], 1)
        self.assertEqual(progress['running_sessions'], 0)

        with self.assertRaisesRegex(WorkflowError, 'WORKER_LAUNCH_TRANSITION_INVALID'):
            record_launch_receipt(
                self.project, self.state,
                receipt('request-verified', evidence=['response:too-early']),
            )

        record_launch_receipt(
            self.project, self.state,
            receipt('started', evidence=['thread:S03']),
        )
        verified = record_launch_receipt(
            self.project, self.state,
            receipt('request-verified', evidence=['response:success']),
        )
        self.assertEqual(verified['status'], 'REQUEST_VERIFIED')
        current = load_dispatch(self.project, self.state)
        stored = next(item for item in current['dispatches'] if item['id'] == row['id'])
        self.assertTrue(stored['launch_plan']['started'])
        self.assertTrue(stored['launch_plan']['request_verified'])
        self.assertEqual(stored['status'], 'running')
        self.assertEqual(len(stored['launch_receipts']), 4)

        stored['status'] = 'accepted'
        _save_dispatch(self.project, self.state, current)
        with self.assertRaisesRegex(WorkflowError, 'WORKER_LAUNCH_TERMINAL'):
            record_launch_receipt(
                self.project, self.state,
                receipt('blocked', reason='too late'),
            )

    @patch('gsp.dispatch.inspect_codex_host')
    def test_codex_dispatch_rejects_an_unsupported_route_before_publishing(self, inspect_host):
        inspect_host.return_value = {
            'providers': ['magpie'],
            'catalog_models': ['opencode-go/deepseek-v4.1-flash'],
            'profiles': ['magpie'],
            'profile_catalogs': {},
            'profile_providers': {},
            'profile_model_providers': {},
            'cli_path': 'codex',
            'cli_supports': {
                'exec': True,
                'profile': True,
                'model': False,
                'reasoning_effort': True,
                'config_override': True,
                'workdir': True,
            },
        }
        with self.assertRaisesRegex(WorkflowError, r'DISPATCH_MODEL_ROUTE_UNSUPPORTED T01.*cli_does_not_support_model'):
            dispatch_tasks(self.project, self.state, self.args(
                '--task', 'T01', '--target', 'T01=codex', '--path', 'T01=src/data',
                '--provider', 'T01=magpie',
                '--model', 'T01=opencode-go/deepseek-v4.1-flash'))
        self.assertFalse(
            (self.root / self.state['doc_dir'] / 'sessions' / 'T01-prompt.md').exists())

    def test_zcode_is_a_manual_required_worker_platform(self):
        result = dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--target', 'T01=zcode', '--path', 'T01=src/data'))
        row = result['dispatches'][0]
        self.assertEqual(row['platform'], 'zcode')
        self.assertEqual(row['status'], 'manual-required')

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

    def test_retry_supersedes_every_active_dispatch_across_worktrees(self):
        worktrees = [self.root / f'retry-child-{index}' for index in range(1, 4)]
        for worktree in worktrees:
            worktree.mkdir()
        first = dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--target', 'T01=manual', '--path', 'T01=src/data',
            '--worktree', f'T01={worktrees[0]}'))['dispatches'][0]
        second = dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--target', 'T01=manual', '--path', 'T01=src/data',
            '--worktree', f'T01={worktrees[1]}', '--retry'))['dispatches'][0]
        third = dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--target', 'T01=manual', '--path', 'T01=src/data',
            '--worktree', f'T01={worktrees[2]}', '--retry'))['dispatches'][0]
        data = load_dispatch(self.project, self.state)
        by_id = {row['id']: row for row in data['dispatches']}
        self.assertEqual(by_id[first['id']]['status'], 'superseded')
        self.assertEqual(by_id[second['id']]['status'], 'superseded')
        self.assertEqual(by_id[third['id']]['status'], 'manual-required')
        active = [row for row in data['dispatches'] if row['status'] in {'adapter-ready', 'running', 'reported', 'manual-required'}]
        self.assertEqual([row['id'] for row in active], [third['id']])

    def test_retry_supersedes_previous_worker_state_and_outbox_before_new_dispatch(self):
        first_child = self.root / 'retry-worker-state-one'
        second_child = self.root / 'retry-worker-state-two'
        first_child.mkdir()
        second_child.mkdir()
        first = dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--target', 'T01=manual', '--path', 'T01=src/data',
            '--worktree', f'T01={first_child}'))['dispatches'][0]
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--session', 'S02', '--role', 'worker', '--host', 'manual',
            '--task', 'T01', '--worktree', str(first_child), '--write-mode', 'write',
            '--path', 'src/data',
        ]))
        dispatch(self.project, parser().parse_args([
            'session-report', 'work', 'T01', '--session', 'S02', '--status', 'DONE',
            '--summary', 'old worker completed before controller retried',
        ]))
        before = load_registry(self.project, self.state)
        self.assertEqual(before['tasks']['T01']['report_status'], 'DONE')
        self.assertIn('S02', before['workspace_leases'].values())
        self.assertTrue(any(event.get('task') == 'T01' and event.get('session') == 'S02'
                            and not event.get('delivered') for event in before['outbox']))

        second = dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--target', 'T01=manual', '--path', 'T01=src/data',
            '--worktree', f'T01={second_child}', '--retry'))['dispatches'][0]
        registry = load_registry(self.project, self.state)
        mapping = registry['tasks']['T01']
        previous_worker = next(row for row in registry['sessions'] if row['id'] == 'S02')
        old_event = next(event for event in registry['outbox']
                         if event.get('task') == 'T01' and event.get('session') == 'S02')

        self.assertEqual(mapping['dispatch_id'], second['id'])
        self.assertNotIn('report_status', mapping)
        self.assertNotIn('report_session', mapping)
        self.assertEqual(mapping['previous_dispatches'][0]['dispatch_id'], first['id'])
        self.assertEqual(previous_worker['status'], 'superseded')
        self.assertNotIn('S02', registry['workspace_leases'].values())
        self.assertTrue(old_event['superseded'])
        watch = watch_session_events(
            self.project, self.state,
            parser().parse_args(['session-watch', 'work', '--adapter', 'manual', '--limit', '10']),
        )
        self.assertEqual(watch['delivered'], 0)

        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--session', 'S04', '--role', 'worker', '--host', 'manual',
            '--task', 'T01', '--worktree', str(first_child), '--write-mode', 'write',
            '--path', 'src/data',
        ]))
    @patch('gsp.dispatch.inspect_codex_host')
    def test_retry_route_failure_keeps_previous_dispatch_and_current_prompt(self, inspect_host):
        inspect_host.return_value = {
            'codex_home': 'C:/Users/example/.codex',
            'providers': ['magpie'],
            'catalog_models': ['opencode-go/deepseek-v4.1-flash'],
            'profiles': ['magpie'],
            'profile_catalogs': {'magpie': {'catalog_models': ['opencode-go/deepseek-v4.1-flash']}},
            'profile_providers': {'magpie': ['magpie']},
            'profile_model_providers': {'magpie': 'magpie'},
            'cli_path': 'codex',
            'cli_supports': {
                'exec': True, 'profile': True, 'model': True,
                'reasoning_effort': True, 'config_override': True, 'workdir': True,
            },
        }
        child = self.root / 'retry-route-child'
        child.mkdir()
        first = dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--target', 'T01=codex', '--path', 'T01=src/data',
            '--worktree', f'T01={child}', '--provider', 'T01=magpie',
            '--model-profile', 'T01=magpie',
            '--model', 'T01=opencode-go/deepseek-v4.1-flash'))['dispatches'][0]
        prompt = self.root / first['prompt']
        brief = self.root / first['brief']
        prompt_before = prompt.read_bytes()
        brief_before = brief.read_bytes()

        inspect_host.return_value = {
            'codex_home': 'C:/Users/example/.codex',
            'providers': ['magpie'],
            'catalog_models': [],
            'profiles': ['magpie'],
            'profile_catalogs': {'magpie': {'catalog_models': []}},
            'profile_providers': {'magpie': ['magpie']},
            'profile_model_providers': {'magpie': 'magpie'},
            'cli_path': 'codex',
            'cli_supports': {
                'exec': True, 'profile': True, 'model': True,
                'reasoning_effort': True, 'config_override': True, 'workdir': True,
            },
        }
        with self.assertRaisesRegex(WorkflowError, 'DISPATCH_MODEL_ROUTE_UNSUPPORTED'):
            dispatch_tasks(self.project, self.state, self.args(
                '--task', 'T01', '--target', 'T01=codex', '--path', 'T01=src/data',
                '--worktree', f'T01={child}', '--provider', 'T01=magpie',
                '--model-profile', 'T01=magpie',
                '--model', 'T01=opencode-go/missing', '--retry'))
        data = load_dispatch(self.project, self.state)
        previous = next(row for row in data['dispatches'] if row['id'] == first['id'])
        self.assertEqual(previous['status'], 'adapter-ready')
        self.assertEqual(prompt.read_bytes(), prompt_before)
        self.assertEqual(brief.read_bytes(), brief_before)
        history = self.root / self.state['doc_dir'] / 'sessions' / 'history'
        self.assertTrue(list(history.glob('T01-prompt-*.md')))
        self.assertTrue(list(history.glob('T01-brief-*.md')))

    def test_retry_still_rejects_a_shared_worktree_for_another_task(self):
        dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--target', 'T01=manual', '--path', 'T01=src/data',
            '--worktree', f'T01={self.root}'))
        with self.assertRaisesRegex(WorkflowError, 'DISPATCH_WORKTREE_CONFLICT'):
            dispatch_tasks(self.project, self.state, self.args(
                '--task', 'T02', '--target', 'T02=manual', '--path', 'T02=src/api',
                '--worktree', f'T02={self.root}', '--retry'))

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
        self.assertEqual(progress['managed_worktrees'], 1)
        self.assertEqual(progress['bound_worktrees'], 0)
        self.assertEqual(progress['cleanup_pending_worktrees'], 0)
        self.assertEqual(progress['overall_progress'], 33.3)

    def test_cleanup_requires_accepted_task_and_records_success(self):
        dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--target', 'T01=manual', '--path', 'T01=src/data',
            '--worktree', str(self.root)))
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


    def test_failed_batch_rolls_back_created_managed_worktrees(self):
        with self.assertRaisesRegex(WorkflowError, 'DISPATCH_PATH_ESCAPE'):
            dispatch_tasks(self.project, self.state, self.args(
                '--task', 'T01', '--task', 'T02',
                '--target', 'T01=codex', '--target', 'T02=codex',
                '--path', 'T01=src/data', '--path', 'T02=../escape',
                '--max-parallel', '2'))

        listing = subprocess.run(
            ['git', 'worktree', 'list', '--porcelain'], cwd=self.root,
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertNotIn('\\.gsp\\worktrees\\sessions', listing)
        self.assertFalse((self.root / self.state['doc_dir'] / 'sessions' / 'T01-prompt.md').exists())


    def test_retry_removes_accepted_managed_worktree(self):
        row = dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--target', 'T01=codex', '--path', 'T01=src/data'))['dispatches'][0]
        current = read(self.root / '.gsp/runs/work/state.json')
        current['tasks'][0]['status'] = 'done'
        self.project.write(current, 'test-accept', 'test fixture')
        retry_args = parser().parse_args([
            'session-clean', 'work', '--task', 'T01', '--result', 'removed',
            '--reason', 'retry lifecycle removal', '--retry',
        ])
        result = clean_session_worktree(self.project, current, retry_args)
        self.assertEqual(result['cleanup']['result'], 'removed')
        self.assertFalse(Path(row['worktree']).exists())


if __name__ == '__main__':
    unittest.main()
