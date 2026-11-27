import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gsp.runtime import Project, WorkflowError, atomic, parser, dispatch, read
from gsp.documents import Governance
try:
    from process_helpers import session_binding, temporary_env
except ModuleNotFoundError:
    from tests.process_helpers import session_binding, temporary_env
from gsp.identity_store import IdentityStore
from gsp.orchestration_types import digest_value
from gsp.session_context import load_binding, require_capability
from gsp.sessions import (
    _new_transition_intent,
    _recover_controller_transitions,
    authorize_edit,
    claim_session,
    handoff_controller,
    register_session,
    session_status,
    write_brief,
    write_report,
)


class SessionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-session-registry-')
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Session Test')
        self.git('config', 'user.email', 'session@example.invalid')
        (self.root / 'README.md').write_text('# sessions\n', encoding='utf-8')
        self.git('add', 'README.md')
        self.git('commit', '-qm', 'chore: baseline')
        self.project = Project(self.root)
        self.project.initialize()
        self.cmd('new', 'work', '--title', '中文跨会话任务', '--intent', 'cross-session')
        self.state = read(self.root / '.gsp/runs/work/state.json')
        self.cmd('task-add', 'work', 'T01', '--title', '服务实现', '--acceptance', '接口测试通过')
        self.state = read(self.root / '.gsp/runs/work/state.json')

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        result = subprocess.run(['git', *args], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def cmd(self, *argv):
        return dispatch(self.project, parser().parse_args(list(argv)))[0]

    def test_registers_sessions_and_rejects_second_writer_in_same_workspace(self):
        main = session_binding(self.root, self.state['work_uuid'], 'main', 'codex', 'thread-a')
        first = register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-a', '--write-mode', 'write', '--path', 'services',
            '--context', str(self.root / main.handle_path)]))
        self.assertEqual(first['session']['id'], 'S01')
        child = session_binding(self.root, self.state['work_uuid'], 'child', 'claude', 'thread-b')
        second_args = parser().parse_args([
            'session-register', 'work', '--role', 'worker', '--host', 'claude',
            '--thread', 'thread-b', '--task', 'T01', '--write-mode', 'write', '--path',
            'services/quant-control.service', '--context', str(self.root / child.handle_path)])
        with self.assertRaisesRegex(WorkflowError, 'WORKSPACE_WRITE_LEASE_HELD'):
            register_session(self.project, self.state, second_args)

    def test_second_controller_is_rejected_until_handoff(self):
        main = session_binding(self.root, self.state['work_uuid'], 'main', 'codex', 'thread-a')
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-a', '--write-mode', 'write', '--path', '.',
            '--context', str(self.root / main.handle_path)]))
        main2 = session_binding(self.root, self.state['work_uuid'], 'main', 'claude', 'thread-b')
        with self.assertRaisesRegex(WorkflowError, 'CONTROLLER_LEASE_HELD S01'):
            register_session(self.project, self.state, parser().parse_args([
                'session-register', 'work', '--role', 'controller', '--host', 'claude',
                '--thread', 'thread-b', '--write-mode', 'read-only',
                '--context', str(self.root / main2.handle_path)]))

    def test_takeover_cannot_replace_active_worker_workspace_lease(self):
        controller = session_binding(
            self.root, self.state['work_uuid'], 'main', 'codex', 'thread-controller',
        )
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-controller', '--worktree', str(self.root / 'controller'),
            '--write-mode', 'write', '--path', 'src',
            '--context', str(self.root / controller.handle_path)]))
        worker = session_binding(
            self.root, self.state['work_uuid'], 'child', 'codex', 'thread-worker',
        )
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'worker', '--host', 'codex',
            '--thread', 'thread-worker', '--task', 'T01',
            '--worktree', str(self.root / 'worker'),
            '--write-mode', 'write', '--path', 'tests',
            '--context', str(self.root / worker.handle_path)]))
        before = read(self.root / '.gsp/runs/work/sessions/index.json')
        newcomer = session_binding(
            self.root, self.state['work_uuid'], 'main', 'codex', 'thread-newcomer',
        )
        with self.assertRaisesRegex(WorkflowError, 'WORKSPACE_WRITE_LEASE_HELD S02'):
            register_session(self.project, self.state, parser().parse_args([
                'session-register', 'work', '--role', 'controller', '--host', 'codex',
                '--thread', 'thread-newcomer', '--worktree', str(self.root / 'worker'),
                '--write-mode', 'write', '--path', 'src', '--takeover',
                '--reason', 'must not steal worker workspace',
                '--context', str(self.root / newcomer.handle_path)]))
        self.assertEqual(
            read(self.root / '.gsp/runs/work/sessions/index.json'),
            before,
        )

    def test_takeover_rejects_target_without_write_without_mutation(self):
        controller = session_binding(
            self.root, self.state['work_uuid'], 'main', 'codex', 'thread-controller',
        )
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-controller', '--write-mode', 'write', '--path', '.',
            '--context', str(self.root / controller.handle_path)]))
        target = session_binding(
            self.root, self.state['work_uuid'], 'main', 'claude', 'thread-target',
            capabilities={'handoff'},
        )
        registry_path = self.root / '.gsp/runs/work/sessions/index.json'
        before_registry = read(registry_path)
        before_old = load_binding(self.root / controller.handle_path)
        before_target = load_binding(self.root / target.handle_path)
        intents = self.root / '.gsp/orchestration/controller-transition-intents'
        before_intents = set(intents.glob('*.json')) if intents.is_dir() else set()
        with self.assertRaisesRegex(WorkflowError, 'CAPABILITY_REQUIRED write'):
            register_session(self.project, self.state, parser().parse_args([
                'session-register', 'work', '--session', 'S02', '--role', 'controller',
                '--host', 'claude', '--thread', 'thread-target', '--write-mode', 'read-only',
                '--takeover', '--reason', 'target is not a writer',
                '--context', str(self.root / target.handle_path)]))
        self.assertEqual(read(registry_path), before_registry)
        self.assertEqual(load_binding(self.root / controller.handle_path), before_old)
        self.assertEqual(load_binding(self.root / target.handle_path), before_target)
        after_intents = set(intents.glob('*.json')) if intents.is_dir() else set()
        self.assertEqual(after_intents, before_intents)

    def test_pending_takeover_recovery_requires_write_before_mutation(self):
        controller = session_binding(
            self.root, self.state['work_uuid'], 'main', 'codex', 'thread-controller',
        )
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-controller', '--write-mode', 'write', '--path', '.',
            '--context', str(self.root / controller.handle_path)]))
        target = session_binding(
            self.root, self.state['work_uuid'], 'main', 'claude', 'thread-target',
            capabilities={'handoff'},
        )
        record = {
            'id': 'S02', 'role': 'controller', 'host': 'claude',
            'thread_id': 'thread-target', 'task': None, 'branch': None,
            'worktree': str(self.root), 'write_mode': 'read-only',
            'claimed_paths': [], 'status': 'active', 'context': target.handle_path,
            'context_mode': 'bound', 'binding_uid': target.binding_uid,
            'work_uid': target.work_uid,
            'controller_generation': target.controller_generation,
            'cancel_generation': target.cancel_generation,
            'context_binding': target.to_dict(),
        }
        envelope = _new_transition_intent(
            self.project, self.state, 'takeover', controller, target, 2,
            'S01', 'S02', 'pending target lacks write', new_record=record,
        )
        registry_path = self.root / '.gsp/runs/work/sessions/index.json'
        before_registry = read(registry_path)
        before_old = load_binding(self.root / controller.handle_path)
        before_target = load_binding(self.root / target.handle_path)
        with self.assertRaisesRegex(WorkflowError, 'CAPABILITY_REQUIRED write'):
            _recover_controller_transitions(self.project, self.state)
        self.assertEqual(read(registry_path), before_registry)
        self.assertEqual(load_binding(self.root / controller.handle_path), before_old)
        self.assertEqual(load_binding(self.root / target.handle_path), before_target)
        intent_path = (
            self.root / '.gsp/orchestration/controller-transition-intents' /
            (next(iter((self.root / '.gsp/orchestration/controller-transition-intents').glob('*.json'))).name)
        )
        self.assertIsNone(read(intent_path).get('operation_uid'))
        self.assertEqual(envelope['payload_digest'], read(intent_path)['payload_digest'])

    def test_pending_takeover_recovery_rejects_tampered_intent_without_mutation(self):
        controller = session_binding(
            self.root, self.state['work_uuid'], 'main', 'codex', 'thread-controller',
        )
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-controller', '--write-mode', 'write', '--path', '.',
            '--context', str(self.root / controller.handle_path)]))
        target = session_binding(
            self.root, self.state['work_uuid'], 'main', 'claude', 'thread-target',
            capabilities={'handoff'},
        )
        record = {
            'id': 'S02', 'role': 'controller', 'host': 'claude',
            'thread_id': 'thread-target', 'task': None, 'branch': None,
            'worktree': str(self.root), 'write_mode': 'read-only',
            'claimed_paths': [], 'status': 'active', 'context': target.handle_path,
            'context_mode': 'bound', 'binding_uid': target.binding_uid,
            'work_uid': target.work_uid,
            'controller_generation': target.controller_generation,
            'cancel_generation': target.cancel_generation,
            'context_binding': target.to_dict(),
        }
        _new_transition_intent(
            self.project, self.state, 'takeover', controller, target, 2,
            'S01', 'S02', 'pending target lacks write', new_record=record,
        )
        intents = self.root / '.gsp/orchestration/controller-transition-intents'
        intent_path = next(intents.glob('*.json'))
        envelope = read(intent_path)
        envelope['payload']['new_binding']['capabilities'] = [
            'handoff', 'write',
        ]
        envelope['payload_digest'] = digest_value(envelope['payload'])
        atomic(intent_path, envelope)
        registry_path = self.root / '.gsp/runs/work/sessions/index.json'
        before_registry = read(registry_path)
        store = IdentityStore(self.root)
        before_old = store.read_session_state(controller.binding_uid)
        before_target = store.read_session_state(target.binding_uid)
        with self.assertRaisesRegex(
            WorkflowError, 'CONTROLLER_TRANSITION_INTENT_TAMPERED',
        ):
            _recover_controller_transitions(self.project, self.state)
        self.assertEqual(read(registry_path), before_registry)
        self.assertEqual(store.read_session_state(controller.binding_uid), before_old)
        self.assertEqual(store.read_session_state(target.binding_uid), before_target)
        self.assertIsNone(read(intent_path).get('operation_uid'))

    def test_non_overlapping_worktrees_can_claim_paths_and_overlap_is_blocked(self):
        main = session_binding(self.root, self.state['work_uuid'], 'main', 'codex', 'thread-a')
        first = register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-a', '--worktree', str(self.root / 'one'),
            '--write-mode', 'write', '--path', 'services',
            '--context', str(self.root / main.handle_path)]))
        child = session_binding(self.root, self.state['work_uuid'], 'child', 'claude', 'thread-b')
        second = register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'worker', '--host', 'claude',
            '--thread', 'thread-b', '--task', 'T01', '--worktree', str(self.root / 'two'),
            '--write-mode', 'write', '--path', 'tests',
            '--context', str(self.root / child.handle_path)]))
        self.assertEqual((first['session']['id'], second['session']['id']), ('S01', 'S02'))
        result = claim_session(self.project, self.state, 'S02', ['tests'])
        self.assertEqual(result['claimed_paths'], ['tests'])

    def test_chinese_title_uses_stable_run_id_folder_suffix(self):
        record = Governance(self.root).create('中文标题', '目标', 'test', run_id='cross-session-development')
        self.assertTrue(record['folder'].endswith('GSP-000002-cross-session-development'))

    def test_handoff_rejects_observer_without_capabilities_without_mutation(self):
        main = session_binding(self.root, self.state['work_uuid'], 'main', 'codex', 'thread-a')
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-a', '--write-mode', 'write', '--path', '.',
            '--context', str(self.root / main.handle_path)]))
        obs = session_binding(self.root, self.state['work_uuid'], 'observer', 'claude', 'thread-b',
                              capabilities=set())
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'observer', '--host', 'claude',
            '--thread', 'thread-b', '--write-mode', 'read-only',
            '--context', str(self.root / obs.handle_path)]))
        before_registry = read(self.root / '.gsp/runs/work/sessions/index.json')
        before_old = load_binding(self.root / main.handle_path)
        before_new = load_binding(self.root / obs.handle_path)
        with self.assertRaisesRegex(WorkflowError, r'CAPABILITY_REQUIRED handoff\b'):
            handoff_controller(self.project, self.state, parser().parse_args([
                'session-handoff', 'work', '--from', 'S01', '--to', 'S02',
                '--reason', '主会话切换']))
        self.assertEqual(
            read(self.root / '.gsp/runs/work/sessions/index.json'), before_registry,
        )
        self.assertEqual(load_binding(self.root / main.handle_path), before_old)
        self.assertEqual(load_binding(self.root / obs.handle_path), before_new)

    def test_handoff_rejects_target_without_write_without_mutation(self):
        main = session_binding(
            self.root, self.state['work_uuid'], 'main', 'codex', 'thread-a',
        )
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-a', '--write-mode', 'write', '--path', '.',
            '--context', str(self.root / main.handle_path)]))
        target = session_binding(
            self.root, self.state['work_uuid'], 'main', 'claude', 'thread-b',
            capabilities={'handoff'},
        )
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'observer', '--host', 'claude',
            '--thread', 'thread-b', '--write-mode', 'read-only',
            '--context', str(self.root / target.handle_path)]))
        before_registry = read(self.root / '.gsp/runs/work/sessions/index.json')
        before_old = load_binding(self.root / main.handle_path)
        before_target = load_binding(self.root / target.handle_path)
        with self.assertRaisesRegex(WorkflowError, 'CAPABILITY_REQUIRED write'):
            handoff_controller(self.project, self.state, parser().parse_args([
                'session-handoff', 'work', '--from', 'S01', '--to', 'S02',
                '--reason', 'must not promote a non-writer',
            ]))
        self.assertEqual(
            read(self.root / '.gsp/runs/work/sessions/index.json'),
            before_registry,
        )
        self.assertEqual(load_binding(self.root / main.handle_path), before_old)
        self.assertEqual(load_binding(self.root / target.handle_path), before_target)

    def test_claim_requires_claim_capability_before_mutation(self):
        main = session_binding(
            self.root, self.state['work_uuid'], 'main', 'codex', 'thread-claim',
            capabilities={'write'},
        )
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-claim', '--write-mode', 'write', '--path', 'services',
            '--context', str(self.root / main.handle_path)]))
        with self.assertRaisesRegex(WorkflowError, 'CAPABILITY_REQUIRED claim'):
            claim_session(self.project, self.state, 'S01', ['tests'])
        row = next(item for item in session_status(self.project, self.state)['sessions']
                   if item['id'] == 'S01')
        self.assertEqual(row['claimed_paths'], ['services'])

    def test_brief_requires_brief_capability_before_document_write(self):
        main = session_binding(
            self.root, self.state['work_uuid'], 'main', 'codex', 'thread-brief',
            capabilities={'claim', 'write'},
        )
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-brief', '--write-mode', 'write', '--path', 'services',
            '--context', str(self.root / main.handle_path)]))
        with self.assertRaisesRegex(WorkflowError, 'CAPABILITY_REQUIRED brief'):
            write_brief(self.project, self.state, SimpleNamespace(
                session='S01', owner='S01', task='T01', title='Missing brief capability',
                objective='Must not write this brief.', path=[], interface=[], test=[],
                depends='', forbid='', replace=False,
            ))
        self.assertFalse(
            (self.root / self.state['doc_dir'] / 'sessions' / 'T01-brief.md').exists()
        )

    def test_report_requires_report_capability_before_mutation(self):
        main = session_binding(
            self.root, self.state['work_uuid'], 'main', 'codex', 'thread-report',
            capabilities={'write'},
        )
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-report', '--write-mode', 'write', '--path', 'services',
            '--context', str(self.root / main.handle_path)]))
        caller = load_binding(self.root / main.handle_path)
        with self.assertRaisesRegex(WorkflowError, 'CAPABILITY_REQUIRED report'):
            write_report(self.project, self.state, SimpleNamespace(
                session='S01', task='T01', status='DONE', summary='Must not be recorded',
                files=[], tests=[], interfaces=[], evidence=[], need_from_main='', ruling='',
            ), caller)
        registry = read(self.root / '.gsp/runs/work/sessions/index.json')
        self.assertNotIn('report_status', registry.get('tasks', {}).get('T01', {}))
        self.assertEqual(registry.get('outbox', []), [])

    def test_report_owner_mismatch_leaves_all_artifacts_unchanged(self):
        state, context = self._register_report_worker()
        registry_file = self.root / '.gsp/runs/work/sessions/index.json'
        registry = read(registry_file)
        registry.setdefault('tasks', {})['T01'] = {'owner_session': 'S99'}
        atomic(registry_file, registry)
        state_file = self.root / '.gsp/runs/work/state.json'
        session_dir = self.root / state['doc_dir'] / 'sessions'
        before_state = state_file.read_bytes()
        before_registry = registry_file.read_bytes()
        before_files = {
            path.relative_to(session_dir).as_posix(): path.read_bytes()
            for path in session_dir.glob('**/*')
            if path.is_file()
        }

        with self.assertRaisesRegex(WorkflowError, 'SESSION_REPORT_OWNER_MISMATCH'):
            write_report(
                self.project,
                read(state_file),
                self._report_args('wrong owner report'),
                load_binding(context),
            )

        self.assertEqual(state_file.read_bytes(), before_state)
        self.assertEqual(registry_file.read_bytes(), before_registry)
        self.assertEqual(
            {
                path.relative_to(session_dir).as_posix(): path.read_bytes()
                for path in session_dir.glob('**/*')
                if path.is_file()
            },
            before_files,
        )

    def test_report_wrong_work_binding_leaves_all_artifacts_unchanged(self):
        state, context = self._register_report_worker()
        self.cmd('new', 'other', '--title', 'Other report work', '--intent', 'cross-session')
        other = read(self.root / '.gsp/runs/other/state.json')
        wrong = session_binding(
            self.root, other['work_uuid'], 'main', 'codex', 'thread-other-work',
            capabilities={'report', 'write'},
        )
        state_file = self.root / '.gsp/runs/work/state.json'
        registry_file = self.root / '.gsp/runs/work/sessions/index.json'
        session_dir = self.root / state['doc_dir'] / 'sessions'
        before_state = state_file.read_bytes()
        before_registry = registry_file.read_bytes()
        before_files = {
            path.relative_to(session_dir).as_posix(): path.read_bytes()
            for path in session_dir.glob('**/*')
            if path.is_file()
        }

        with self.assertRaisesRegex(WorkflowError, 'WORK_UID_MISMATCH'):
            write_report(
                self.project,
                read(state_file),
                self._report_args('wrong work report'),
                load_binding(self.root / wrong.handle_path),
            )

        self.assertEqual(state_file.read_bytes(), before_state)
        self.assertEqual(registry_file.read_bytes(), before_registry)
        self.assertEqual(
            {
                path.relative_to(session_dir).as_posix(): path.read_bytes()
                for path in session_dir.glob('**/*')
                if path.is_file()
            },
            before_files,
        )

    def test_cancelled_report_binding_leaves_all_artifacts_unchanged(self):
        state = read(self.root / '.gsp/runs/work/state.json')
        first = session_binding(
            self.root, state['work_uuid'], 'main', 'codex', 'thread-first',
        )
        first_context = self.root / first.handle_path
        register_session(self.project, state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-first', '--write-mode', 'write', '--path', '.',
            '--context', str(first_context),
        ]))
        second = session_binding(
            self.root, state['work_uuid'], 'main', 'codex', 'thread-second',
        )
        register_session(self.project, state, parser().parse_args([
            'session-register', 'work', '--session', 'S02', '--role', 'controller',
            '--host', 'codex', '--thread', 'thread-second', '--write-mode', 'write',
            '--path', '.', '--takeover', '--reason', 'replace stale report owner',
            '--context', str(self.root / second.handle_path),
        ]))
        state_file = self.root / '.gsp/runs/work/state.json'
        registry_file = self.root / '.gsp/runs/work/sessions/index.json'
        session_dir = self.root / state['doc_dir'] / 'sessions'
        before_state = state_file.read_bytes()
        before_registry = registry_file.read_bytes()
        before_files = {
            path.relative_to(session_dir).as_posix(): path.read_bytes()
            for path in session_dir.glob('**/*')
            if path.is_file()
        }

        with self.assertRaisesRegex(WorkflowError, 'SESSION_'):
            write_report(
                self.project,
                read(state_file),
                SimpleNamespace(
                    command='session-report', id='work', task='T01', session='S01',
                    status='DONE', summary='stale report must not publish',
                    files=[], tests=[], interfaces=[], evidence=[],
                    need_from_main='', ruling='',
                ),
                load_binding(first_context),
            )

        self.assertEqual(state_file.read_bytes(), before_state)
        self.assertEqual(registry_file.read_bytes(), before_registry)
        self.assertEqual(
            {
                path.relative_to(session_dir).as_posix(): path.read_bytes()
                for path in session_dir.glob('**/*')
                if path.is_file()
            },
            before_files,
        )

    def _register_report_worker(self):
        state = read(self.root / '.gsp/runs/work/state.json')
        main = session_binding(
            self.root, state['work_uuid'], 'main', 'codex', 'thread-report-main',
        )
        register_session(self.project, state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-report-main', '--write-mode', 'write', '--path', '.',
            '--context', str(self.root / main.handle_path),
        ]))
        worker = session_binding(
            self.root, state['work_uuid'], 'child', 'codex', 'thread-report-worker',
        )
        register_session(self.project, state, parser().parse_args([
            'session-register', 'work', '--session', 'S02', '--role', 'worker',
            '--host', 'codex', '--thread', 'thread-report-worker', '--task', 'T01',
            '--worktree', str(self.root / 'report-worker'), '--write-mode', 'write',
            '--path', 'services', '--context', str(self.root / worker.handle_path),
        ]))
        return state, self.root / worker.handle_path

    @staticmethod
    def _report_args(summary):
        return SimpleNamespace(
            command='session-report', id='work', task='T01', session='S02',
            status='DONE', summary=summary, files=['services/one.py'],
            tests=['1 passed'], interfaces=[], evidence=[],
            need_from_main='', ruling='',
        )

    def _session_artifact_snapshot(self, state):
        state_file = self.root / '.gsp/runs/work/state.json'
        registry_file = self.root / '.gsp/runs/work/sessions/index.json'
        session_dir = self.root / state['doc_dir'] / 'sessions'
        store = IdentityStore(self.project.root)
        return {
            'state': state_file.read_bytes(),
            'registry': registry_file.read_bytes(),
            'files': {
                path.relative_to(session_dir).as_posix(): path.read_bytes()
                for path in session_dir.glob('**/*')
                if path.is_file()
            },
            'operations': store.operation_count(),
            'events': store.event_count(),
        }

    def _assert_session_artifacts_equal(self, state, expected):
        state_file = self.root / '.gsp/runs/work/state.json'
        registry_file = self.root / '.gsp/runs/work/sessions/index.json'
        session_dir = self.root / state['doc_dir'] / 'sessions'
        store = IdentityStore(self.project.root)
        self.assertEqual(state_file.read_bytes(), expected['state'])
        self.assertEqual(registry_file.read_bytes(), expected['registry'])
        self.assertEqual(
            {
                path.relative_to(session_dir).as_posix(): path.read_bytes()
                for path in session_dir.glob('**/*')
                if path.is_file()
            },
            expected['files'],
        )
        self.assertEqual(store.operation_count(), expected['operations'])
        self.assertEqual(store.event_count(), expected['events'])

    def test_new_report_rejects_missing_completed_artifact_without_mutation(self):
        state, context = self._register_report_worker()
        binding = load_binding(context)
        write_report(
            self.project,
            state,
            self._report_args('completed report before deletion'),
            binding,
        )
        state_file = self.root / '.gsp/runs/work/state.json'
        report_file = (
            self.root / state['doc_dir'] / 'sessions' / 'T01-report.md'
        )
        report_file.unlink()
        before = self._session_artifact_snapshot(state)

        with self.assertRaisesRegex(
            WorkflowError, 'SESSION_REPORT_DOCUMENT_CONFLICT',
        ):
            write_report(
                self.project,
                read(state_file),
                self._report_args('new request after report deletion'),
                binding,
            )

        self._assert_session_artifacts_equal(state, before)

    def test_new_report_rejects_tampered_completed_artifact_without_mutation(self):
        state, context = self._register_report_worker()
        binding = load_binding(context)
        write_report(
            self.project,
            state,
            self._report_args('completed report before tamper'),
            binding,
        )
        state_file = self.root / '.gsp/runs/work/state.json'
        report_file = (
            self.root / state['doc_dir'] / 'sessions' / 'T01-report.md'
        )
        report_file.write_bytes(b'# tampered completed report\n')
        before = self._session_artifact_snapshot(state)

        with self.assertRaisesRegex(
            WorkflowError, 'SESSION_REPORT_DOCUMENT_CONFLICT',
        ):
            write_report(
                self.project,
                read(state_file),
                self._report_args('new request after report tamper'),
                binding,
            )

        self._assert_session_artifacts_equal(state, before)

    def test_report_integrity_survives_later_non_report_write(self):
        state, context = self._register_report_worker()
        binding = load_binding(context)
        write_report(
            self.project,
            state,
            self._report_args('completed report before later write'),
            binding,
        )
        state_file = self.root / '.gsp/runs/work/state.json'
        self.project.write(
            read(state_file),
            'report-followup',
            'unrelated state write after completed report',
        )
        report_file = (
            self.root / state['doc_dir'] / 'sessions' / 'T01-report.md'
        )
        report_file.unlink()
        before = self._session_artifact_snapshot(state)

        with self.assertRaisesRegex(
            WorkflowError, 'SESSION_REPORT_DOCUMENT_CONFLICT',
        ):
            write_report(
                self.project,
                read(state_file),
                self._report_args('report after later write and deletion'),
                binding,
            )

        self._assert_session_artifacts_equal(state, before)

    def test_new_report_archives_only_verified_prior_bytes(self):
        state, context = self._register_report_worker()
        binding = load_binding(context)
        write_report(
            self.project,
            state,
            self._report_args('verified original report'),
            binding,
        )
        state_file = self.root / '.gsp/runs/work/state.json'
        session_dir = self.root / state['doc_dir'] / 'sessions'
        report_file = session_dir / 'T01-report.md'
        first_bytes = report_file.read_bytes()

        write_report(
            self.project,
            read(state_file),
            self._report_args('legitimate replacement report'),
            binding,
        )

        history = sorted((session_dir / 'history').glob('T01-report-*.md'))
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].read_bytes(), first_bytes)
        self.assertNotEqual(report_file.read_bytes(), first_bytes)
        registry = read(
            self.root / '.gsp/runs/work/sessions/index.json'
        )
        self.assertEqual(len(registry['outbox']), 2)

    def test_report_requires_real_caller_binding_at_library_boundary(self):
        state, _context = self._register_report_worker()
        before = self._session_artifact_snapshot(state)

        with self.assertRaisesRegex(
            WorkflowError, 'SESSION_BINDING_REQUIRED',
        ):
            write_report(
                self.project,
                read(self.root / '.gsp/runs/work/state.json'),
                self._report_args('caller binding omitted'),
                None,
            )

        self._assert_session_artifacts_equal(state, before)

    def test_report_rejects_non_authoritative_caller_state_without_mutation(self):
        state, context = self._register_report_worker()
        binding = load_binding(context)
        tampered = replace(
            binding,
            controller_generation=binding.controller_generation + 1,
        )
        before = self._session_artifact_snapshot(state)

        with self.assertRaisesRegex(
            WorkflowError, 'SESSION_CONTEXT_TAMPERED',
        ):
            write_report(
                self.project,
                state,
                self._report_args('tampered caller generation'),
                tampered,
            )

        self._assert_session_artifacts_equal(state, before)

    def test_report_thread_alias_exact_replay_reuses_one_operation(self):
        state, context = self._register_report_worker()
        binding = load_binding(context)
        args = self._report_args('thread alias exact replay')
        args.session = 'thread-report-worker'

        write_report(self.project, state, args, binding)
        state_file = self.root / '.gsp/runs/work/state.json'
        after_first = self._session_artifact_snapshot(state)

        result = write_report(
            self.project,
            read(state_file),
            args,
            binding,
        )

        self.assertTrue(result['replayed'])
        after_retry = self._session_artifact_snapshot(state)
        self.assertEqual(after_retry, after_first)

    def test_report_wrong_task_is_rejected_before_effects(self):
        state, context = self._register_report_worker()
        binding = load_binding(context)
        self.cmd(
            'task-add', 'work', 'T02', '--title', 'Second task',
            '--acceptance', 'A worker registered for T01 cannot report it',
        )
        state = read(self.root / '.gsp/runs/work/state.json')
        before = self._session_artifact_snapshot(state)
        args = self._report_args('wrong task report')
        args.task = 'T02'

        with self.assertRaisesRegex(
            WorkflowError, 'SESSION_TASK_MISMATCH',
        ):
            write_report(self.project, state, args, binding)

        self._assert_session_artifacts_equal(state, before)

    def test_reports_for_distinct_tasks_keep_independent_history_authority(self):
        state, context = self._register_report_worker()
        first_binding = load_binding(context)
        self.cmd(
            'task-add', 'work', 'T02', '--title', 'Second report task',
            '--acceptance', 'Each task retains its own latest report authority',
        )
        state = read(self.root / '.gsp/runs/work/state.json')
        second_handle = session_binding(
            self.root, state['work_uuid'], 'child', 'codex',
            'thread-report-worker-two',
        )
        register_session(self.project, state, parser().parse_args([
            'session-register', 'work', '--session', 'S03', '--role', 'worker',
            '--host', 'codex', '--thread', 'thread-report-worker-two',
            '--task', 'T02', '--worktree', str(self.root / 'report-worker-two'),
            '--write-mode', 'write', '--path', 'services/two.py',
            '--context', str(self.root / second_handle.handle_path),
        ]))
        second_binding = load_binding(self.root / second_handle.handle_path)
        state = read(self.root / '.gsp/runs/work/state.json')
        session_dir = self.root / state['doc_dir'] / 'sessions'

        write_report(
            self.project,
            state,
            self._report_args('first task initial report'),
            first_binding,
        )
        state = read(self.root / '.gsp/runs/work/state.json')
        second = self._report_args('second task report')
        second.session = 'S03'
        second.task = 'T02'
        second.files = ['services/two.py']
        write_report(self.project, state, second, second_binding)

        state = read(self.root / '.gsp/runs/work/state.json')
        result = write_report(
            self.project,
            state,
            self._report_args('first task replacement report'),
            first_binding,
        )

        self.assertEqual(result['plan_task'], 'T01')
        self.assertIn(
            b'first task replacement report',
            (session_dir / 'T01-report.md').read_bytes(),
        )
        self.assertIn(
            b'second task report',
            (session_dir / 'T02-report.md').read_bytes(),
        )
        self.assertEqual(
            len(list((session_dir / 'history').glob('T01-report-*.md'))),
            1,
        )

    def test_stale_report_contract_rejects_before_ready_refresh(self):
        state, context = self._register_report_worker()
        argv = [
            'session-report', 'work', 'T01', '--session', 'S02',
            '--status', 'DONE', '--summary', 'frozen old contract result',
            '--files', 'services/one.py', '--tests', '1 passed',
        ]
        with temporary_env({'GSP_SESSION_CONTEXT': str(context)}):
            dispatch(self.project, parser().parse_args(argv), enforce_context=True)
        state_file = self.root / '.gsp/runs/work/state.json'
        changed = read(state_file)
        changed['status'] = 'ready'
        changed['plan_seal'] = {'sha256': 'b' * 64, 'tasks_sha256': 'c' * 64}
        self.project.write(changed, 'fixture-contract-changed',
                           'stale ready fixture to exercise rejection before refresh',
                           action_uid='fixture-stale-contract-ready')
        before = self._session_artifact_snapshot(changed)
        with temporary_env({'GSP_SESSION_CONTEXT': str(context)}):
            with self.assertRaisesRegex(WorkflowError, 'SESSION_REPORT_REQUEST_CONFLICT'):
                dispatch(self.project, parser().parse_args(argv), enforce_context=True)
        self._assert_session_artifacts_equal(changed, before)

    def test_report_ready_self_invalidation_exact_retry_reuses_one_request(self):
        state, context = self._register_report_worker()
        state_file = self.root / '.gsp/runs/work/state.json'
        state = read(state_file)
        state['status'] = 'ready'
        self.project.write(
            state,
            'fixture-ready',
            'set up a valid ready request before response loss',
            action_uid='fixture-ready-response-loss',
        )
        argv = [
            'session-report', 'work', 'T01', '--session', 'S02',
            '--status', 'DONE', '--summary', 'ready response loss',
            '--files', 'services/one.py', '--tests', '1 passed',
        ]

        from gsp.runtime import project_command_effects as original_effects

        def lose_after_report_effect(project, frozen_state, doc, operation_uid, committed_at):
            original_effects(project, frozen_state, doc, operation_uid, committed_at)
            marker = frozen_state.get('_command_commit') or {}
            if ((marker.get('effects') or {}).get('report')) is not None:
                raise OSError('response lost after ready report commit')

        with temporary_env(
            {'GSP_SESSION_CONTEXT': str(context)},
        ):
            with patch(
                'gsp.runtime.project_command_effects',
                lose_after_report_effect,
            ):
                with self.assertRaisesRegex(
                    OSError, 'response lost after ready report commit',
                ):
                    dispatch(
                        self.project,
                        parser().parse_args(argv),
                        enforce_context=True,
                    )

            crashed_state = read(state_file)
            self.assertEqual(crashed_state['status'], 'needs-verification')
            crashed_marker = crashed_state['_command_commit']
            crashed = self._session_artifact_snapshot(state)
            result = dispatch(
                self.project,
                parser().parse_args(argv),
                enforce_context=True,
            )[0]

        self.assertTrue(result['replayed'])
        retried_state = read(state_file)
        self.assertEqual(retried_state['status'], 'needs-verification')
        self.assertEqual(retried_state['_command_commit'], crashed_marker)
        after_retry = self._session_artifact_snapshot(state)
        self.assertEqual(after_retry['state'], crashed['state'])
        self.assertEqual(after_retry['registry'], crashed['registry'])
        self.assertEqual(after_retry['files'], crashed['files'])
        self.assertEqual(after_retry['operations'], crashed['operations'])

    def test_report_publication_without_registry_completion_recovers_exactly(self):
        state, context = self._register_report_worker()
        args = self._report_args('report publication crash')
        registry_file = self.root / '.gsp/runs/work/sessions/index.json'
        report_dir = self.root / state['doc_dir'] / 'sessions'
        report_file = report_dir / 'T01-report.md'
        before_registry = read(registry_file)

        with patch(
            'gsp.sessions._save',
            side_effect=OSError('crash after report publication'),
        ):
            with self.assertRaisesRegex(OSError, 'report publication'):
                write_report(self.project, state, args, load_binding(context))

        crashed_state = read(self.root / '.gsp/runs/work/state.json')
        marker = crashed_state['_command_commit']
        self.assertEqual(marker['event'], 'session-report')
        self.assertTrue(report_file.is_file())
        self.assertEqual(read(registry_file), before_registry)
        crashed_report = report_file.read_bytes()

        result = write_report(self.project, crashed_state, args, load_binding(context))
        recovered_state = read(self.root / '.gsp/runs/work/state.json')
        recovered_registry = read(registry_file)
        self.assertEqual(result['status'], 'DONE')
        self.assertEqual(recovered_state['_command_commit'], marker)
        self.assertEqual(report_file.read_bytes(), crashed_report)
        self.assertEqual(list((report_dir / 'history').glob('T01-report-*.md')), [])
        self.assertEqual(len(recovered_registry['outbox']), 1)

        write_report(self.project, recovered_state, args, load_binding(context))
        replay_registry = read(registry_file)
        self.assertEqual(len(replay_registry['outbox']), 1)
        self.assertEqual(list((report_dir / 'history').glob('T01-report-*.md')), [])

    def test_pending_report_rejects_changed_summary_before_projection(self):
        state, context = self._register_report_worker()
        first = self._report_args('pending report original')
        registry_file = self.root / '.gsp/runs/work/sessions/index.json'
        session_dir = self.root / state['doc_dir'] / 'sessions'
        report_file = session_dir / 'T01-report.md'

        with patch(
            'gsp.sessions._save',
            side_effect=OSError('crash after pending report publication'),
        ):
            with self.assertRaisesRegex(OSError, 'pending report publication'):
                write_report(
                    self.project, state, first, load_binding(context),
                )

        state_file = self.root / '.gsp/runs/work/state.json'
        pending_state = read(state_file)
        before_state = state_file.read_bytes()
        before_registry = registry_file.read_bytes()
        before_files = {
            path.relative_to(session_dir).as_posix(): path.read_bytes()
            for path in session_dir.glob('**/*')
            if path.is_file()
        }

        with self.assertRaisesRegex(
            WorkflowError, 'SESSION_REPORT_REQUEST_CONFLICT',
        ):
            write_report(
                self.project,
                pending_state,
                self._report_args('pending report changed'),
                load_binding(context),
            )

        self.assertEqual(state_file.read_bytes(), before_state)
        self.assertEqual(registry_file.read_bytes(), before_registry)
        self.assertEqual(list((session_dir / 'history').glob('*.md')), [])
        self.assertEqual(
            {
                path.relative_to(session_dir).as_posix(): path.read_bytes()
                for path in session_dir.glob('**/*')
                if path.is_file()
            },
            before_files,
        )
        self.assertTrue(report_file.is_file())

    def test_report_response_loss_after_commit_does_not_duplicate_effects(self):
        state, context = self._register_report_worker()
        args = self._report_args('report response loss')
        registry_file = self.root / '.gsp/runs/work/sessions/index.json'
        report_dir = self.root / state['doc_dir'] / 'sessions'
        report_file = report_dir / 'T01-report.md'

        with patch.object(
            Project,
            'finish_write_projection',
            side_effect=OSError('response lost after report commit'),
        ):
            with self.assertRaisesRegex(OSError, 'response lost'):
                write_report(self.project, state, args, load_binding(context))

        crashed_state = read(self.root / '.gsp/runs/work/state.json')
        crashed_registry = read(registry_file)
        crashed_report = report_file.read_bytes()
        self.assertEqual(len(crashed_registry['outbox']), 1)

        write_report(self.project, crashed_state, args, load_binding(context))
        recovered_state = read(self.root / '.gsp/runs/work/state.json')
        recovered_registry = read(registry_file)
        self.assertEqual(recovered_state['_command_commit'], crashed_state['_command_commit'])
        self.assertEqual(report_file.read_bytes(), crashed_report)
        self.assertEqual(len(recovered_registry['outbox']), 1)
        self.assertEqual(list((report_dir / 'history').glob('T01-report-*.md')), [])

    def test_blocked_report_exact_replay_reuses_one_operation(self):
        state, context = self._register_report_worker()
        args = self._report_args('blocked report exact replay')
        args.status = 'BLOCKED'
        write_report(self.project, state, args, load_binding(context))

        state_file = self.root / '.gsp/runs/work/state.json'
        registry_file = self.root / '.gsp/runs/work/sessions/index.json'
        before = self._session_artifact_snapshot(state)
        result = write_report(
            self.project,
            read(state_file),
            args,
            load_binding(context),
        )

        self.assertTrue(result['replayed'])
        self.assertEqual(self._session_artifact_snapshot(state), before)
        registry = read(registry_file)
        self.assertEqual(registry['tasks']['T01']['report_status'], 'BLOCKED')
        self.assertEqual(len(registry['outbox']), 1)

    def test_completed_report_document_deletion_is_not_recreated_on_replay(self):
        state, context = self._register_report_worker()
        args = self._report_args('completed report document')
        write_report(self.project, state, args, load_binding(context))

        state_file = self.root / '.gsp/runs/work/state.json'
        registry_file = self.root / '.gsp/runs/work/sessions/index.json'
        session_dir = self.root / state['doc_dir'] / 'sessions'
        report_file = session_dir / 'T01-report.md'
        report_file.unlink()
        before_state = state_file.read_bytes()
        before_registry = registry_file.read_bytes()
        before_files = {
            path.relative_to(session_dir).as_posix(): path.read_bytes()
            for path in session_dir.glob('**/*')
            if path.is_file()
        }
        store = IdentityStore(self.project.root)
        before_operations, before_events = store.operation_count(), store.event_count()

        with self.assertRaisesRegex(
            WorkflowError, 'SESSION_REPORT_DOCUMENT_CONFLICT',
        ):
            write_report(
                self.project,
                read(state_file),
                args,
                load_binding(context),
            )

        self.assertFalse(report_file.exists())
        self.assertEqual(state_file.read_bytes(), before_state)
        self.assertEqual(registry_file.read_bytes(), before_registry)
        self.assertEqual(
            {
                path.relative_to(session_dir).as_posix(): path.read_bytes()
                for path in session_dir.glob('**/*')
                if path.is_file()
            },
            before_files,
        )
        self.assertEqual(store.operation_count(), before_operations)
        self.assertEqual(store.event_count(), before_events)

    def test_forced_report_action_uid_collision_leaves_effects_unchanged(self):
        state, context = self._register_report_worker()
        first = self._report_args('first forced request')
        forced_uid = 'act-' + ('0' * 64)
        write_report(
            self.project, state, first, load_binding(context),
            action_uid=forced_uid,
        )

        state_file = self.root / '.gsp/runs/work/state.json'
        registry_file = self.root / '.gsp/runs/work/sessions/index.json'
        session_dir = self.root / state['doc_dir'] / 'sessions'
        committed_state = read(state_file)
        before_state = state_file.read_bytes()
        before_registry = registry_file.read_bytes()
        before_files = {
            path.relative_to(session_dir).as_posix(): path.read_bytes()
            for path in session_dir.glob('**/*')
            if path.is_file()
        }

        changed = self._report_args('changed semantic request')
        with self.assertRaisesRegex(
            WorkflowError, 'SESSION_REPORT_REQUEST_CONFLICT',
        ):
            write_report(
                self.project, committed_state, changed, load_binding(context),
                action_uid=forced_uid,
            )

        self.assertEqual(state_file.read_bytes(), before_state)
        self.assertEqual(registry_file.read_bytes(), before_registry)
        self.assertEqual(
            {
                path.relative_to(session_dir).as_posix(): path.read_bytes()
                for path in session_dir.glob('**/*')
                if path.is_file()
            },
            before_files,
        )


    def _register_pair(self):
        first = session_binding(
            self.root, self.state['work_uuid'], 'main', 'codex', 'thread-a',
            capabilities={'accept', 'brief', 'claim', 'dispatch', 'handoff', 'report', 'write'},
        )
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-a', '--write-mode', 'write', '--path', '.',
            '--context', str(self.root / first.handle_path)]))
        second = session_binding(
            self.root, self.state['work_uuid'], 'main', 'codex', 'thread-b',
            capabilities={'accept', 'brief', 'claim', 'dispatch', 'handoff', 'report', 'write'},
        )
        return first, second

    def _assert_recovered_transition(self, first, second, operation, expect_takeover):
        current = read(self.root / '.gsp/runs/work/state.json')
        recovered = _recover_controller_transitions(self.project, current)
        self.assertIn(operation, recovered)
        again = _recover_controller_transitions(self.project, current)
        self.assertEqual(again, [])
        old = load_binding(self.root / first.handle_path)
        new = load_binding(self.root / second.handle_path)
        self.assertTrue(old.cancelled)
        self.assertFalse(new.cancelled)
        self.assertGreater(new.controller_generation, old.controller_generation)
        registry = read(self.root / '.gsp/runs/work/sessions/index.json')
        rows = [row for row in registry['sessions'] if row['id'] in {'S01', 'S02'}]
        self.assertEqual(len(rows), 2)
        old_row = next(row for row in rows if row['id'] == 'S01')
        new_row = next(row for row in rows if row['id'] == 'S02')
        self.assertEqual(old_row['status'], 'handed-off')
        self.assertEqual(new_row['role'], 'controller')
        self.assertEqual(registry['controller_session'], 'S02')
        workspace = str(Path(new_row['worktree']).resolve())
        self.assertEqual(
            next(value for key, value in registry['workspace_leases'].items()
                 if Path(key).resolve() == Path(workspace)),
            'S02',
        )
        self.assertEqual(
            sum(row['role'] == 'controller' and row['status'] in {'active', 'waiting', 'reported'}
                for row in rows),
            1,
        )
        with self.assertRaisesRegex(WorkflowError, 'SESSION_BINDING_STALE'):
            require_capability(old, 'write', self.state['work_uuid'])
        require_capability(new, 'write', self.state['work_uuid'])
        state = IdentityStore(self.root).read_session_state(new.binding_uid)
        self.assertEqual(state['controller_generation'], new.controller_generation)
        if expect_takeover:
            self.assertEqual(new_row['context_binding']['binding_uid'], second.binding_uid)

    def _assert_three_handoff_crash_windows(self):
        for window in ('after_db_commit', 'between_handles', 'before_registry_save'):
            with self.subTest(window=window):
                # Each scenario gets a fresh project so a crash cannot affect the next.
                if window != 'after_db_commit':
                    self.tearDown()
                    self.setUp()
                first, second = self._register_pair()
                register_session(self.project, self.state, parser().parse_args([
                    'session-register', 'work', '--session', 'S02', '--role', 'observer',
                    '--host', 'codex', '--thread', 'thread-b', '--write-mode', 'read-only',
                    '--context', str(self.root / second.handle_path)]))
                from gsp import sessions as sessions_module
                original_persist = sessions_module._persist_binding_handles
                original_save = sessions_module._save
                calls = {'handles': 0}

                def crash_handles(project, *bindings):
                    if window == 'after_db_commit':
                        raise OSError('crash after database commit')
                    if window == 'between_handles':
                        calls['handles'] += 1
                        if calls['handles'] == 1:
                            original_persist(project, bindings[0])
                            raise OSError('crash between handles')
                    return original_persist(project, *bindings)

                def crash_save(project, state, registry):
                    if window == 'before_registry_save':
                        raise OSError('crash before registry save')
                    return original_save(project, state, registry)

                with patch.object(sessions_module, '_persist_binding_handles', side_effect=crash_handles), \
                     patch.object(sessions_module, '_save', side_effect=crash_save):
                    with self.assertRaisesRegex(OSError, 'crash'):
                        handoff_controller(self.project, self.state, parser().parse_args([
                            'session-handoff', 'work', '--from', 'S01', '--to', 'S02',
                            '--reason', window]))
                intents = list((self.root / '.gsp/orchestration/controller-transition-intents').glob('*.json'))
                self.assertEqual(len(intents), 1)
                operation = read(intents[0])['operation_uid']
                self._assert_recovered_transition(first, second, operation, False)

    def test_handoff_recovers_after_db_commit_handle_writes_and_registry_crash(self):
        self._assert_three_handoff_crash_windows()

    def _assert_takeover_three_crash_windows(self):
        for window in ('after_db_commit', 'between_handles', 'before_registry_save'):
            with self.subTest(window=window):
                if window != 'after_db_commit':
                    self.tearDown()
                    self.setUp()
                first = session_binding(
                    self.root, self.state['work_uuid'], 'main', 'codex', 'thread-a',
                    capabilities={'accept', 'brief', 'claim', 'dispatch', 'handoff', 'report', 'write'},
                )
                register_session(self.project, self.state, parser().parse_args([
                    'session-register', 'work', '--role', 'controller', '--host', 'codex',
                    '--thread', 'thread-a', '--write-mode', 'write', '--path', '.',
                    '--context', str(self.root / first.handle_path)]))
                second = session_binding(
                    self.root, self.state['work_uuid'], 'main', 'codex', 'thread-b',
                    capabilities={'accept', 'brief', 'claim', 'dispatch', 'handoff', 'report', 'write'},
                )
                from gsp import sessions as sessions_module
                original_persist = sessions_module._persist_binding_handles
                original_save = sessions_module._save
                calls = {'handles': 0}

                def crash_handles(project, *bindings):
                    if window == 'after_db_commit':
                        raise OSError('crash after database commit')
                    if window == 'between_handles':
                        calls['handles'] += 1
                        if calls['handles'] == 1:
                            original_persist(project, bindings[0])
                            raise OSError('crash between handles')
                    return original_persist(project, *bindings)

                def crash_save(project, state, registry):
                    if window == 'before_registry_save':
                        raise OSError('crash before registry save')
                    return original_save(project, state, registry)

                with patch.object(sessions_module, '_persist_binding_handles', side_effect=crash_handles), \
                     patch.object(sessions_module, '_save', side_effect=crash_save):
                    with self.assertRaisesRegex(OSError, 'crash'):
                        register_session(self.project, self.state, parser().parse_args([
                            'session-register', 'work', '--session', 'S02', '--role', 'controller',
                            '--host', 'codex', '--thread', 'thread-b', '--write-mode', 'write',
                            '--path', '.', '--takeover', '--reason', window,
                            '--context', str(self.root / second.handle_path),
                        ]))
                intents = list((self.root / '.gsp/orchestration/controller-transition-intents').glob('*.json'))
                self.assertEqual(len(intents), 1)
                operation = read(intents[0])['operation_uid']
                self._assert_recovered_transition(first, second, operation, True)

    def test_takeover_recovers_after_db_commit_handle_writes_and_registry_crash(self):
        self._assert_takeover_three_crash_windows()


    def test_completed_transition_is_not_replayed_during_later_handoff(self):
        first = session_binding(
            self.root, self.state['work_uuid'], 'main', 'codex', 'thread-a',
            capabilities={'accept', 'brief', 'claim', 'dispatch', 'handoff', 'report', 'write'},
        )
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-a', '--write-mode', 'write', '--path', '.',
            '--context', str(self.root / first.handle_path)]))
        second = session_binding(
            self.root, self.state['work_uuid'], 'main', 'codex', 'thread-b',
            capabilities={'accept', 'brief', 'claim', 'dispatch', 'handoff', 'report', 'write'},
        )
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--session', 'S02', '--role', 'observer',
            '--host', 'codex', '--thread', 'thread-b', '--write-mode', 'read-only',
            '--context', str(self.root / second.handle_path)]))
        handoff_controller(self.project, self.state, parser().parse_args([
            'session-handoff', 'work', '--from', 'S01', '--to', 'S02',
            '--reason', 'first handoff',
        ]))

        third = session_binding(
            self.root, self.state['work_uuid'], 'main', 'codex', 'thread-c',
            capabilities={'accept', 'brief', 'claim', 'dispatch', 'handoff', 'report', 'write'},
        )
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--session', 'S03', '--role', 'observer',
            '--host', 'codex', '--thread', 'thread-c', '--write-mode', 'read-only',
            '--context', str(self.root / third.handle_path)]))
        handoff_controller(self.project, self.state, parser().parse_args([
            'session-handoff', 'work', '--from', 'S02', '--to', 'S03',
            '--reason', 'second handoff',
        ]))

        current = read(self.root / '.gsp/runs/work/state.json')
        self.assertEqual(_recover_controller_transitions(self.project, current), [])
        registry = read(self.root / '.gsp/runs/work/sessions/index.json')
        self.assertEqual(registry['controller_session'], 'S03')
        self.assertEqual(
            [row['status'] for row in registry['sessions']],
            ['handed-off', 'handed-off', 'active'],
        )
        self.assertEqual(
            next(value for key, value in registry['workspace_leases'].items()
                 if Path(key).resolve() == self.root.resolve()),
            'S03',
        )

    def test_committed_transition_with_tampered_old_handle_fails_finitely(self):
        first = session_binding(
            self.root, self.state['work_uuid'], 'main', 'codex', 'thread-a',
            capabilities={'accept', 'brief', 'claim', 'dispatch', 'handoff', 'report', 'write'},
        )
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-a', '--write-mode', 'write', '--path', '.',
            '--context', str(self.root / first.handle_path)]))
        second = session_binding(
            self.root, self.state['work_uuid'], 'main', 'codex', 'thread-b',
            capabilities={'accept', 'brief', 'claim', 'dispatch', 'handoff', 'report', 'write'},
        )
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--session', 'S02', '--role', 'observer',
            '--host', 'codex', '--thread', 'thread-b', '--write-mode', 'read-only',
            '--context', str(self.root / second.handle_path)]))
        handoff_controller(self.project, self.state, parser().parse_args([
            'session-handoff', 'work', '--from', 'S01', '--to', 'S02',
            '--reason', 'completed transition fixture',
        ]))

        handle = self.root / first.handle_path
        payload = read(handle)
        payload['binding']['cancelled'] = False
        atomic(handle, payload)
        with self.assertRaisesRegex(WorkflowError, 'SESSION_CONTEXT_TAMPERED'):
            load_binding(handle)


if __name__ == '__main__':
    unittest.main()
