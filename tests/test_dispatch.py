from contextlib import closing
import os
import json
import subprocess
import sqlite3
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace
from pathlib import Path

from gsp.dispatch import (
    _inflight_blocks_slot_release,
    _save_dispatch,
    clean_session_worktree,
    dispatch_tasks,
    load_dispatch,
    release_execution_slot_for_report,
    session_progress,
    watch_session_events,
)
from gsp.resource_registry import ResourceOwner, ResourceRegistry
from gsp.identity_store import IdentityStore
from gsp.runtime import Project, WorkflowError, dispatch, parser, read
from gsp.sessions import _find, load_registry, register_session
from gsp.worktree_lifecycle import prepare_worktree
try:
    from process_helpers import session_binding, temporary_env
except ModuleNotFoundError:
    from tests.process_helpers import session_binding, temporary_env


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
        # This direct-provider fixture exercises the published, unsealed legacy
        # dispatch contract. New sealed-entry enforcement has real CLI coverage
        # in test_gate_entrypoints and contract_store tests.
        self.state.pop('operation_schema',None)
        self.project.write(self.state,'fixture','legacy direct dispatch compatibility')
        ctrl = session_binding(self.root, self.state['work_uuid'], 'main', 'codex', 'main-thread')
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'main-thread', '--write-mode', 'write', '--path', '.',
            '--context', str(self.root / ctrl.handle_path),
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

    def set_plan_writes(self, first='src/data', second='src/api',
                        third='src/integration'):
        writes = {
            'T01': first,
            'T02': second,
            'T03': third,
        }
        sections = ['# Plan', '## Global Constraints', 'Keep all quality gates.']
        for task_id, path in writes.items():
            sections.extend([
                f'## Task {task_id}: {task_id}',
                f'Implement {task_id}.',
            ])
            if path is not None:
                sections.append(
                    '**Writes:** ' + json.dumps({'paths': [path]})
                )
        self.project.document(self.state, 'plan').write_text(
            '\n'.join(sections) + '\n',
            encoding='utf-8',
        )

    def test_dispatch_without_dispatch_capability_fails_before_worktree_creation(self):
        current = read(self.root / '.gsp/runs/work/state.json')
        replacement = session_binding(
            self.root, current['work_uuid'], 'main', 'codex', 'replacement-thread',
            capabilities={'accept', 'brief', 'claim', 'handoff', 'report', 'write'},
        )
        register_session(self.project, current, parser().parse_args([
            'session-register', 'work', '--session', 'S02', '--role', 'controller',
            '--host', 'codex', '--thread', 'replacement-thread', '--write-mode', 'write',
            '--path', '.', '--takeover', '--reason', 'capability fixture',
            '--context', str(self.root / replacement.handle_path),
        ]))
        before = subprocess.run(
            ['git', 'worktree', 'list', '--porcelain'], cwd=self.root,
            capture_output=True, text=True, check=True,
        ).stdout
        with self.assertRaisesRegex(WorkflowError, 'CAPABILITY_REQUIRED dispatch'):
            dispatch_args = self.args(
                '--task', 'T01', '--target', 'T01=codex', '--path', 'T01=src/data',
            )
            dispatch_args.session = 'S02'
            dispatch_tasks(self.project, current, dispatch_args)
        after = subprocess.run(
            ['git', 'worktree', 'list', '--porcelain'], cwd=self.root,
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(after, before)
        self.assertFalse(
            (self.root / self.state['doc_dir'] / 'sessions' / 'T01-contract.md').exists()
        )

    def test_dispatches_independent_batch_with_platform_specific_requests(self):
        self.set_plan_writes()
        one = self.root / 'child-one'
        two = self.root / 'child-two'
        one.mkdir()
        two.mkdir()
        def native_worker(job, cwd, env, timeout, folder, contract_text, **kwargs):
            kwargs['on_started'](SimpleNamespace(pid=os.getpid()))
            return {
                'status': 'completed',
                'native_session_id': '01a21b31-2815-72f2-9964-88ee78efc61b',
                'requested_model': 'group/deepseek-2',
                'requested_effort': 'max',
                'actual_model': 'group/deepseek-2',
                'usage': None,
                'usage_events': [],
                'execution': None,
            }

        with mock.patch('gsp.dispatch.run_worker', side_effect=native_worker):
            result = dispatch_tasks(self.project, self.state, self.args(
                '--task', 'T01', '--task', 'T02',
                '--target', 'T01=codex', '--target', 'T02=manual',
                '--path', 'T01=src/data', '--path', 'T02=src/api',
                '--worktree', f'T01={one}', '--worktree', f'T02={two}',
                '--max-parallel', '2'))
        self.assertEqual(result['batch_size'], 2)
        self.assertEqual([row['status'] for row in result['dispatches']], ['dispatched', 'manual-required'])
        resource_registry = ResourceRegistry(self.root)
        for row in result['dispatches']:
            text = (self.root / row['contract']).read_text(encoding='utf-8')
            self.assertIn('session-report', text)
            self.assertIn(row['task'], text)
            self.assertEqual(
                resource_registry.get(row['resource_request_uid'])['state'],
                'committed',
            )
            if row['platform'] == 'codex':
                self.assertEqual(
                    resource_registry.get_slot(
                        row['execution_slot_request_uid']
                    )['state'],
                    'released',
                )
                continue
            slot = resource_registry.get_slot(
                row['execution_slot_request_uid']
            )
            self.assertEqual(slot['state'], 'reserved')
            self.assertEqual(
                slot['owner']['session_uid'],
                row['dispatch_ref']['uid'],
            )
            self.assertEqual(
                slot['resources'][0]['identity'],
                row['execution_slot_identity'],
            )

    def test_codex_auto_dispatch_launches_native_worker_through_one_adapter(self):
        self.set_plan_writes()
        calls = []

        def native_worker(job, cwd, env, timeout, folder, contract_text, **kwargs):
            calls.append((job, cwd, timeout, contract_text, kwargs))
            kwargs['on_started'](SimpleNamespace(pid=os.getpid()))
            return {
                'status': 'completed',
                'native_session_id': '01a21b31-2815-72f2-9964-88ee78efc61b',
                'requested_model': 'group/deepseek-2',
                'requested_effort': 'max',
                'actual_model': 'group/deepseek-2',
                'usage': {'input_tokens': 1, 'output_tokens': 1,
                          'cached_input_tokens': 0, 'reasoning_tokens': 0},
                'usage_events': [],
                'execution': None,
            }

        with mock.patch('gsp.dispatch.run_worker', side_effect=native_worker) as worker:
            result = dispatch_tasks(self.project, self.state, self.args(
                '--task', 'T01', '--target', 'T01=codex',
                '--path', 'T01=src/data',
                '--worktree', f'T01={self.root}', '--worktree-mode', 'bind',
            ))

        row = result['dispatches'][0]
        worker.assert_called_once()
        job, cwd, timeout, contract_text, kwargs = calls[0]
        self.assertEqual(row['status'], 'dispatched')
        self.assertEqual(cwd, Path(row['worktree']))
        self.assertEqual(
            contract_text,
            (self.root / row['contract']).read_text(encoding='utf-8'),
        )
        self.assertEqual(kwargs['thread_id'], None)
        self.assertEqual(
            row['native_session_id'],
            '01a21b31-2815-72f2-9964-88ee78efc61b',
        )

    def test_dispatch_persists_one_complete_contract_for_brief_and_prompt(self):
        design = self.project.document(self.state, 'design')
        plan = self.project.document(self.state, 'plan')
        design.write_text(
            '# Design\n'
            'Only the required task context.\n'
            'Preserve the mandatory public interface and error ordering.\n',
            encoding='utf-8',
        )
        plan.write_text(
            '# Plan\n'
            '## Global Constraints\n'
            'Keep SP, TDD and independent review.\n'
            '## Task T01: Data\n'
            'Implement the complete data layer.\n'
            '**Writes:** {"paths":["src/data"]}\n'
            '## Task T02: API\n'
            'Implement the API layer.\n'
            '**Writes:** {"paths":["src/api"]}\n',
            encoding='utf-8',
        )
        child = self.root / 'contract-worker'
        child.mkdir()
        row = dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--target', 'T01=manual',
            '--path', 'T01=src/data', '--worktree', f'T01={child}',
            '--worktree-mode', 'bind',
        ))['dispatches'][0]

        self.assertEqual(row['contract'], row['brief'])
        self.assertEqual(row['contract'], row['prompt'])
        self.assertTrue(row['contract_uid'])
        self.assertTrue(row['content_digest'])
        self.assertEqual(row['model_alias'], 'group/deepseek-2')
        self.assertEqual(row['effort'], 'max')
        self.assertEqual(row['execution_slot_scope'], 'project')
        self.assertIs(row['features']['multi_agent'], False)
        sessions = self.root / self.state['doc_dir'] / 'sessions'
        self.assertFalse((sessions / 'T01-brief.md').exists())
        self.assertFalse((sessions / 'T01-prompt.md').exists())
        self.assertEqual(
            list(sessions.glob('T01-*.md')),
            [self.root / row['contract']],
        )
        text = (self.root / row['contract']).read_text(encoding='utf-8')
        self.assertIn('本文件同时是完整任务契约', text)
        self.assertIn('Implement the complete data layer.', text)
        self.assertIn('Keep SP, TDD and independent review.', text)
        self.assertIn(
            'Preserve the mandatory public interface and error ordering.',
            text,
        )
        self.assertIn('**Writes:** {"paths":["src/data"]}', text)
        self.assertIn(
            self.project.document(self.state, 'design').relative_to(self.root).as_posix(),
            text,
        )
        self.assertIn(
            self.project.document(self.state, 'plan').relative_to(self.root).as_posix(),
            text,
        )
        self.assertIn(row['content_digest'], text)
        self.assertIn('group/deepseek-2', text)
        self.assertIn('effort=max', text)
        self.assertNotIn('Implement the API layer.', text)
        self.assertNotIn('先读取总体 plan.md', text)

    def test_legacy_brief_and_prompt_aliases_resolve_to_one_contract(self):
        row = dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--target', 'T01=manual', '--path', 'T01=src/data',
            '--worktree', f'T01={self.root}', '--worktree-mode', 'bind',
        ))['dispatches'][0]
        data = load_dispatch(self.project, self.state)
        stored = data['dispatches'][0]
        stored.pop('contract', None)
        stored['brief'] = row['contract']
        stored['prompt'] = row['contract']
        _save_dispatch(self.project, self.state, data)
        legacy = load_dispatch(self.project, self.state)['dispatches'][0]
        self.assertEqual(legacy['contract'], row['contract'])
        self.assertEqual(legacy['brief'], legacy['contract'])
        self.assertEqual(legacy['prompt'], legacy['contract'])

    def test_global_dispatch_and_session_ids_keep_local_compatibility_aliases(self):
        self.set_plan_writes()
        one = self.root / 'canonical-child-one'
        two = self.root / 'canonical-child-two'
        one.mkdir()
        two.mkdir()
        rows = dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--task', 'T02',
            '--target', 'T01=manual', '--target', 'T02=manual',
            '--path', 'T01=src/data', '--path', 'T02=src/api',
            '--worktree', f'T01={one}', '--worktree', f'T02={two}',
            '--worktree-mode', 'bind', '--max-parallel', '2',
        ))['dispatches']
        row, second = rows
        self.assertEqual(row['id'], 'D001')
        self.assertEqual(row['dispatch_ref']['canonical_id'], 'D-000001')
        self.assertEqual(row['dispatch_ref']['kind'], 'dispatch')
        self.assertEqual(second['id'], 'D002')
        self.assertEqual(
            second['dispatch_ref']['canonical_id'], 'D-000002'
        )

        observer = register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--session', 'S02',
            '--role', 'observer', '--host', 'manual',
            '--thread', 'observer-thread', '--write-mode', 'read-only',
        ]))['session']
        self.assertEqual(observer['id'], 'S02')
        self.assertEqual(observer['session_ref']['canonical_id'], 'S-000002')
        self.assertEqual(observer['session_ref']['kind'], 'session')

        registry = load_registry(self.project, self.state)
        controller = next(
            item for item in registry['sessions'] if item['id'] == 'S01'
        )
        self.assertEqual(controller['session_ref']['canonical_id'], 'S-000001')
        store = IdentityStore(self.root)
        self.assertEqual(
            store.resolve(
                'dispatch', 'D-000001', None, write=False,
            ).uid,
            row['dispatch_ref']['uid'],
        )
        self.assertEqual(
            store.resolve(
                'dispatch', 'D-000002', None, write=False,
            ).uid,
            second['dispatch_ref']['uid'],
        )
        self.assertEqual(
            store.resolve(
                'session', 'S-000002', None, write=False,
            ).uid,
            observer['session_ref']['uid'],
        )
        self.assertEqual(
            load_dispatch(self.project, self.state)['dispatches'], rows
        )
        self.assertEqual(_find(registry, 'S01'), controller)
        self.assertEqual(_find(registry, 'S-000001'), controller)
        self.assertEqual(_find(registry, 'S02'), observer)

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
        self.set_plan_writes(first='src', second='src/api')
        with self.assertRaisesRegex(
            WorkflowError, 'DISPATCH_WRITES_GATE .*WRITE_CONFLICT'
        ):
            dispatch_tasks(self.project, self.state, self.args(
                '--task', 'T01', '--task', 'T02', '--target', 'T01=codex', '--target', 'T02=codex',
                '--path', 'T01=src', '--path', 'T02=src/api', '--worktree', str(self.root),
                '--max-parallel', '2'))

    def test_parallel_write_gate_rejects_before_uid_resource_or_worktree_side_effects(self):
        self.set_plan_writes(second=None)
        store = IdentityStore(self.root)
        identity_before = (
            store.count('dispatch', None),
            store.next_value('dispatch', None),
        )
        git_before = subprocess.run(
            ['git', 'worktree', 'list', '--porcelain'], cwd=self.root,
            capture_output=True, text=True, check=True,
        ).stdout
        with mock.patch('gsp.dispatch._allocate_dispatch_ref') as allocate:
            with mock.patch.object(
                ResourceRegistry, 'reserve_execution_batch',
            ) as reserve:
                with mock.patch('gsp.dispatch.prepare_worktree') as prepare:
                    with self.assertRaisesRegex(
                        WorkflowError,
                        'DISPATCH_WRITES_GATE .*WRITES_COVERAGE_INCOMPLETE'
                        '.*WRITES_PARALLEL_UNVERIFIED',
                    ):
                        dispatch_tasks(
                            self.project,
                            self.state,
                            self.args(
                                '--task', 'T01', '--task', 'T02',
                                '--target', 'T01=manual',
                                '--target', 'T02=manual',
                                '--path', 'T01=src/data',
                                '--path', 'T02=src/api',
                                '--max-parallel', '2',
                            ),
                        )
        allocate.assert_not_called()
        reserve.assert_not_called()
        prepare.assert_not_called()
        self.assertEqual(
            (
                store.count('dispatch', None),
                store.next_value('dispatch', None),
            ),
            identity_before,
        )
        self.assertEqual(load_dispatch(self.project, self.state)['dispatches'], [])
        self.assertEqual(
            ResourceRegistry(self.root).list_for_work(
                self.state['work_uuid']
            ),
            [],
        )
        git_after = subprocess.run(
            ['git', 'worktree', 'list', '--porcelain'], cwd=self.root,
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(git_after, git_before)

    def test_single_task_without_writes_remains_serial_compatible(self):
        self.set_plan_writes(
            first=None,
            second=None,
            third=None,
        )
        row = dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--target', 'T01=manual',
            '--path', 'T01=src/data',
        ))['dispatches'][0]
        self.assertEqual(row['status'], 'manual-required')
        self.assertEqual(row['task'], 'T01')

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
        wb = session_binding(self.root, self.state['work_uuid'], 'child', 'manual', 'worker-thread')
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--session', 'S02', '--role', 'worker', '--host', 'manual',
            '--task', 'T01', '--worktree', str(child), '--write-mode', 'write', '--path', 'src/data',
            '--thread', 'worker-thread', '--context', str(self.root / wb.handle_path),
        ]))
        report = parser().parse_args([
            'session-report', 'work', 'T01', '--session', 'S02', '--status', 'DONE',
            '--summary', '数据层完成', '--files', 'src/data.py', '--tests', 'unit: pass',
        ])
        with temporary_env({
            'GSP_SESSION_CONTEXT': str(self.root / wb.handle_path),
            'USERPROFILE': str(self.root),
            'HOME': str(self.root),
            'CODEX_HOME': str(self.root / '.codex-test-home'),
        }):
            result, success = dispatch(
                self.project, report, enforce_context=True,
            )
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

    def test_inflight_slot_release_probe_is_task_scoped_and_fail_closed(self):
        cases = (
            ({'id': 'same', 'task': 'T01'}, True),
            ({'id': 'all', 'task': 'all'}, True),
            ({'id': 'unknown'}, True),
            ({'id': 'bad-task', 'task': 123}, True),
            ('malformed', True),
            ({'id': 'other', 'task': 'T02'}, False),
            (None, False),
        )
        for inflight, expected in cases:
            with self.subTest(inflight=inflight):
                state = {'inflight': inflight}
                self.assertIs(
                    _inflight_blocks_slot_release(state, 'T01'),
                    expected,
                )

    def test_terminal_report_releases_slot_but_keeps_workspace_claims(self):
        row = dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--target', 'T01=codex',
            '--dispatch-mode', 'adapter',
            '--path', 'T01=src/data',
        ))['dispatches'][0]
        registry = ResourceRegistry(self.root)
        request_before = registry.get(row['resource_request_uid'])
        self.assertEqual(request_before['state'], 'committed')
        data = load_dispatch(self.project, self.state)
        stored = data['dispatches'][0]
        expected_owner = dict(stored['execution_slot_owner'])
        nonterminal = release_execution_slot_for_report(
            self.project, self.state, 'T01', 'UNKNOWN', 'S02',
        )
        self.assertFalse(nonterminal['released'])
        self.assertEqual(
            registry.get_slot(row['execution_slot_request_uid'])['state'],
            'reserved',
        )
        stored['execution_slot_owner']['session_uid'] = 'tampered-owner'
        _save_dispatch(self.project, self.state, data)
        with self.assertRaisesRegex(
            WorkflowError, 'SESSION_REPORT_SLOT_AUTHORITY_INVALID'
        ):
            release_execution_slot_for_report(
                self.project, self.state, 'T01', 'DONE', 'S02',
            )
        self.assertEqual(
            registry.get_slot(row['execution_slot_request_uid'])['state'],
            'reserved',
        )
        stored['execution_slot_owner'] = expected_owner
        _save_dispatch(self.project, self.state, data)

        child = self.root / 'slot-worker'
        child.mkdir()
        binding = session_binding(
            self.root, self.state['work_uuid'], 'child', 'manual',
            'slot-worker-thread',
        )
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--session', 'S02',
            '--role', 'worker', '--host', 'manual', '--task', 'T01',
            '--worktree', str(child), '--write-mode', 'write',
            '--path', 'src/data', '--thread', 'slot-worker-thread',
            '--context', str(self.root / binding.handle_path),
        ]))
        report = parser().parse_args([
            'session-report', 'work', 'T01', '--session', 'S02',
            '--status', 'DONE', '--summary', 'completed in slot',
            '--files', 'src/data.py', '--tests', 'unit: pass',
        ])
        with temporary_env({
            'GSP_SESSION_CONTEXT': str(self.root / binding.handle_path),
            'USERPROFILE': str(self.root),
            'HOME': str(self.root),
            'CODEX_HOME': str(self.root / '.codex-test-home'),
        }):
            result, success = dispatch(
                self.project, report, enforce_context=True,
            )
        self.assertTrue(success)
        self.assertEqual(result['status'], 'DONE')

        slot = registry.get_slot(row['execution_slot_request_uid'])
        self.assertEqual(slot['state'], 'released')
        self.assertEqual(
            registry.get(row['resource_request_uid'])['state'],
            'committed',
        )
        active = registry.list_for_work(self.state['work_uuid'])
        self.assertFalse(any(
            claim['kind'] == 'slot'
            and claim['request_uid'] == row['execution_slot_request_uid']
            for claim in active
        ))
        self.assertTrue(any(
            claim['kind'] == 'path'
            and claim['request_uid'] == row['resource_request_uid']
            for claim in active
        ))
        stored = load_dispatch(self.project, self.state)['dispatches'][0]
        self.assertEqual(stored['execution_slot_state'], 'released')

        replacement = registry.reserve_execution_batch(
            'review-resource-request',
            ResourceOwner(
                self.state['work_uuid'], 'independent-reviewer', 1,
            ),
            [('path', 'review/output')],
            [(
                'review-slot-request',
                ResourceOwner(
                    self.state['work_uuid'], 'independent-reviewer', 1,
                ),
            )],
        )
        self.assertEqual(
            replacement['slots']['review-slot-request']['resources'][0][
                'identity'
            ],
            row['execution_slot_identity'],
        )

    def test_terminal_report_keeps_slot_pending_while_execution_is_unknown(self):
        row = dispatch_tasks(self.project, self.state, self.args(
            '--task', 'T01', '--target', 'T01=codex',
            '--dispatch-mode', 'adapter',
            '--path', 'T01=src/data',
        ))['dispatches'][0]
        registry = ResourceRegistry(self.root)

        child = self.root / 'pending-slot-worker'
        child.mkdir()
        binding = session_binding(
            self.root, self.state['work_uuid'], 'child', 'manual',
            'pending-slot-thread',
        )
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--session', 'S02',
            '--role', 'worker', '--host', 'manual', '--task', 'T01',
            '--worktree', str(child), '--write-mode', 'write',
            '--path', 'src/data', '--thread', 'pending-slot-thread',
            '--context', str(self.root / binding.handle_path),
        ]))
        current = read(self.root / '.gsp/runs/work/state.json')
        current['inflight'] = {
            'id': 'unknown-execution', 'kind': 'check', 'task': 'T01',
        }
        self.project.write(current, 'unknown-execution', 'fixture')

        report = parser().parse_args([
            'session-report', 'work', 'T01', '--session', 'S02',
            '--status', 'DONE', '--summary', 'report survives unknown child',
            '--files', 'src/data.py', '--tests', 'unit: pass',
        ])
        environment = {
            'GSP_SESSION_CONTEXT': str(self.root / binding.handle_path),
            'USERPROFILE': str(self.root),
            'HOME': str(self.root),
            'CODEX_HOME': str(self.root / '.codex-test-home'),
        }
        with temporary_env(environment):
            first, success = dispatch(
                self.project, report, enforce_context=True,
            )
        self.assertTrue(success)
        self.assertEqual(first['status'], 'DONE')
        self.assertEqual(
            registry.get_slot(row['execution_slot_request_uid'])['state'],
            'reserved',
        )
        report_path = self.root / first['report']
        self.assertTrue(report_path.is_file())
        registry_data = load_registry(self.project, self.state)
        self.assertEqual(
            registry_data['tasks']['T01']['report_status'], 'DONE'
        )

        resolved = read(self.root / '.gsp/runs/work/state.json')
        resolved.pop('inflight', None)
        self.project.write(resolved, 'recover-execution', 'fixture resolved')
        with temporary_env(environment):
            replay, success = dispatch(
                self.project, report, enforce_context=True,
            )
        self.assertTrue(success)
        self.assertTrue(replay['replayed'])
        self.assertEqual(
            registry.get_slot(row['execution_slot_request_uid'])['state'],
            'released',
        )

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
        self.set_plan_writes()
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
        self.assertFalse((self.root / self.state['doc_dir'] / 'sessions' / 'T01-contract.md').exists())

    def test_late_invalid_task_creates_no_worktree_or_dispatch_intent(self):
        before = subprocess.run(
            ['git', 'worktree', 'list', '--porcelain'], cwd=self.root,
            capture_output=True, text=True, check=True,
        ).stdout
        with mock.patch(
            'gsp.dispatch.prepare_worktree', wraps=prepare_worktree,
        ) as prepare:
            with self.assertRaisesRegex(
                WorkflowError, 'DISPATCH_TASK_NOT_FOUND T99'
            ):
                dispatch_tasks(self.project, self.state, self.args(
                    '--task', 'T01', '--task', 'T99',
                    '--target', 'T01=codex', '--target', 'T99=codex',
                    '--path', 'T01=src/data', '--path', 'T99=src/missing',
                    '--max-parallel', '2',
                ))
        prepare.assert_not_called()
        after = subprocess.run(
            ['git', 'worktree', 'list', '--porcelain'], cwd=self.root,
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(after, before)
        self.assertEqual(load_dispatch(self.project, self.state)['dispatches'], [])

    def test_unknown_preparation_result_keeps_intent_and_replays_one_request(self):
        seen = []

        def fail_after_durable_intent(project, state, task, candidate, **kwargs):
            data = load_dispatch(project, state)
            intent = next(row for row in data['dispatches']
                          if row.get('task') == task['id'])
            self.assertEqual(intent['status'], 'preparing')
            self.assertEqual(
                ResourceRegistry(project.root).get(
                    intent['resource_request_uid']
                )['state'],
                'reserved',
            )
            seen.append((intent['id'], intent['resource_request_uid']))
            raise RuntimeError('synthetic unknown Git result')

        dispatch_args = self.args(
            '--task', 'T01', '--target', 'T01=manual', '--path', 'T01=src/data',
            '--worktree', f'T01={self.root}', '--worktree-mode', 'bind',
        )
        with mock.patch('gsp.dispatch.prepare_worktree',
                        side_effect=fail_after_durable_intent):
            for _ in range(2):
                with self.assertRaisesRegex(
                    RuntimeError, 'synthetic unknown Git result'
                ):
                    dispatch_tasks(self.project, self.state, dispatch_args)

        intents = load_dispatch(self.project, self.state)['dispatches']
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]['status'], 'preparing')
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(
            ResourceRegistry(self.root).get(
                intents[0]['resource_request_uid']
            )['state'],
            'reserved',
        )

    def test_auto_schedule_recovers_intent_after_grant_before_dispatch_save(self):
        from gsp.contract_store import publish_contracts

        self.set_plan_writes()
        state = read(self.root / '.gsp/runs/work/state.json')
        state['operation_schema'] = 2
        state['status'] = 'execute'
        state['tasks'][2]['depends'] = []
        publish_contracts(self.project, state)
        self.project.write(
            state, 'scheduler-fixture', 'enable sealed scheduler recovery'
        )
        self.state = state

        workers = {}
        for task_id in ('T01', 'T02', 'T03'):
            path = self.root / ('scheduler-' + task_id.casefold())
            path.mkdir()
            workers[task_id] = path
        args = self.args(
            '--target', 'manual',
            '--path', 'T01=src/data', '--path', 'T02=src/api',
            '--path', 'T03=src/integration',
            '--worktree', 'T01=' + str(workers['T01']),
            '--worktree', 'T02=' + str(workers['T02']),
            '--worktree', 'T03=' + str(workers['T03']),
            '--worktree-mode', 'bind', '--max-parallel', '3',
        )
        real_commit = ResourceRegistry.commit
        crashed = {'value': False}

        def crash_after_registry_commit(registry, request_uid, owner):
            result = real_commit(registry, request_uid, owner)
            if not crashed['value']:
                crashed['value'] = True
                raise RuntimeError(
                    'synthetic crash after grant before dispatch save'
                )
            return result

        with mock.patch(
            'gsp.dispatch.ResourceRegistry.commit',
            new=crash_after_registry_commit,
        ):
            with self.assertRaisesRegex(
                RuntimeError, 'synthetic crash after grant before dispatch save'
            ):
                dispatch_tasks(self.project, self.state, args)

        intent = load_dispatch(self.project, self.state)['dispatches']
        self.assertEqual(
            sorted(row['task'] for row in intent), ['T01', 'T02', 'T03']
        )
        self.assertTrue(all(row['status'] == 'preparing' for row in intent))
        self.assertTrue(all(
            row['scheduler_token'] == {
                'revision': 0, 'cursor': {'work_uid': self.state['work_uuid']}
            }
            for row in intent
        ))
        request_uid = intent[0]['resource_request_uid']
        dispatch_ids = [row['id'] for row in intent]
        registry = ResourceRegistry(self.root)
        granted = registry.scheduler_snapshot()
        self.assertEqual(granted['revision'], 1)
        self.assertEqual(granted['occupied'], 3)
        self.assertEqual(registry.get(request_uid)['state'], 'committed')

        with mock.patch.object(
            ResourceRegistry, '_advance_scheduler',
            wraps=ResourceRegistry._advance_scheduler,
        ) as advance:
            recovered = dispatch_tasks(self.project, self.state, args)
        advance.assert_not_called()
        self.assertEqual(
            sorted(row['task'] for row in recovered['dispatches']),
            ['T01', 'T02', 'T03'],
        )
        self.assertEqual(
            [row['id'] for row in recovered['dispatches']], dispatch_ids
        )
        self.assertTrue(all(
            row['resource_request_uid'] == request_uid
            for row in recovered['dispatches']
        ))
        self.assertEqual(registry.scheduler_snapshot(), granted)
        self.assertEqual(
            sorted(
                registry.get_slot(row['execution_slot_request_uid'])['state']
                for row in intent
            ),
            ['reserved', 'reserved', 'reserved'],
        )

    def test_crash_after_managed_creation_recovers_the_same_intent(self):
        calls = {'count': 0}

        def fail_after_creation(project, state, data):
            calls['count'] += 1
            if calls['count'] > 0 and any(
                row.get('worktree_state') == 'created'
                and row.get('worktree')
                and Path(row['worktree']).is_dir()
                for row in data.get('dispatches', [])
            ):
                raise RuntimeError('synthetic crash after worktree creation')
            return _save_dispatch(project, state, data)

        args = self.args(
            '--task', 'T01', '--target', 'T01=manual', '--path', 'T01=src/data',
        )
        with mock.patch(
            'gsp.dispatch._save_dispatch', side_effect=fail_after_creation,
        ):
            with self.assertRaisesRegex(
                RuntimeError, 'synthetic crash after worktree creation'
            ):
                dispatch_tasks(self.project, self.state, args)

        interrupted = load_dispatch(self.project, self.state)['dispatches']
        self.assertEqual(len(interrupted), 1)
        self.assertEqual(interrupted[0]['status'], 'preparing')
        self.assertEqual(interrupted[0]['worktree_state'], 'unknown')
        self.assertTrue(Path(interrupted[0]['worktree']).is_dir())

        recovered = dispatch_tasks(self.project, self.state, args)['dispatches']
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]['id'], interrupted[0]['id'])
        self.assertEqual(
            recovered[0]['resource_request_uid'],
            interrupted[0]['resource_request_uid'],
        )
        self.assertEqual(
            ResourceRegistry(self.root).get(
                recovered[0]['resource_request_uid']
            )['state'],
            'committed',
        )

    def test_other_repository_in_batch_rejected_before_any_side_effect(self):
        self.set_plan_writes()
        other = self.root.parent / 'other-repository'
        other.mkdir()
        subprocess.run(
            ['git', 'init', '-q', '-b', 'main'], cwd=other, check=True,
        )
        subprocess.run(
            ['git', 'config', 'user.name', 'Other Test'],
            cwd=other, check=True,
        )
        subprocess.run(
            ['git', 'config', 'user.email', 'other@example.invalid'],
            cwd=other, check=True,
        )
        (other / 'README.md').write_text('# other\n', encoding='utf-8')
        subprocess.run(['git', 'add', 'README.md'], cwd=other, check=True)
        subprocess.run(
            ['git', 'commit', '-qm', 'chore: other baseline'],
            cwd=other, check=True,
        )
        with mock.patch(
            'gsp.dispatch.prepare_worktree', wraps=prepare_worktree,
        ) as prepare:
            with self.assertRaisesRegex(
                WorkflowError, 'WORKTREE_COMMON_DIR_MISMATCH'
            ):
                dispatch_tasks(self.project, self.state, self.args(
                    '--task', 'T01', '--task', 'T02',
                    '--target', 'T01=manual', '--target', 'T02=manual',
                    '--path', 'T01=src/data', '--path', 'T02=src/api',
                    '--worktree', f'T01={self.root}', '--worktree', f'T02={other}',
                    '--worktree-mode', 'bind', '--max-parallel', '2',
                ))
        prepare.assert_not_called()
        self.assertEqual(
            load_dispatch(self.project, self.state)['dispatches'], []
        )
        resource_registry = ResourceRegistry(self.root)
        self.assertEqual(
            resource_registry.list_for_work(self.state['work_uuid']), []
        )
        with closing(sqlite3.connect(resource_registry.path)) as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='resource_claims'"
            ).fetchone()
            claims = (
                db.execute("SELECT COUNT(*) FROM resource_claims").fetchone()[0]
                if table else 0
            )
        self.assertEqual(claims, 0)

    def test_created_worktree_branch_swap_is_not_adopted_during_recovery(self):
        calls = {'count': 0}

        def fail_after_creation(project, state, data):
            calls['count'] += 1
            if calls['count'] > 0 and any(
                row.get('worktree_state') == 'created'
                and row.get('worktree')
                and Path(row['worktree']).is_dir()
                for row in data.get('dispatches', [])
            ):
                raise RuntimeError('synthetic create interruption')
            return _save_dispatch(project, state, data)

        args = self.args(
            '--task', 'T01', '--target', 'T01=manual', '--path', 'T01=src/data',
        )
        with mock.patch(
            'gsp.dispatch._save_dispatch', side_effect=fail_after_creation,
        ):
            with self.assertRaisesRegex(
                RuntimeError, 'synthetic create interruption'
            ):
                dispatch_tasks(self.project, self.state, args)
        intent = load_dispatch(self.project, self.state)['dispatches'][0]
        subprocess.run(
            ['git', 'switch', '-c', 'unexpected-recovery-branch'],
            cwd=intent['worktree'], check=True, capture_output=True, text=True,
        )

        with self.assertRaisesRegex(
            WorkflowError, 'DISPATCH_WORKTREE_RECOVERY_MISMATCH T01:branch'
        ):
            dispatch_tasks(self.project, self.state, args)
        stored = load_dispatch(self.project, self.state)['dispatches'][0]
        self.assertEqual(stored['status'], 'preparing')
        self.assertEqual(stored['branch'], intent['branch'])
        self.assertEqual(
            ResourceRegistry(self.root).get(
                stored['resource_request_uid']
            )['state'],
            'reserved',
        )

    def test_wrong_common_dir_intent_is_not_adopted_during_recovery(self):
        calls = {'count': 0}

        def fail_after_creation(project, state, data):
            calls['count'] += 1
            if calls['count'] > 0 and any(
                row.get('worktree_state') == 'created'
                and row.get('worktree')
                and Path(row['worktree']).is_dir()
                for row in data.get('dispatches', [])
            ):
                raise RuntimeError('synthetic create interruption')
            return _save_dispatch(project, state, data)

        args = self.args(
            '--task', 'T01', '--target', 'T01=manual', '--path', 'T01=src/data',
        )
        with mock.patch(
            'gsp.dispatch._save_dispatch', side_effect=fail_after_creation,
        ):
            with self.assertRaisesRegex(
                RuntimeError, 'synthetic create interruption'
            ):
                dispatch_tasks(self.project, self.state, args)
        data = load_dispatch(self.project, self.state)
        intent = data['dispatches'][0]
        intent['common_dir'] = str(
            (self.root.parent / 'wrong-common-dir').resolve()
        )
        _save_dispatch(self.project, self.state, data)

        with self.assertRaisesRegex(
            WorkflowError, 'DISPATCH_WORKTREE_RECOVERY_MISMATCH T01'
        ):
            dispatch_tasks(self.project, self.state, args)
        stored = load_dispatch(self.project, self.state)['dispatches'][0]
        self.assertEqual(stored['status'], 'preparing')
        self.assertEqual(
            ResourceRegistry(self.root).get(
                stored['resource_request_uid']
            )['state'],
            'reserved',
        )

    def test_retry_removes_accepted_managed_worktree(self):
        def native_worker(job, cwd, env, timeout, folder, contract_text, **kwargs):
            kwargs['on_started'](SimpleNamespace(pid=os.getpid()))
            return {
                'status': 'completed',
                'native_session_id': '01a21b31-2815-72f2-9964-88ee78efc61b',
                'requested_model': 'group/deepseek-2',
                'requested_effort': 'max',
                'actual_model': 'group/deepseek-2',
                'usage': None,
                'usage_events': [],
                'execution': None,
            }

        with mock.patch('gsp.dispatch.run_worker', side_effect=native_worker):
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
