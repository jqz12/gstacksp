import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from gsp.codex_hooks import handle
from gsp.runtime import Project, parser, dispatch, read
try:
    from process_helpers import session_binding
except ModuleNotFoundError:
    from tests.process_helpers import session_binding


class SessionHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-session-hook-')
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=self.root, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Session Hook Test'], cwd=self.root, check=True)
        subprocess.run(['git', 'config', 'user.email', 'hook@example.invalid'], cwd=self.root, check=True)
        (self.root / 'README.md').write_text('# hook\n', encoding='utf-8')
        subprocess.run(['git', 'add', 'README.md'], cwd=self.root, check=True)
        subprocess.run(['git', 'commit', '-qm', 'chore: baseline'], cwd=self.root, check=True)
        self.project = Project(self.root)
        self.project.initialize()
        self.cmd('new', 'work', '--title', '跨会话 hook', '--intent', 'cross-session')
        self.cmd('topology-select', 'work', '--mode', 'in-place',
                 '--quote', 'in-place for hook test')
        self.state = read(self.root / '.gsp/runs/work/state.json')
        self.binding = session_binding(self.root, self.state['work_uuid'], 'main', 'codex', 'thread-a')
        self.cmd('session-register', 'work', '--role', 'controller', '--host', 'codex',
                 '--thread', 'thread-a', '--write-mode', 'write', '--path', 'services',
                 '--context', str(self.root / self.binding.handle_path))

    def tearDown(self):
        self.tmp.cleanup()

    def cmd(self, *argv):
        return dispatch(self.project, parser().parse_args(list(argv)))[0]

    def payload(self, session, path):
        return {'hook_event_name': 'PreToolUse', 'tool_name': 'Write', 'cwd': str(self.root),
                'session_id': session, 'gsp_session_context': str(self.root / self.binding.handle_path),
                'tool_input': {'file_path': path}}

    def control_documents(self):
        doc = self.project.paths(self.state['id'])[1]
        paths = [self.project.document(self.state, kind) for kind in ('design', 'plan')]
        paths += [doc / name for name in ('review.md', 'decisions.md', 'delivery.md')]
        for path in paths:
            if not path.exists():
                path.write_text('# Control document fixture\n', encoding='utf-8')
        return paths

    def worker_without_main_source_lease(self, *, plan_flag=False):
        extra = tempfile.TemporaryDirectory(prefix='gsp-protected-doc-hook-')
        self.addCleanup(extra.cleanup)
        self.root = Path(extra.name) / 'project'
        self.root.mkdir()
        for argv in [('init', '-q', '-b', 'main'),
                     ('config', 'user.name', 'Protected Document Test'),
                     ('config', 'user.email', 'protected-doc@example.invalid')]:
            subprocess.run(['git', *argv], cwd=self.root, check=True)
        (self.root / 'README.md').write_text('# protected control\n', encoding='utf-8')
        subprocess.run(['git', 'add', 'README.md'], cwd=self.root, check=True)
        subprocess.run(['git', 'commit', '-qm', 'fixture'], cwd=self.root, check=True)
        self.project = Project(self.root)
        self.project.initialize()
        self.cmd('new', 'work', '--title', 'Protected documents', '--intent', 'cross-session')
        self.cmd('topology-select', 'work', '--mode', 'in-place', '--quote', 'isolated test fixture')
        self.cmd('task-add', 'work', 'T01', '--title', 'Worker implementation', '--acceptance', 'Behavior passes')
        self.state = read(self.root / '.gsp/runs/work/state.json')
        main = session_binding(self.root, self.state['work_uuid'], 'main', 'codex', 'planning-thread')
        caps = {'claim', 'report', 'write'} | ({'plan'} if plan_flag else set())
        self.binding = session_binding(self.root, self.state['work_uuid'], 'child',
                                       'codex', 'thread-a', capabilities=caps)
        self.cmd('session-register', 'work', '--session', 'S01', '--role', 'worker',
                 '--task', 'T01', '--host', 'codex', '--thread', 'thread-a',
                 '--write-mode', 'write', '--path', 'services',
                 '--context', str(self.root / self.binding.handle_path))
        from gsp.identity_store import IdentityStore
        store = IdentityStore(self.root)
        self.assertEqual(store.read_work_controller(self.state['work_uuid'])['binding_uid'], main.binding_uid)
        registry = read(self.root / '.gsp/runs/work/sessions/index.json')
        self.assertIsNone(registry['controller_session'])
        self.assertEqual(len(registry['sessions']), 1)
        self.assertEqual(list(registry['workspace_leases'].values()), ['S01'])
        return main

    def test_worker_cannot_edit_control_documents_even_with_plan_flag(self):
        for plan_flag in (False, True):
            with self.subTest(plan_flag=plan_flag):
                self.worker_without_main_source_lease(plan_flag=plan_flag)
                allowed = self.payload('planning-thread', 'new/unplanned_source.py')
                allowed['agent_id'] = 'thread-a'
                self.assertNotEqual(handle(allowed).get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
                paths = self.control_documents()
                state_file = self.root / '.gsp/runs/work/state.json'
                registry_file = self.root / '.gsp/runs/work/sessions/index.json'
                before = {p: p.read_bytes() for p in [*paths, state_file, registry_file]}
                for path in paths:
                    with self.subTest(path=path.name):
                        payload = self.payload('planning-thread', path.relative_to(self.root).as_posix())
                        payload['agent_id'] = 'thread-a'
                        result = handle(payload)
                        self.assertEqual(result.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
                        self.assertRegex(str(result), 'CAPABILITY_REQUIRED|SESSION_CONTROLLER_REQUIRED')
                self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_control_document_owner_tracks_real_controller_handoff(self):
        from process_helpers import temporary_env
        paths = self.control_documents()
        for path in paths:
            self.assertNotEqual(handle(self.payload('thread-a', str(path))).get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
        successor = session_binding(self.root, self.state['work_uuid'], 'main', 'codex', 'thread-b')
        self.cmd('session-register', 'work', '--session', 'S02', '--role', 'observer',
                 '--host', 'codex', '--thread', 'thread-b', '--write-mode', 'read-only',
                 '--context', str(self.root / successor.handle_path))
        args = parser().parse_args(['session-handoff', 'work', '--from', 'S01',
                                   '--to', 'S02', '--reason', 'transfer control documents'])
        with temporary_env({'GSP_SESSION_CONTEXT': str(self.root / self.binding.handle_path)}):
            _, success = dispatch(self.project, args, enforce_context=True)
        self.assertTrue(success)
        old_binding = self.binding
        for path in paths:
            self.binding = old_binding
            denied = handle(self.payload('thread-a', str(path)))
            self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')
            self.binding = successor
            self.assertNotEqual(handle(self.payload('thread-b', str(path))).get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')

    def test_registered_session_can_edit_claimed_path(self):
        self.assertEqual(handle(self.payload('thread-a', 'services/unit.service')), {})

    def test_registered_session_can_edit_new_unclaimed_workspace_path(self):
        self.assertEqual(handle(self.payload('thread-a', 'tests/new_code.py')), {})

    def test_apply_patch_checks_each_declared_target(self):
        payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'apply_patch', 'cwd': str(self.root),
                   'session_id': 'thread-a',
                   'gsp_session_context': str(self.root / self.binding.handle_path),
                   'tool_input': {'command':
                       '*** Begin Patch\n*** Add File: services/another.service\n+placeholder\n*** End Patch'}}
        self.assertEqual(handle(payload), {})

    def test_unknown_session_is_denied_before_edit(self):
        denied = handle(self.payload('thread-b', 'services/unit.service'))
        self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('SESSION_', denied['hookSpecificOutput']['permissionDecisionReason'])

    def test_active_overlapping_claim_is_denied(self):
        registry_path = self.root / '.gsp/runs/work/sessions/index.json'
        registry = read(registry_path)
        registry['sessions'].append({
            'id': 'S02',
            'role': 'worker',
            'host': 'codex',
            'thread_id': 'thread-b',
            'task': None,
            'worktree': str(self.root.resolve()),
            'write_mode': 'write',
            'claimed_paths': ['tests'],
            'status': 'active',
        })
        registry_path.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )
        denied = handle(self.payload('thread-a', 'tests/unit.py'))
        self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn(
            'SESSION_PATH_CONFLICT',
            denied['hookSpecificOutput']['permissionDecisionReason'],
        )

    def test_invalid_explicit_context_is_denied_without_token_fallback(self):
        payload = self.payload('thread-a', 'services/unit.service')
        invalid = self.root / '.gsp' / 'invalid-context.json'
        invalid.write_text('{"schema": 1, "binding": {}}\n', encoding='utf-8')
        payload['gsp_session_context'] = str(invalid)
        denied = handle(payload)
        self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn(
            'SESSION_ACTOR_MISMATCH',
            denied['hookSpecificOutput']['permissionDecisionReason'],
        )


if __name__ == '__main__':
    unittest.main()
