"""Session binding, capability and legacy-entry boundary tests."""
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from gsp.codex_hooks import handle
from gsp.documents import Governance
from gsp.identity_store import IdentityStore
from gsp.orchestration_types import RequestRef, digest_value
from gsp.runtime import Project, WorkflowError, atomic, dispatch, parser, read
from gsp.session_context import (
    NativeModelObservation,
    create_binding,
    establish_initial_controller,
    issue_delegate_binding,
    load_binding,
    require_capability,
    require_decision_controller,
    require_native_model_role,
    record_native_model_observation,
    resume_pending_controller,
)
from gsp.sessions import authorize_edit, handoff_controller, register_session


class SessionContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-session-context-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Session Context Test')
        self.git('config', 'user.email', 'session-context@example.invalid')
        (self.root / 'README.md').write_text('# context\n', encoding='utf-8')
        self.git('add', 'README.md')
        self.git('commit', '-qm', 'chore: baseline')
        self.project = Project(self.root)
        self.project.initialize()
        self.store = IdentityStore(self.project.root)
        self.cmd('new', 'work', '--title', 'Session context', '--intent', 'cross-session')
        self.state = read(self.project.root / '.gsp/runs/work/state.json')
        self.work_uid = self.state['work_uuid']

    def git(self, *args):
        result = subprocess.run(
            ['git', *args], cwd=self.root, capture_output=True, text=True,
            encoding='utf-8',
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def cmd(self, *argv):
        return dispatch(self.project, parser().parse_args(list(argv)))[0]

    def public_cli(self, *argv, context=None):
        import os
        import sys
        from process_helpers import run
        env = os.environ.copy()
        env.pop('GSP_SESSION_CONTEXT', None)
        if context is not None:
            env['GSP_SESSION_CONTEXT'] = str(context)
        result = run([
            sys.executable, '-X', 'utf8', '-B',
            str(Path(__file__).resolve().parents[1] / 'scripts' / 'gsp.py'),
            '--project', str(self.root), *argv,
        ], capture_output=True, text=True, encoding='utf-8', env=env)
        return result

    def _public_handoff_pair(self, perform=True):
        from process_helpers import public_main_binding
        first = public_main_binding(self.root, self.work_uid, 'public-first')
        first_path = self.root / first.handle_path
        result = self.public_cli(
            'session-register', 'work', '--session', 'S01',
            '--role', 'controller', '--host', 'codex', '--thread', 'public-first',
            '--write-mode', 'write', '--path', 'services', '--context', str(first_path),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        second = public_main_binding(
            self.root, self.work_uid, 'public-second', first_path,
        )
        second_path = self.root / second.handle_path
        result = self.public_cli(
            'session-register', 'work', '--session', 'S02', '--role', 'observer',
            '--host', 'codex', '--thread', 'public-second', '--write-mode', 'read-only',
            '--context', str(second_path), context=first_path,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        command = ('session-handoff', 'work', '--from', 'S01', '--to', 'S02',
                   '--reason', 'Public main role handoff')
        if perform:
            result = self.public_cli(*command, context=first_path)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return first, second, first_path, second_path, command

    def _pending_public_handoff_pair(self):
        from unittest import mock
        first, second, first_path, second_path, command = self._public_handoff_pair(perform=False)
        with mock.patch('gsp.sessions._sync_controller_transition',
                        side_effect=OSError('pending before projections')):
            with self.assertRaisesRegex(Exception, 'pending before projections'):
                handoff_controller(self.project, self.project.ledger_state('work'),
                                   parser().parse_args(command), caller_binding=first)
        self.assertEqual(self.store.read_work_controller(self.work_uid)['binding_uid'], second.binding_uid)
        self.assertEqual(read(second_path)['binding']['controller_generation'], 1)
        return first, second, first_path, second_path, command

    def _controller_snapshot(self):
        import hashlib
        from gsp.telemetry import telemetry_path
        files = {}
        database_files = {self.store.path.name, self.store.path.name + '-wal',
                          self.store.path.name + '-shm'}
        for directory in (self.project.meta, self.project.paths('work')[1]):
            for path in directory.rglob('*'):
                if (path.is_file() and path.name not in database_files
                        and path.resolve() != telemetry_path(self.root)):
                    # A CLI-attempt observation is separate from business
                    # effects. Keep every owner ledger, event and document.
                    files[path.relative_to(self.root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        connection = sqlite3.connect(self.store.path.as_uri() + '?mode=ro', uri=True)
        try:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )]
            rows = {table: hashlib.sha256(repr(sorted(connection.execute(
                'SELECT * FROM "' + table.replace('"', '""') + '"'
            ).fetchall(), key=repr)).encode('utf-8')).hexdigest() for table in tables}
        finally:
            connection.close()
        return files, rows

    def test_public_main_chat_handoff_revokes_old_controller(self):
        first, second, first_path, second_path, _ = self._public_handoff_pair()
        current = load_binding(second_path)
        self.assertEqual(current.controller_generation, first.controller_generation + 1)
        self.assertTrue(load_binding(first_path).cancelled)
        accepted = self.public_cli(
            'decision', 'work', '--source', 'agent', '--text', 'New controller decides.',
            context=second_path,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        state_file = self.project.meta / 'runs' / 'work' / 'state.json'
        before = state_file.read_bytes()
        rejected = self.public_cli(
            'decision', 'work', '--source', 'agent', '--text', 'Revoked controller.',
            context=first_path,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('SESSION_BINDING_STALE', rejected.stderr)
        self.assertEqual(state_file.read_bytes(), before)

    def test_successor_replays_completed_handoff_without_any_effect(self):
        _, _, _, second_path, command = self._public_handoff_pair()
        before = self._controller_snapshot()
        replay = self.public_cli(*command, context=second_path)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        result = json.loads(replay.stdout)
        self.assertEqual(result['status'], 'handed-off')
        self.assertTrue(result['replayed'])
        self.assertEqual(self._controller_snapshot(), before)

    def test_rejected_handoff_retry_never_repairs_completed_projection(self):
        import time
        from process_helpers import public_main_binding
        _, _, first_path, second_path, command = self._public_handoff_pair()
        time.sleep(1.05)  # Make an accidental timestamp rewrite observable.
        before = self._controller_snapshot()
        old = self.public_cli(*command, context=first_path)
        self.assertNotEqual(old.returncode, 0)
        self.assertIn('SESSION_BINDING_STALE', old.stderr)
        self.assertEqual(self._controller_snapshot(), before)
        changed = self.public_cli(*(command[:-1] + ('Changed handoff input',)), context=second_path)
        self.assertNotEqual(changed.returncode, 0)
        self.assertEqual(self._controller_snapshot(), before)
        third = public_main_binding(self.root, self.work_uid, 'standby-replay-caller', second_path)
        before = self._controller_snapshot()
        wrong = self.public_cli(*command, context=self.root / third.handle_path)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertEqual(self._controller_snapshot(), before)

    def test_pending_handoff_rejects_wrong_work_before_context_repair(self):
        self.cmd('new', 'other', '--title', 'Other work must remain separate')
        _, _, _, second_path, _ = self._pending_public_handoff_pair()
        before = self._controller_snapshot()
        wrong = self.public_cli('task-add', 'other', 'T01', '--title', 'Wrong work',
                                '--acceptance', 'Reject without repair', context=second_path)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn('WORK_UID_MISMATCH', wrong.stderr)
        self.assertEqual(self._controller_snapshot(), before)

    def test_pending_handoff_rejects_wrong_actor_before_context_repair(self):
        _, _, _, second_path, _ = self._pending_public_handoff_pair()
        before = self._controller_snapshot()
        wrong = self.public_cli('session-claim', 'work', '--session', 'S01',
                                '--path', 'services', context=second_path)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn('SESSION_ACTOR_MISMATCH', wrong.stderr)
        self.assertEqual(self._controller_snapshot(), before)

    def test_successor_finishes_exact_pending_handoff_then_replays_without_effect(self):
        _, second, _, second_path, command = self._pending_public_handoff_pair()
        resumed = self.public_cli(*command, context=second_path)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        registry = read(self.project.meta / 'runs/work/sessions/index.json')
        self.assertEqual(registry['controller_session'], 'S02')
        self.assertEqual(load_binding(second_path).binding_uid, second.binding_uid)
        before = self._controller_snapshot()
        replay = self.public_cli(*command, context=second_path)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        self.assertEqual(self._controller_snapshot(), before)

    def test_initial_controller_preflight_rejects_mismatched_intent_without_writes(self):
        from gsp.sessions import resume_pending_controller as resume_project_controller

        binding = self.binding(thread='preflight-boundary')
        record = {
            'id': 'S01', 'role': 'controller', 'host': binding.host,
            'thread_id': binding.host_thread_id, 'task': None, 'branch': None,
            'worktree': str(self.root), 'write_mode': 'write',
            'claimed_paths': ['.'], 'status': 'active',
            'context': binding.handle_path, 'context_mode': 'bound',
            'binding_uid': binding.binding_uid, 'work_uid': binding.work_uid,
            'controller_generation': binding.controller_generation,
            'cancel_generation': binding.cancel_generation,
            'context_binding': binding.to_dict(),
        }
        intent_path = (
            self.project.meta / 'orchestration' /
            'controller-transition-intents' / 'preflight-boundary.json'
        )

        def persist_intent(next_generation, record_work_uid):
            payload = {
                'task': 'work', 'work_uid': binding.work_uid,
                'operation': 'establish-controller', 'old_session': None,
                'new_session': 'S01', 'old_binding': None,
                'new_binding': binding.to_dict(),
                'next_generation': next_generation,
                'reason': 'preflight boundary fixture',
                'new_record': {**record, 'work_uid': record_work_uid},
            }
            atomic(intent_path, {
                'schema': 1,
                'payload': payload,
                'payload_digest': digest_value(payload),
            })

        for label, generation, record_work_uid in (
            ('generation', binding.controller_generation + 1, binding.work_uid),
            ('record-work', binding.controller_generation, 'other-work'),
        ):
            with self.subTest(label=label):
                persist_intent(generation, record_work_uid)
                before_intent = intent_path.read_bytes()
                before_owner = self.store.read_work_controller(binding.work_uid)
                before_operations = self.store.operation_count()

                with self.assertRaisesRegex(
                    WorkflowError, 'CONTROLLER_TRANSITION_INTENT_TAMPERED',
                ):
                    resume_project_controller(
                        self.project, self.state, binding, preflight=True,
                    )
                with self.assertRaisesRegex(
                    WorkflowError, 'CONTROLLER_TRANSITION_INTENT_TAMPERED',
                ):
                    resume_project_controller(
                        self.project, self.state, binding, preflight=False,
                    )

                self.assertEqual(intent_path.read_bytes(), before_intent)
                self.assertEqual(
                    self.store.read_work_controller(binding.work_uid),
                    before_owner,
                )
                self.assertEqual(
                    self.store.operation_count(), before_operations,
                )

    def test_pre_operation_controller_intent_recovery_finishes_projection(self):
        from gsp.sessions import (
            _new_transition_intent,
            _recover_controller_transitions,
        )

        self.cmd(
            'task-add', 'work', 'T01', '--title', 'Recovery task',
            '--acceptance', 'controller projection is recovered',
        )
        self.state = read(self.project.meta / 'runs/work/state.json')
        first = self.binding(
            thread='recovery-first',
            capabilities={
                'accept', 'brief', 'claim', 'dispatch', 'handoff', 'plan',
                'report', 'write',
            },
        )
        self.register(first)
        second = self.binding(
            thread='recovery-second',
            capabilities={
                'accept', 'brief', 'claim', 'dispatch', 'handoff', 'plan',
                'report', 'write',
            },
        )
        self.register(second, role='observer', mode='read-only')
        record = {
            'id': 'S02', 'role': 'controller', 'host': second.host,
            'thread_id': second.host_thread_id, 'task': None, 'branch': None,
            'worktree': str(self.root), 'write_mode': 'write',
            'claimed_paths': ['.'], 'status': 'active',
            'context': second.handle_path, 'context_mode': 'bound',
            'binding_uid': second.binding_uid, 'work_uid': second.work_uid,
            'controller_generation': second.controller_generation,
            'cancel_generation': second.cancel_generation,
            'context_binding': second.to_dict(),
        }
        envelope = _new_transition_intent(
            self.project, self.state, 'takeover', first, second, 2,
            'S01', 'S02', 'pre-operation crash recovery', new_record=record,
        )
        intent_path = next(
            (self.project.meta / 'orchestration' /
             'controller-transition-intents').glob('*.json')
        )
        self.assertIsNone(read(intent_path).get('operation_uid'))

        recovered = _recover_controller_transitions(self.project, self.state)

        self.assertEqual(len(recovered), 1)
        self.assertEqual(
            _recover_controller_transitions(self.project, self.state), [],
        )
        operation = self.store.get_operation(recovered[0])
        self.assertEqual(operation.phase, 'committed')
        registry = read(self.project.meta / 'runs/work/sessions/index.json')
        self.assertEqual(registry['controller_session'], 'S02')
        self.assertEqual(
            next(row['status'] for row in registry['sessions']
                 if row['id'] == 'S01'),
            'handed-off',
        )
        self.assertEqual(
            self.store.read_work_controller(self.work_uid)['binding_uid'],
            second.binding_uid,
        )
        self.assertTrue(self.store.read_session_state(first.binding_uid)['cancelled'])
        self.assertFalse(self.store.read_session_state(second.binding_uid)['cancelled'])

    def test_pending_handoff_wrong_work_registration_cannot_repair_context(self):
        self.cmd('new', 'other', '--title', 'Other registration target', '--intent', 'cross-session')
        _, _, _, second_path, _ = self._pending_public_handoff_pair()
        before = self._controller_snapshot()
        rejected = self.public_cli(
            'session-register', 'other', '--session', 'S03', '--role', 'observer',
            '--host', 'codex', '--thread', 'public-second', '--write-mode', 'read-only',
            '--context', str(second_path),
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('WORK_UID_MISMATCH', rejected.stderr)
        self.assertEqual(self._controller_snapshot(), before)

    def test_only_one_public_single_session_main_can_plan_without_snn(self):
        from process_helpers import public_main_binding
        self.cmd('new', 'solo', '--title', 'Single main ownership')
        state = self.project.ledger_state('solo')
        first = public_main_binding(self.root, state['work_uuid'], 'solo-first')
        first_path = self.root / first.handle_path
        second = public_main_binding(
            self.root, state['work_uuid'], 'solo-standby', first_path,
        )
        first_result = self.public_cli(
            'task-add', 'solo', 'T01', '--title', 'First controller',
            '--acceptance', 'Behavior covered', context=first_path,
        )
        self.assertEqual(first_result.returncode, 0, first_result.stderr)
        state_file = self.project.meta / 'runs' / 'solo' / 'state.json'
        event_file = state_file.parent / 'events.jsonl'
        before = (state_file.read_bytes(), event_file.read_bytes(), self.store.operation_count())
        rejected = self.public_cli(
            'task-add', 'solo', 'T02', '--title', 'Standby must not plan',
            '--acceptance', 'No effect', context=self.root / second.handle_path,
        )
        self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
        self.assertRegex(rejected.stderr, 'SESSION_CONTROLLER_REQUIRED|CONTROLLER_LEASE_REQUIRED')
        self.assertEqual((state_file.read_bytes(), event_file.read_bytes(), self.store.operation_count()), before)
        self.assertFalse((state_file.parent / 'sessions' / 'index.json').exists())

    def test_legacy_placeholder_does_not_occupy_controller_or_lease(self):
        from process_helpers import public_main_binding
        legacy = self.public_cli(
            'session-register', 'work', '--session', 'S01', '--role', 'controller',
            '--host', 'codex', '--thread', 'legacy-placeholder', '--write-mode', 'write',
            '--path', 'services',
        )
        self.assertEqual(legacy.returncode, 0, legacy.stdout + legacy.stderr)
        registry_path = self.project.meta / 'runs' / 'work' / 'sessions' / 'index.json'
        registry = read(registry_path)
        self.assertIsNone(registry.get('controller_session'))
        self.assertEqual(registry['workspace_leases'], {})
        self.assertEqual(registry['sessions'][0]['context_mode'], 'legacy-read-only')
        binding = public_main_binding(self.root, self.work_uid, 'real-controller')
        current = self.public_cli(
            'session-register', 'work', '--session', 'S02', '--role', 'controller',
            '--host', 'codex', '--thread', 'real-controller', '--write-mode', 'write',
            '--path', 'services', '--context', str(self.root / binding.handle_path),
        )
        self.assertEqual(current.returncode, 0, current.stdout + current.stderr)
        decision = self.public_cli(
            'decision', 'work', '--source', 'agent', '--text', 'Bound controller.',
            context=self.root / binding.handle_path,
        )
        self.assertEqual(decision.returncode, 0, decision.stdout + decision.stderr)

    def test_imported_legacy_placeholder_does_not_block_real_bootstrap(self):
        from process_helpers import public_main_binding
        registered = self.public_cli(
            'session-register', 'work', '--session', 'S01', '--role', 'controller',
            '--host', 'codex', '--thread', 'legacy-import', '--write-mode', 'write',
            '--path', 'services',
        )
        self.assertEqual(registered.returncode, 0, registered.stderr)
        registry_path = self.project.meta / 'runs/work/sessions/index.json'
        registry = read(registry_path)
        # Older GSP stored the requested write mode and controller projection
        # even though the context-free row never held a real workspace lease.
        registry['controller_session'] = 'S01'
        registry['sessions'][0]['write_mode'] = 'write'
        atomic(registry_path, registry)
        binding = public_main_binding(self.root, self.work_uid, 'import-successor')
        result = self.public_cli(
            'session-register', 'work', '--session', 'S02', '--role', 'controller',
            '--host', 'codex', '--thread', 'import-successor', '--write-mode', 'write',
            '--path', 'services', '--context', str(self.root / binding.handle_path),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(read(registry_path)['controller_session'], 'S02')

    def test_concurrent_main_bindings_reserve_one_control_lease(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.binding, thread='concurrent-' + str(i)) for i in range(2)]
            bindings = [future.result() for future in futures]
        owner = self.store.read_work_controller(self.work_uid)
        self.assertIn(owner['binding_uid'], {binding.binding_uid for binding in bindings})
        for binding in bindings:
            if binding.binding_uid == owner['binding_uid']:
                require_decision_controller(self.store, binding, self.work_uid)
            else:
                with self.assertRaisesRegex(WorkflowError, 'SESSION_CONTROLLER_REQUIRED'):
                    require_decision_controller(self.store, binding, self.work_uid)

    def test_initial_controller_request_replays_exactly_and_rejects_second_owner(self):
        from unittest import mock
        def request(label):
            key = 'initial-owner:' + label
            return RequestRef(
                request_uid=digest_value({'project_uid': self.store.project_uid(), 'logical_key': key}),
                logical_key=key, input_digest=digest_value({'work_uid': self.work_uid}),
                project_uid=self.store.project_uid(), work_uid=self.work_uid,
                attempt_uid=None, operation_kind='initial-controller',
            )
        first_request, second_request = request('first'), request('second')
        capabilities = frozenset({'plan', 'write', 'handoff'})
        first = establish_initial_controller(
            self.store, first_request, self.work_uid, 'codex', 'first-owner', capabilities, 1,
        )
        path = self.root / first.handle_path
        original = path.read_bytes()
        with mock.patch('gsp.session_context.now', return_value='2099-01-01T00:00:00+00:00'):
            replay = establish_initial_controller(
                self.store, first_request, self.work_uid, 'codex', 'first-owner', capabilities, 1,
            )
        self.assertEqual(replay.to_dict(), first.to_dict())
        self.assertEqual(path.read_bytes(), original)
        before_owner = self.store.read_work_controller(self.work_uid)
        with self.assertRaisesRegex(WorkflowError, 'CONTROLLER_LEASE_HELD'):
            establish_initial_controller(
                self.store, second_request, self.work_uid, 'codex', 'second-owner', capabilities, 1,
            )
        self.assertIsNone(self.store.read_controller_pending(second_request.request_uid))
        self.assertEqual(self.store.read_work_controller(self.work_uid), before_owner)

    def test_public_uppercase_work_id_can_bind_and_plan(self):
        from process_helpers import public_main_binding
        created = self.public_cli('new', 'GSP-999999', '--title', 'Canonical uppercase work')
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        state = self.project.ledger_state('GSP-999999')
        binding = public_main_binding(self.root, state['work_uuid'], 'uppercase-main')
        result = self.public_cli(
            'task-add', 'GSP-999999', 'T01', '--title', 'First uppercase task',
            '--acceptance', 'Public binding can plan', context=self.root / binding.handle_path,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.project.ledger_state('GSP-999999')['tasks']), 1)
        self.assertEqual(self.store.read_work_controller(state['work_uuid'])['run_id'], 'GSP-999999')

    def test_invalid_work_record_is_rejected_before_binding_publication(self):
        state_file = self.project.meta / 'runs/work/state.json'
        invalid = read(state_file)
        invalid['id'] = '../invalid-controller-path'
        atomic(state_file, invalid)
        def snapshot():
            connection = sqlite3.connect(str(self.store.path))
            try:
                tables = {table: connection.execute(
                    'SELECT * FROM ' + table + ' ORDER BY 1, 2'
                ).fetchall() for table in ('objects', 'requests', 'sequences', 'metadata')}
            finally:
                connection.close()
            handles = self.project.meta / 'runs/session-context'
            paths = {str(path.relative_to(handles)): path.read_bytes() if path.is_file() else None
                     for path in handles.rglob('*')} if handles.exists() else {}
            return tables, paths, state_file.read_bytes()
        before = snapshot()
        with self.assertRaisesRegex(WorkflowError, 'INVALID_ID|WORK_UID_MISMATCH|CONTROLLER_RECORD_INVALID'):
            self.binding(thread='must-not-publish')
        self.assertEqual(snapshot(), before)

    def binding(self, kind='main', host='codex', thread='thread-main',
                capabilities=None, work_uid=None):
        return create_binding(
            self.store,
            work_uid or self.work_uid,
            kind,
            host,
            thread,
            frozenset(capabilities or {'accept', 'claim', 'plan', 'report', 'write'}),
        )

    def register(self, binding, role='controller', path='services', mode='write',
                 takeover=False):
        argv = [
            'session-register', 'work', '--role', role, '--host', binding.host,
            '--thread', binding.host_thread_id or 'unknown', '--write-mode', mode,
            '--context', str(binding.handle_path),
        ]
        if takeover:
            argv.extend(['--takeover', '--reason', 'explicit controller takeover'])
        if role != 'controller':
            argv.extend(['--task', 'T01'])
        if mode == 'write':
            argv.extend(['--path', path])
        return register_session(
            self.project, self.state, parser().parse_args(argv),
        )

    def test_active_switch_does_not_rebind(self):
        first = self.binding()
        self.register(first)
        self.cmd('new', 'other', '--title', 'Other work', '--intent', 'cross-session')
        self.assertEqual(read(self.project.root / '.gsp/active.json')['task'], 'other')
        loaded = load_binding(self.project.root / first.handle_path)
        self.assertEqual(loaded.work_uid, self.work_uid)
        self.assertEqual(loaded.project_uid, self.store.project_uid())
        self.assertEqual(loaded.binding_uid, first.binding_uid)

    def test_single_session_main_context_can_write_without_formal_session(self):
        import contextlib
        import io
        from process_helpers import temporary_env
        from gsp.runtime import main

        self.cmd('new', 'solo', '--title', 'Single session fixture')
        state = self.project.ledger_state('solo')
        binding = create_binding(
            self.store, state['work_uuid'], 'main', 'codex', 'solo-thread',
            frozenset({'write', 'plan'}),
        )
        registry = self.project.meta / 'runs' / 'solo' / 'sessions' / 'index.json'
        self.assertFalse(registry.exists())
        output, error = io.StringIO(), io.StringIO()
        with (temporary_env({'GSP_SESSION_CONTEXT': str(self.project.root / binding.handle_path)}),
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(error)):
            code = main([
                '--project', str(self.project.root), 'task-add', 'solo', 'T01',
                '--title', 'Local change', '--acceptance', 'Behavior test passes',
            ])
        self.assertEqual(code, 0, (output.getvalue(), error.getvalue()))
        self.assertEqual(self.project.ledger_state('solo')['tasks'][0]['id'], 'T01')
        self.assertFalse(registry.exists())

    def test_helper_cannot_accept(self):
        self.cmd('task-add', 'work', 'T01', '--title', 'Task', '--acceptance', 'test')
        helper = self.binding(
            kind='helper', thread='thread-helper',
            capabilities={'claim', 'report', 'write'},
        )
        require_capability(helper, 'write', self.work_uid)
        with self.assertRaisesRegex(WorkflowError, 'CAPABILITY_REQUIRED accept'):
            require_capability(helper, 'accept', self.work_uid)
        with self.assertRaisesRegex(WorkflowError, 'WORK_UID_MISMATCH'):
            require_capability(helper, 'write', 'other-work')

    def test_takeover_invalidates_binding(self):
        first = self.binding(
            capabilities={'accept', 'handoff', 'claim', 'report', 'write'},
        )
        self.register(first)
        takeover = self.binding(
            thread='thread-takeover',
            capabilities={'accept', 'handoff', 'claim', 'report', 'write'},
        )
        second = self.register(takeover, role='controller', takeover=True)
        self.assertGreater(
            load_binding(self.project.root / second['session']['context']).controller_generation,
            load_binding(self.project.root / first.handle_path).controller_generation,
        )
        with self.assertRaisesRegex(WorkflowError, 'SESSION_BINDING_STALE'):
            require_capability(
                load_binding(self.project.root / first.handle_path),
                'write', self.work_uid,
            )

    def test_valid_main_to_main_handoff_uses_authoritative_generation(self):
        self.cmd('task-add', 'work', 'T01', '--title', 'Task', '--acceptance', 'test')
        self.state = read(self.project.root / '.gsp/runs/work/state.json')
        old = self.binding(
            capabilities={'accept', 'brief', 'claim', 'dispatch', 'handoff', 'report', 'write'},
        )
        self.register(old)
        new = self.binding(
            thread='thread-next-main',
            capabilities={'accept', 'brief', 'claim', 'dispatch', 'handoff', 'report', 'write'},
        )
        self.register(new, role='observer', mode='read-only')
        before = load_binding(self.project.root / old.handle_path)
        result = handoff_controller(self.project, self.state, parser().parse_args([
            'session-handoff', 'work', '--from', 'S01', '--to', 'S02',
            '--reason', 'main to main handoff',
        ]))
        self.assertEqual(result['controller_session'], 'S02')
        current = load_binding(self.project.root / new.handle_path)
        stale = load_binding(self.project.root / old.handle_path)
        self.assertEqual(current.controller_generation, before.controller_generation + 1)
        self.assertFalse(current.cancelled)
        self.assertTrue(stale.cancelled)
        with self.assertRaisesRegex(WorkflowError, 'SESSION_BINDING_STALE'):
            require_capability(stale, 'write', self.work_uid)

    def test_known_host_label_remains_unverified(self):
        binding = self.binding(host='codex', thread='thread-codex-unverified')
        self.assertEqual(binding.host_state, 'unverified')
        self.assertFalse(binding.cancelled)
        loaded = load_binding(self.project.root / binding.handle_path)
        self.assertEqual(loaded.host_state, 'unverified')
        self.assertFalse(loaded.cancelled)
        require_capability(loaded, 'write', self.work_uid)

    def test_native_model_layers_are_distinct(self):
        worker = self.binding(
            kind='native_executor',
            thread='thread-native-executor',
            capabilities={'claim', 'report', 'write'},
        )
        observation = NativeModelObservation(
            requested_model='group/deepseek-2',
            requested_status='actual',
            host_selected_route='group/deepseek-2',
            host_selected_status='actual',
            provider_backend='unknown',
            provider_backend_status='unknown',
            effort='max',
            native_turn_id='turn-native-executor',
            observed_at='2026-11-27T00:00:00+08:00',
        )
        require_native_model_role(
            worker, observation, 'executor', 'group/deepseek-2', 'max',
        )

        missing_route_evidence = NativeModelObservation(
            **{
                **observation.to_dict(),
                'host_selected_status': 'unknown',
            }
        )
        with self.assertRaisesRegex(
            WorkflowError, 'NATIVE_MODEL_ROUTE_UNVERIFIED',
        ):
            require_native_model_role(
                worker, missing_route_evidence, 'executor',
                'group/deepseek-2', 'max',
            )

        forged_backend = NativeModelObservation(
            **{
                **observation.to_dict(),
                'provider_backend': 'deepseek-direct',
                'provider_backend_status': 'actual',
            }
        )
        with self.assertRaisesRegex(
            WorkflowError, 'NATIVE_PROVIDER_BACKEND_UNSUPPORTED',
        ):
            require_native_model_role(
                worker, forged_backend, 'executor',
                'group/deepseek-2', 'max',
            )

    def test_native_model_observation_is_request_keyed_and_immutable(self):
        controller = self.binding(thread='thread-model-observation')
        self.register(controller)
        request = RequestRef(
            request_uid='native-model-observation-fixture',
            logical_key='native-model-observation',
            input_digest=digest_value({'work_uid': self.work_uid}),
            project_uid=self.store.project_uid(),
            work_uid=self.work_uid,
            attempt_uid=None,
            operation_kind='native-model-observation',
        )
        observation = NativeModelObservation(
            requested_model='group/deepseek-2',
            requested_status='actual',
            host_selected_route='group/deepseek-2',
            host_selected_status='actual',
            provider_backend='unknown',
            provider_backend_status='unknown',
            effort='max',
            native_turn_id='turn-model-observation',
            observed_at='2026-11-27T00:00:00+08:00',
        )
        first = record_native_model_observation(
            self.store, controller, request, observation,
            controller.controller_generation,
        )
        again = record_native_model_observation(
            self.store, controller, request, observation,
            controller.controller_generation,
        )
        self.assertEqual(first, again)
        self.assertEqual(
            self.store.read_native_model_observation(request.request_uid),
            observation.to_dict(),
        )

        changed = NativeModelObservation(
            **{
                **observation.to_dict(),
                'native_turn_id': 'turn-model-observation-replayed',
            }
        )
        with self.assertRaisesRegex(
            WorkflowError, 'NATIVE_MODEL_OBSERVATION_CHANGED',
        ):
            record_native_model_observation(
                self.store, controller, request, changed,
                controller.controller_generation,
            )

        db = sqlite3.connect(self.store.path)
        try:
            db.execute(
                'UPDATE metadata SET value=? WHERE key=?',
                ('{}', 'native_model_observation:' + request.request_uid),
            )
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(
            WorkflowError, 'NATIVE_MODEL_OBSERVATION_INVALID',
        ):
            record_native_model_observation(
                self.store, controller, request, changed,
                controller.controller_generation,
            )

    def test_controller_issue_surfaces_are_idempotent_and_restricted(self):
        self.cmd(
            'task-add', 'work', 'T01', '--title', 'Controller fixture',
            '--acceptance', 'exercise controller issuance',
        )
        self.state = read(self.project.root / '.gsp/runs/work/state.json')
        logical_key = 'initial-controller-fixture'
        initial_request = RequestRef(
            request_uid=digest_value({
                'project_uid': self.store.project_uid(),
                'logical_key': logical_key,
            }),
            logical_key=logical_key,
            input_digest=digest_value({'work_uid': self.work_uid}),
            project_uid=self.store.project_uid(),
            work_uid=self.work_uid,
            attempt_uid=None,
            operation_kind='initial-controller',
        )
        capabilities = frozenset({
            'accept', 'brief', 'claim', 'dispatch', 'handoff', 'plan',
            'report', 'write',
        })
        first = establish_initial_controller(
            self.store, initial_request, self.work_uid, 'codex',
            'thread-initial-controller', capabilities, 1,
        )
        again = establish_initial_controller(
            self.store, initial_request, self.work_uid, 'codex',
            'thread-initial-controller', capabilities, 1,
        )
        self.assertEqual(first.binding_uid, again.binding_uid)
        self.assertEqual(first.session.uid, again.session.uid)
        self.register(first)
        require_decision_controller(self.store, first, self.work_uid)

        delegate = issue_delegate_binding(
            self.store, first, 'native_executor', 'codex',
            'thread-delegate', frozenset({'claim', 'report', 'write'}), 1,
        )
        self.assertEqual(delegate.work_uid, self.work_uid)
        with self.assertRaisesRegex(
            WorkflowError, 'SESSION_CAPABILITY_ESCALATION',
        ):
            issue_delegate_binding(
                self.store, first, 'native_executor', 'codex',
                'thread-escalated', frozenset({'accept'}), 1,
            )

        spare = self.binding(
            kind='main_chat', thread='thread-spare-main',
            capabilities={'accept', 'handoff', 'plan', 'write'},
        )
        self.register(spare, role='observer', mode='read-only')
        with self.assertRaisesRegex(
            WorkflowError, 'SESSION_CONTROLLER_REQUIRED',
        ):
            require_decision_controller(self.store, spare, self.work_uid)

        recovered = resume_pending_controller(
            self.store, initial_request.request_uid,
            {'host': 'codex', 'host_thread_id': 'thread-initial-controller'},
        )
        self.assertEqual(recovered.binding_uid, first.binding_uid)
        with self.assertRaisesRegex(
            WorkflowError, 'CONTROLLER_PENDING_HOST_MISMATCH',
        ):
            resume_pending_controller(
                self.store, initial_request.request_uid,
                {'host': 'codex', 'host_thread_id': 'wrong-thread'},
            )

    def test_handle_tamper_rejected(self):
        binding = self.binding(thread='thread-tamper')
        handle = self.project.root / binding.handle_path
        payload = read(handle)
        payload['binding']['capabilities'] = ['claim', 'report', 'write']
        atomic(handle, payload)
        with self.assertRaisesRegex(WorkflowError, 'SESSION_CONTEXT_TAMPERED'):
            load_binding(handle)

        state_binding = self.binding(thread='thread-state-tamper')
        state_handle = self.project.root / state_binding.handle_path
        state_payload = read(state_handle)
        state_payload['binding']['controller_generation'] += 1
        atomic(state_handle, state_payload)
        with self.assertRaisesRegex(WorkflowError, 'SESSION_CONTEXT_TAMPERED'):
            load_binding(state_handle)

    def test_rejected_takeover_does_not_mutate_old_controller(self):
        first = self.binding(thread='thread-before-rejected')
        self.register(first)
        before = load_binding(self.project.root / first.handle_path)
        child = self.binding(kind='child', thread='thread-rejected-takeover')
        with self.assertRaisesRegex(
            WorkflowError, 'SESSION_CONTROLLER_CONTEXT_REQUIRED',
        ):
            self.register(child, role='controller', takeover=True)
        after = load_binding(self.project.root / first.handle_path)
        self.assertEqual(after, before)
        registry = read(
            self.project.root / '.gsp/runs/work/sessions/index.json'
        )
        row = next(item for item in registry['sessions'] if item['id'] == 'S01')
        self.assertEqual(row['status'], 'active')
        self.assertFalse(row.get('invalidated_by_takeover', False))

    def test_wrong_common_dir_rejected(self):
        foreign_root = Path(self.tmp.name) / 'foreign-project'
        foreign_root.mkdir()
        subprocess.run(
            ['git', 'init', '-q', '-b', 'main'], cwd=foreign_root,
            check=True, capture_output=True,
        )
        (foreign_root / '.gsp').mkdir()
        atomic(foreign_root / '.gsp/project.json', {
            'schema': 1, 'id': self.store.project_uid(),
            'root': str(foreign_root),
        })
        foreign_store = IdentityStore(foreign_root)
        foreign_store.initialize()
        with self.assertRaisesRegex(WorkflowError, 'WORK_UID_MISMATCH'):
            create_binding(
                foreign_store, self.work_uid, 'main', 'codex',
                'foreign-thread', frozenset({'write'}),
            )

    def test_first_work_creation_without_task_context(self):
        result = Governance(self.project.root).create(
            'First work', 'goal', 'test', run_id='first-work',
        )
        self.assertEqual(result['kind'], 'work')
        self.assertEqual(result['stage'], 'idea')
        self.assertFalse((self.project.root / '.gsp/runs/first-work/state.json').exists())

    def test_project_request_cannot_mutate_existing_work(self):
        before = Governance(self.project.root).get(self.work_uid)
        requested = self.binding()
        requested = requested.__class__(
            **{
                **requested.to_dict(),
                'work_uid': 'requested-other-work',
                'session': requested.session,
                'capabilities': frozenset(requested.capabilities),
            }
        )
        with self.assertRaisesRegex(WorkflowError, 'SESSION_PROJECT_MISMATCH|WORK_UID_MISMATCH'):
            require_capability(requested, 'write', self.work_uid)
        after = Governance(self.project.root).get(self.work_uid)
        self.assertEqual(after, before)

    def test_legacy_bare_ids_are_read_only(self):
        self.cmd('task-add', 'work', 'T01', '--title', 'Task', '--acceptance', 'test')
        bare = self.cmd('session-register', 'work', '--role', 'controller',
                        '--host', 'codex', '--thread', 'legacy-thread',
                        '--write-mode', 'write', '--path', 'services')
        self.assertEqual(bare['session']['context_mode'], 'legacy-read-only')
        registry = read(
            self.project.root / '.gsp/runs/work/sessions/index.json'
        )
        self.assertEqual(registry['workspace_leases'], {})
        with self.assertRaisesRegex(WorkflowError, 'SESSION_CONTEXT_REQUIRED'):
            authorize_edit(
                self.project, self.state, 'legacy-thread',
                [self.project.root / 'README.md'],
            )

    def test_hook_requires_bound_context(self):
        self.cmd('task-add', 'work', 'T01', '--title', 'Task', '--acceptance', 'test')
        self.cmd('session-register', 'work', '--role', 'controller',
                 '--host', 'codex', '--thread', 'legacy-thread',
                 '--write-mode', 'write', '--path', 'services')
        payload = {
            'hook_event_name': 'PreToolUse', 'tool_name': 'Write',
            'cwd': str(self.root), 'session_id': 'legacy-thread',
            'gsp_session_id': 'legacy-thread',
            'tool_input': {'file_path': 'services/unit.py'},
        }
        denied = handle(payload)
        self.assertEqual(
            denied['hookSpecificOutput']['permissionDecision'], 'deny',
        )
        self.assertIn(
            'SESSION_CONTEXT_REQUIRED',
            denied['hookSpecificOutput']['permissionDecisionReason'],
        )


if __name__ == '__main__':
    unittest.main()
