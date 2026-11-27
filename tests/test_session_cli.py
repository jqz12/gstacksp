import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from gsp.identity_store import IdentityStore
from gsp.project import Project

try:
    from process_helpers import run, session_binding
except ModuleNotFoundError:
    from tests.process_helpers import run, session_binding


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts' / 'gsp.py'


class SessionCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-session-cli-')
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Session CLI Test')
        self.git('config', 'user.email', 'session-cli@example.invalid')
        (self.project / 'README.md').write_text('# cli\n', encoding='utf-8')
        self.git('add', 'README.md')
        self.git('commit', '-qm', 'chore: baseline')
        self.cli('init')
        self.cli('new', 'work', '--title', '跨会话 CLI', '--intent', 'cross-session')
        state = self.state()
        # Preserve the legacy session CLI protocol in this compatibility fixture.
        # New sealed-contract entrypoints have separate positive/negative tests.
        state.pop('operation_schema', None)
        Project(self.project).write(state, 'fixture', 'legacy session CLI state')
        main = session_binding(
            self.project, state['work_uuid'], 'main', 'codex', 'main-thread',
        )
        self.context = self.project / main.handle_path
        (self.project / state['documents']['design']).write_text('# design\n\nvalid\n', encoding='utf-8')
        self.cli('approve', 'work', '--quote', '批准')
        self.cli('task-add', 'work', 'T01', '--title', '服务', '--acceptance', '测试')

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        result = run(['git', *args], cwd=self.project, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def cli(self, *args, ok=True):
        result = run([sys.executable, str(CLI), '--project', str(self.project), *args],
                     capture_output=True, text=True, encoding='utf-8',
                     env=self.cli_env())
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def cli_env(self, context='default'):
        env = os.environ.copy()
        env.pop('GSP_SESSION_CONTEXT', None)
        if context == 'default':
            context = getattr(self, 'context', None)
        if context is not None:
            env['GSP_SESSION_CONTEXT'] = str(context)
        return env

    def cli_without_context(self, *args):
        result = run(
            [sys.executable, str(CLI), '--project', str(self.project), *args],
            capture_output=True, text=True, encoding='utf-8',
            env=self.cli_env(context=None),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def cli_with_context(self, context, *args):
        result = run(
            [sys.executable, str(CLI), '--project', str(self.project), *args],
            capture_output=True, text=True, encoding='utf-8',
            env=self.cli_env(context=context),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def cli_as(self, context, *args):
        result = run(
            [sys.executable, str(CLI), '--project', str(self.project), *args],
            capture_output=True, text=True, encoding='utf-8',
            env=self.cli_env(context=context),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def state(self):
        return json.loads((self.project / '.gsp/runs/work/state.json').read_text(encoding='utf-8'))

    def test_cli_writes_brief_report_and_history(self):
        st = self.state()
        child_b = session_binding(self.project, st['work_uuid'], 'child', 'claude', 'worker-thread')
        self.cli('session-register', 'work', '--role', 'controller', '--host', 'codex',
                 '--thread', 'main-thread', '--write-mode', 'write', '--path', 'services',
                 '--context', str(self.context))
        self.cli('session-register', 'work', '--role', 'worker', '--host', 'claude',
                 '--thread', 'worker-thread', '--task', 'T01', '--worktree', str(self.project / 'child'),
                 '--write-mode', 'write', '--path', 'services/quant-control.service',
                 '--context', str(self.project / child_b.handle_path))
        self.cli('session-brief', 'work', 'T01', '--session', 'S01', '--owner', 'S02',
                 '--title', '服务实现', '--objective', '实现服务契约', '--path', 'services/quant-control.service',
                 '--test', 'python -m unittest tests.test_service -v')
        child_context = self.project / child_b.handle_path
        self.cli_as(
            child_context, 'session-report', 'work', 'T01', '--session', 'S02',
            '--status', 'DONE', '--summary', '实现并通过测试',
            '--files', 'services/quant-control.service', '--tests', '1 passed',
            '--evidence', '.gsp/runs/work/evidence/x.json',
        )
        self.cli_as(
            child_context, 'session-report', 'work', 'T01', '--session', 'S02',
            '--status', 'DONE', '--summary', '补充接口注释',
            '--files', 'services/quant-control.service',
        )
        sessions = self.project / self.state()['doc_dir'] / 'sessions'
        self.assertTrue((sessions / 'T01-brief.md').is_file())
        self.assertTrue((sessions / 'T01-report.md').is_file())
        self.assertTrue(list((sessions / 'history').glob('T01-report-*.md')))
        status = json.loads(self.cli('session-status', 'work').stdout)
        self.assertEqual(status['tasks']['T01']['report_status'], 'DONE')


    def test_cli_auto_dispatch_accepts_and_cleans_managed_worktree(self):
        st = self.state()
        self.cli('session-register', 'work', '--role', 'controller', '--host', 'codex',
                 '--thread', 'main-thread', '--write-mode', 'write', '--path', '.',
                 '--context', str(self.context))
        dispatched = json.loads(self.cli(
            'session-dispatch', 'work', '--session', 'S01', '--task', 'T01',
            '--target', 'T01=codex', '--path', 'T01=src/service').stdout)
        row = dispatched['dispatches'][0]
        self.assertTrue(row['managed_worktree'])
        self.assertTrue(Path(row['worktree']).is_dir())

        child_b = session_binding(self.project, st['work_uuid'], 'child', 'codex', 'worker-thread')
        self.cli('session-register', 'work', '--session', row['session_hint'], '--role', 'worker',
                 '--host', 'codex', '--thread', 'worker-thread', '--task', 'T01',
                 '--branch', row['branch'], '--worktree', row['worktree'],
                 '--write-mode', 'write', '--path', 'src/service',
                 '--context', str(self.project / child_b.handle_path))
        child_context = self.project / child_b.handle_path
        self.cli_as(
            child_context, 'session-report', 'work', 'T01',
            '--session', row['session_hint'], '--status', 'DONE',
            '--summary', 'worker complete', '--files', 'src/service.py',
            '--tests', 'focused tests pass',
        )
        state = self.state()
        state['tasks'][0]['status'] = 'done'
        # Synthetic completion fixture still uses the real state journal;
        # changing JSON behind a committed operation is not a valid state transition.
        from gsp.runtime import Project
        Project(self.project).write(state, 'fixture-task-complete',
                                   action_uid='fixture:task-complete')

        accepted = json.loads(self.cli(
            'session-accept', 'work', 'T01', '--session', 'S01', '--reason',
            'controller verified report and tests').stdout)
        self.assertEqual(accepted['acceptance'], 'accepted')
        self.assertEqual(accepted['cleanup']['result'], 'removed')
        self.assertFalse(Path(row['worktree']).exists())
        progress = json.loads(self.cli('session-progress', 'work').stdout)
        self.assertEqual(progress['accepted_tasks'], 1)
        self.assertEqual(progress['cleaned_worktrees'], 1)
        self.assertEqual(progress['managed_worktrees'], 0)
        self.assertEqual(progress['cleanup_pending_worktrees'], 0)

    def test_cli_fresh_project_bootstrap(self):
        fresh = Path(self.tmp.name) / 'fresh-project'
        fresh.mkdir()

        def fresh_git(*args):
            result = run(
                ['git', *args], cwd=fresh, capture_output=True, text=True,
                encoding='utf-8',
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        fresh_git('init', '-q', '-b', 'main')
        fresh_git('config', 'user.name', 'Fresh Bootstrap Test')
        fresh_git('config', 'user.email', 'fresh-bootstrap@example.invalid')
        (fresh / 'README.md').write_text('# fresh\n', encoding='utf-8')
        fresh_git('add', 'README.md')
        fresh_git('commit', '-qm', 'chore: baseline')

        env = self.cli_env(context=None)

        def fresh_cli(*args):
            result = run(
                [sys.executable, str(CLI), '--project', str(fresh), *args],
                capture_output=True, text=True, encoding='utf-8', env=env,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result

        fresh_cli('init')
        fresh_cli(
            'new', 'bootstrap', '--title', 'Fresh bootstrap',
            '--intent', 'cross-session',
        )
        state_path = fresh / '.gsp/runs/bootstrap/state.json'
        work_uid = json.loads(state_path.read_text(encoding='utf-8'))['work_uuid']
        active = fresh / '.gsp/active.json'
        if active.exists():
            active.unlink()

        context_out = fresh / '.gsp/contexts/fresh-main.json'
        issued = json.loads(fresh_cli(
            'session-bind', work_uid, '--kind', 'main_chat', '--host', 'codex',
            '--thread', 'fresh-main', '--context-out', str(context_out),
        ).stdout)
        self.assertEqual(Path(issued['context_out']).resolve(), context_out.resolve())
        self.assertTrue(context_out.is_file())
        self.assertFalse(active.exists(), 'session-bind must not fall back to active.json')

        fresh_cli(
            'session-register', 'bootstrap', '--role', 'controller',
            '--host', 'codex', '--thread', 'fresh-main', '--write-mode', 'write',
            '--path', '.', '--context', str(context_out),
        )
        task = run(
            [
                sys.executable, str(CLI), '--project', str(fresh), 'task-add',
                'bootstrap', 'T01', '--title', 'Bootstrap task',
                '--acceptance', 'real CLI write succeeds',
            ],
            capture_output=True, text=True, encoding='utf-8',
            env=self.cli_env(context=context_out),
        )
        self.assertEqual(task.returncode, 0, task.stdout + task.stderr)
        state = json.loads(state_path.read_text(encoding='utf-8'))
        self.assertEqual([item['id'] for item in state['tasks']], ['T01'])

    def test_cli_pending_controller_intent_recovery(self):
        from gsp.orchestration_types import digest_value
        from gsp.runtime import atomic

        from gsp.session_context import load_binding
        candidate = load_binding(self.context)
        record = {
            'id': 'S01', 'role': 'controller', 'host': 'codex',
            'thread_id': candidate.host_thread_id, 'task': None, 'branch': None,
            'worktree': str(self.project), 'write_mode': 'write',
            'claimed_paths': ['.'], 'status': 'active',
            'context': candidate.handle_path, 'context_mode': 'bound',
            'binding_uid': candidate.binding_uid, 'work_uid': candidate.work_uid,
            'controller_generation': candidate.controller_generation,
            'cancel_generation': candidate.cancel_generation,
            'context_binding': candidate.to_dict(),
        }
        payload = {
            'task': 'work', 'work_uid': candidate.work_uid,
            'operation': 'establish-controller', 'old_session': None,
            'new_session': 'S01', 'old_binding': None,
            'new_binding': candidate.to_dict(),
            'next_generation': candidate.controller_generation,
            'reason': 'durable first-controller intent before operation allocation',
            'new_record': record,
        }
        intent_path = (
            self.project / '.gsp/orchestration/controller-transition-intents/'
            'pending-establish.json'
        )
        atomic(intent_path, {
            'schema': 1,
            'payload': payload,
            'payload_digest': digest_value(payload),
        })
        context = self.project / candidate.handle_path

        self.cli_as(
            context, 'session-watch', 'work', '--adapter', 'manual',
        )
        recovered = json.loads(intent_path.read_text(encoding='utf-8'))
        self.assertTrue(recovered.get('operation_uid'))
        registry = json.loads(
            (self.project / '.gsp/runs/work/sessions/index.json').read_text(
                encoding='utf-8',
            )
        )
        self.assertEqual(registry['controller_session'], 'S01')
        self.assertEqual(
            [row['id'] for row in registry['sessions']], ['S01'],
        )

        self.cli_as(
            context, 'session-watch', 'work', '--adapter', 'manual',
        )
        registry = json.loads(
            (self.project / '.gsp/runs/work/sessions/index.json').read_text(
                encoding='utf-8',
            )
        )
        self.assertEqual(
            [row['id'] for row in registry['sessions']], ['S01'],
        )

    def test_spare_main_decision_zero_mutation(self):
        st = self.state()
        # setUp already established the authoritative main binding.
        from gsp.session_context import load_binding
        controller = load_binding(self.context)
        controller_context = self.project / controller.handle_path
        self.cli_as(
            controller_context, 'session-register', 'work',
            '--role', 'controller', '--host', 'codex',
            '--thread', controller.host_thread_id, '--write-mode', 'write',
            '--path', '.', '--context', str(controller_context),
        )
        spare = session_binding(
            self.project, st['work_uuid'], 'main', 'codex', 'spare-thread',
        )
        spare_context = self.project / spare.handle_path
        self.cli_as(
            spare_context, 'session-register', 'work', '--role', 'observer',
            '--host', 'codex', '--thread', 'spare-thread',
            '--write-mode', 'read-only', '--context', str(spare_context),
        )

        state_path = self.project / '.gsp/runs/work/state.json'
        decisions = self.project / self.state()['doc_dir'] / 'decisions.md'
        before_state = state_path.read_bytes()
        before_decisions = decisions.read_bytes() if decisions.is_file() else None
        denied = self.cli_with_context(
            spare_context, 'decision', 'work', '--text',
            'spare main must not decide before handoff', '--source', 'agent',
        )
        self.assertIn('SESSION_CONTROLLER_REQUIRED', denied.stderr)
        self.assertEqual(state_path.read_bytes(), before_state)
        self.assertEqual(
            decisions.read_bytes() if decisions.is_file() else None,
            before_decisions,
        )

        self.cli_as(
            controller_context,
            'session-handoff', 'work', '--from', 'S01', '--to', 'S02',
            '--reason', 'authorized controller handoff',
        )
        accepted = self.cli_as(
            spare_context, 'decision', 'work', '--text',
            'the handed-off controller records the decision',
            '--source', 'agent',
        )
        self.assertIn(
            'the handed-off controller records the decision',
            decisions.read_text(encoding='utf-8'),
        )

    def test_cli_rejects_missing_context_without_mutating_plan(self):
        before = (self.project / '.gsp/runs/work/state.json').read_bytes()
        result = self.cli_without_context(
            'task-add', 'work', 'T02', '--title', '无上下文',
            '--acceptance', 'must not be registered',
        )
        self.assertIn('SESSION_CONTEXT_REQUIRED', result.stderr)
        self.assertEqual(
            (self.project / '.gsp/runs/work/state.json').read_bytes(), before,
        )

    def test_spare_main_binding_cannot_write_plan_before_handoff(self):
        st = self.state()
        # setUp already established the authoritative main binding.
        from gsp.session_context import load_binding
        controller = load_binding(self.context)
        controller_context = self.project / controller.handle_path
        self.cli_as(
            controller_context, 'session-register', 'work', '--role', 'controller',
            '--host', 'codex', '--thread', controller.host_thread_id, '--write-mode', 'write',
            '--path', '.', '--context', str(controller_context),
        )
        spare = session_binding(
            self.project, st['work_uuid'], 'main', 'codex', 'spare-thread',
        )
        spare_context = self.project / spare.handle_path
        self.cli_as(
            spare_context, 'session-register', 'work', '--role', 'observer', '--host', 'codex',
            '--thread', 'spare-thread', '--task', 'T01', '--worktree', str(self.project),
            '--write-mode', 'read-only', '--context', str(spare_context),
        )
        state_path = self.project / '.gsp/runs/work/state.json'
        before = state_path.read_bytes()
        result = self.cli_with_context(
            spare_context, 'task-add', 'work', 'T02', '--title', 'spare 越权计划',
            '--acceptance', 'must not be registered',
        )
        self.assertIn('SESSION_CONTROLLER_REQUIRED', result.stderr)
        self.assertEqual(state_path.read_bytes(), before)

    def test_todo_origin_missing_context_cannot_mutate_stale_ready(self):
        state_path = self.project / '.gsp/runs/work/state.json'
        state = self.state()
        state['status'] = 'ready'
        state['delivery_fingerprint'] = 'stale-before-todo-context-check'
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8',
        )
        backlog = self.project / 'TODOS.md'
        backlog_before = backlog.read_bytes() if backlog.is_file() else None
        before = state_path.read_bytes()
        result = self.cli_without_context(
            'todo-add', '--title', '拒绝前不得刷新', '--what', 'context rejection must be byte-zero',
            '--why', 'security regression', '--context', 'isolated fixture',
            '--source', 'review/t04', '--origin', 'work',
        )
        self.assertIn('SESSION_CONTEXT_REQUIRED', result.stderr)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(self.state()['status'], 'ready')
        self.assertEqual(backlog.read_bytes() if backlog.is_file() else None, backlog_before)

    def test_cli_rejects_cross_work_context_without_mutation(self):
        self.cli('new', 'other', '--title', '另一工作项', '--intent', 'cross-session')
        state_file = self.project / '.gsp/runs/other/state.json'
        before = state_file.read_bytes()
        result = self.cli_with_context(
            self.context, 'task-add', 'other', 'T01', '--title', '越权任务',
            '--acceptance', 'must not be registered',
        )
        self.assertIn('WORK_UID_MISMATCH', result.stderr)
        self.assertEqual(state_file.read_bytes(), before)

    def test_cli_rejects_helper_plan_mutation(self):
        st = self.state()
        helper = session_binding(
            self.project, st['work_uuid'], 'child', 'codex', 'helper-thread',
            capabilities={'brief', 'claim', 'dispatch', 'handoff', 'plan', 'report', 'write'},
        )
        helper_context = self.project / helper.handle_path
        self.cli_as(
            helper_context,
            'session-register', 'work', '--role', 'worker', '--host', 'codex',
            '--thread', 'helper-thread', '--task', 'T01', '--write-mode', 'write',
            '--path', 'services', '--context', str(helper_context),
        )
        before = (self.project / '.gsp/runs/work/state.json').read_bytes()
        result = self.cli_with_context(
            helper_context,
            'task-add', 'work', 'T02', '--title', 'helper 越权计划',
            '--acceptance', 'must not be registered',
        )
        self.assertIn('SESSION_CONTROLLER_REQUIRED', result.stderr)
        self.assertEqual(
            (self.project / '.gsp/runs/work/state.json').read_bytes(), before,
        )

    def test_missing_context_cannot_mutate_stale_ready_before_rejection(self):
        state_path = self.project / '.gsp/runs/work/state.json'
        state = self.state()
        state['status'] = 'ready'
        state['delivery_fingerprint'] = 'stale-before-context-check'
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8',
        )
        before = state_path.read_bytes()
        result = self.cli_without_context(
            'task-add', 'work', 'T02', '--title', '拒绝前不得刷新',
            '--acceptance', 'context rejection must be byte-zero',
        )
        self.assertIn('SESSION_CONTEXT_REQUIRED', result.stderr)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(self.state()['status'], 'ready')

    def test_cli_rejects_stale_generation_after_takeover(self):
        st = self.state()
        self.cli('session-register', 'work', '--role', 'controller', '--host', 'codex',
                 '--thread', 'main-thread', '--write-mode', 'write', '--path', 'services',
                 '--context', str(self.context))
        replacement = session_binding(
            self.project, st['work_uuid'], 'main', 'codex', 'replacement-thread',
        )
        replacement_context = self.project / replacement.handle_path
        self.cli_as(
            replacement_context,
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'replacement-thread', '--write-mode', 'write',
            '--path', 'services', '--takeover', '--reason',
            'replacement is the current controller', '--context',
            str(replacement_context),
        )
        before = (self.project / '.gsp/runs/work/state.json').read_bytes()
        result = self.cli_with_context(
            self.context,
            'task-add', 'work', 'T02', '--title', '旧控制器越权',
            '--acceptance', 'must not be registered',
        )
        self.assertIn('SESSION_BINDING_STALE', result.stderr)
        self.assertEqual(
            (self.project / '.gsp/runs/work/state.json').read_bytes(), before,
        )


    def test_cli_rejects_foreign_session_actor_before_mutation(self):
        st = self.state()
        self.cli('session-register', 'work', '--role', 'controller', '--host', 'codex',
                 '--thread', 'main-thread', '--write-mode', 'write', '--path', '.',
                 '--context', str(self.context))
        first = session_binding(
            self.project, st['work_uuid'], 'child', 'codex', 'worker-one',
        )
        second = session_binding(
            self.project, st['work_uuid'], 'child', 'codex', 'worker-two',
        )
        first_context = self.project / first.handle_path
        second_context = self.project / second.handle_path
        self.cli_as(
            first_context, 'session-register', 'work', '--session', 'S02',
            '--role', 'worker', '--host', 'codex', '--thread', 'worker-one',
            '--task', 'T01', '--worktree', str(self.project / 'one'),
            '--write-mode', 'write', '--path', 'src/one',
            '--context', str(first_context),
        )
        self.cli_as(
            second_context, 'session-register', 'work', '--session', 'S03',
            '--role', 'worker', '--host', 'codex', '--thread', 'worker-two',
            '--task', 'T01', '--worktree', str(self.project / 'two'),
            '--write-mode', 'write', '--path', 'src/two',
            '--context', str(second_context),
        )
        state_path = self.project / '.gsp/runs/work/state.json'
        registry_path = self.project / '.gsp/runs/work/sessions/index.json'
        before_state = state_path.read_bytes()
        before_registry = registry_path.read_bytes()
        claim = self.cli_with_context(
            second_context, 'session-claim', 'work', '--session', 'S02',
            '--path', 'tests/foreign',
        )
        self.assertIn('SESSION_ACTOR_MISMATCH S02', claim.stderr)
        self.assertEqual(state_path.read_bytes(), before_state)
        self.assertEqual(registry_path.read_bytes(), before_registry)
        report = self.cli_with_context(
            second_context, 'session-report', 'work', 'T01',
            '--session', 'S02', '--status', 'DONE',
            '--summary', 'foreign report must not be accepted',
        )
        self.assertIn('SESSION_ACTOR_MISMATCH S02', report.stderr)
        self.assertEqual(state_path.read_bytes(), before_state)
        self.assertEqual(registry_path.read_bytes(), before_registry)

    def test_cli_controller_only_actions_reject_foreign_caller(self):
        st = self.state()
        # setUp already established the authoritative main binding.
        from gsp.session_context import load_binding
        controller = load_binding(self.context)
        controller_context = self.project / controller.handle_path
        self.cli_as(
            controller_context, 'session-register', 'work', '--role', 'controller',
            '--host', 'codex', '--thread', controller.host_thread_id,
            '--write-mode', 'write', '--path', '.',
            '--context', str(controller_context),
        )
        spare = session_binding(
            self.project, st['work_uuid'], 'main', 'codex', 'spare-thread',
        )
        spare_context = self.project / spare.handle_path
        self.cli_as(
            spare_context, 'session-register', 'work', '--role', 'observer',
            '--host', 'codex', '--thread', 'spare-thread',
            '--write-mode', 'read-only', '--context', str(spare_context),
        )
        state_path = self.project / '.gsp/runs/work/state.json'
        registry_path = self.project / '.gsp/runs/work/sessions/index.json'
        before_state = state_path.read_bytes()
        before_registry = registry_path.read_bytes()
        commands = [
            (
                'session-dispatch', 'work', '--session', 'S01',
                '--task', 'T01', '--target', 'T01=manual', '--path', 'T01=src',
            ),
            (
                'session-handoff', 'work', '--from', 'S01', '--to', 'S02',
                '--reason', 'foreign handoff must be rejected',
            ),
            (
                'session-accept', 'work', 'T01', '--session', 'S01',
                '--reason', 'foreign accept must be rejected',
            ),
            (
                'session-watch', 'work', '--adapter', 'manual',
            ),
            (
                'session-clean', 'work', '--task', 'T01',
                '--result', 'released', '--reason', 'foreign clean must be rejected',
            ),
        ]
        for command in commands:
            with self.subTest(command=command[0]):
                result = self.cli_with_context(spare_context, *command)
                # session-accept is fenced by the controller-acceptance rule
                # before actor matching; either way the observer is rejected
                # with byte-zero state and registry effects.
                expected = (
                    'SESSION_CONTROLLER_REQUIRED'
                    if command[0] == 'session-accept'
                    else 'SESSION_ACTOR_MISMATCH'
                )
                self.assertIn(expected, result.stderr)
                self.assertEqual(state_path.read_bytes(), before_state)
                self.assertEqual(registry_path.read_bytes(), before_registry)


class SessionReportDurabilityCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-session-report-cli-')
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()
        self.home = Path(self.tmp.name) / 'home'
        self.home.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Session Report CLI Test')
        self.git('config', 'user.email', 'session-report-cli@example.invalid')
        (self.project / 'README.md').write_text('# cli\n', encoding='utf-8')
        self.git('add', 'README.md')
        self.git('commit', '-qm', 'chore: baseline')
        self.cli('init')
        self.cli('new', 'work', '--title', '跨会话报告', '--intent', 'cross-session')
        state = self.state()
        main = session_binding(
            self.project, state['work_uuid'], 'main', 'codex', 'main-thread',
        )
        self.context = self.project / main.handle_path
        self.cli(
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'main-thread', '--write-mode', 'write', '--path', '.',
            '--context', str(self.context),
        )
        (self.project / state['documents']['design']).write_text(
            '# design\n\nvalid\n', encoding='utf-8',
        )
        self.cli('approve', 'work', '--quote', '批准')
        self.cli(
            'task-add', 'work', 'T01', '--title', '服务',
            '--acceptance', '测试',
        )

    def git(self, *args):
        result = run(
            ['git', *args], cwd=self.project, capture_output=True,
            text=True, encoding='utf-8',
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def cli_env(self, context=None):
        env = os.environ.copy()
        env.pop('GSP_SESSION_CONTEXT', None)
        env['HOME'] = str(self.home)
        env['USERPROFILE'] = str(self.home)
        env['CODEX_HOME'] = str(self.home / '.codex')
        if context is not None:
            env['GSP_SESSION_CONTEXT'] = str(context)
        return env

    def cli(self, *args):
        result = run(
            [sys.executable, str(CLI), '--project', str(self.project), *args],
            capture_output=True, text=True, encoding='utf-8',
            env=self.cli_env(getattr(self, 'context', None)),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def cli_as(self, context, *args):
        result = run(
            [sys.executable, str(CLI), '--project', str(self.project), *args],
            capture_output=True, text=True, encoding='utf-8',
            env=self.cli_env(context),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def state(self):
        return json.loads(
            (self.project / '.gsp/runs/work/state.json').read_text(encoding='utf-8')
        )

    def test_cli_legacy_report_contract_revision_is_not_old_exact_replay(self):
        from gsp.identity_store import IdentityStore
        st = self.state()
        self.assertNotIn('current_contract', st)
        child = session_binding(self.project, st['work_uuid'], 'child', 'codex',
                                'legacy-contract-report-thread')
        context = self.project / child.handle_path
        self.cli_as(
            context, 'session-register', 'work', '--session', 'S02',
            '--role', 'worker', '--host', 'codex',
            '--thread', 'legacy-contract-report-thread', '--task', 'T01',
            '--worktree', str(self.project / 'contract-report-worker'),
            '--write-mode', 'write', '--path', 'services', '--context', str(context),
        )
        argv = ('session-report', 'work', 'T01', '--session', 'S02',
                '--status', 'DONE', '--summary', 'same typed report, revised contract',
                '--files', 'services/one.py', '--tests', '1 passed')
        initial_plan = self.project / st['documents']['plan']
        initial_plan.write_text('# Service implementation plan\n\n## Task T01\n\n'
                                'Implement the service and verify its accepted behavior.\n',
                                encoding='utf-8')
        self.cli('seal-plan', 'work')
        self.cli_as(context, *argv)
        store = IdentityStore(self.project)
        state_file = self.project / '.gsp/runs/work/state.json'
        registry_file = self.project / '.gsp/runs/work/sessions/index.json'
        doc = self.project / self.state()['doc_dir'] / 'sessions'
        previous_uid = self.state()['_command_commit']['action_uid']
        for kind in ['plan', 'design']:
            with self.subTest(kind=kind):
                current = self.state()
                target = self.project / current['documents'][kind]
                target.write_text(target.read_text(encoding='utf-8')
                                  + '\nRevision changes the actual acceptance input: ' + kind + '\n',
                                  encoding='utf-8')
                if kind == 'design':
                    self.cli('documents-adopt', 'work', '--spec', current['documents']['design'],
                             '--plan', current['documents']['plan'], '--reason', 'real contract revision')
                    self.cli('approve', 'work', '--quote', 'approve revised fixture contract')
                self.cli('seal-plan', 'work')
                before_count = store.operation_count()
                before = (state_file.read_bytes(), registry_file.read_bytes(),
                          {p.relative_to(doc).as_posix(): p.read_bytes()
                           for p in doc.rglob('*') if p.is_file()})
                rejected = run(
                    [sys.executable, str(CLI), '--project', str(self.project), *argv],
                    capture_output=True, text=True, encoding='utf-8', env=self.cli_env(context),
                )
                self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
                self.assertIn('SESSION_REPORT_REQUEST_CONFLICT', rejected.stdout + rejected.stderr)
                self.assertEqual(store.operation_count(), before_count)
                self.assertEqual(before, (state_file.read_bytes(), registry_file.read_bytes(),
                                 {p.relative_to(doc).as_posix(): p.read_bytes()
                                  for p in doc.rglob('*') if p.is_file()}))
                fresh = list(argv)
                fresh[fresh.index('--summary') + 1] = 'new result after explicit ' + kind + ' revision'
                result = json.loads(self.cli_as(context, *fresh).stdout)
                self.assertFalse(result.get('replayed', False))
                new_uid = self.state()['_command_commit']['action_uid']
                self.assertNotEqual(previous_uid, new_uid)
                self.assertEqual(store.operation_count(), before_count + 1)
                snapshot = (state_file.read_bytes(), registry_file.read_bytes(),
                            {p.relative_to(doc).as_posix(): p.read_bytes()
                             for p in doc.rglob('*') if p.is_file()})
                result = json.loads(self.cli_as(context, *fresh).stdout)
                self.assertTrue(result['replayed'])
                self.assertEqual(store.operation_count(), before_count + 1)
                self.assertEqual(snapshot, (state_file.read_bytes(), registry_file.read_bytes(),
                                 {p.relative_to(doc).as_posix(): p.read_bytes()
                                  for p in doc.rglob('*') if p.is_file()}))
                previous_uid = new_uid

    def test_cli_session_report_semantic_identity_and_exact_replay(self):
        st = self.state()
        child_b = session_binding(
            self.project, st['work_uuid'], 'child', 'codex', 'report-thread',
        )
        child_context = self.project / child_b.handle_path
        self.cli_as(
            child_context, 'session-register', 'work', '--session', 'S02',
            '--role', 'worker', '--host', 'codex', '--thread', 'report-thread',
            '--task', 'T01', '--worktree', str(self.project / 'report-worker'),
            '--write-mode', 'write', '--path', 'services',
            '--context', str(child_context),
        )

        state_file = self.project / '.gsp/runs/work/state.json'
        registry_file = self.project / '.gsp/runs/work/sessions/index.json'
        report_dir = self.project / self.state()['doc_dir'] / 'sessions'
        report_file = report_dir / 'T01-report.md'
        store = IdentityStore(self.project)
        before_operations = store.operation_count()

        first_argv = (
            'session-report', 'work', 'T01', '--session', 'S02',
            '--status', 'DONE', '--summary', 'first semantic report',
            '--files', 'services/one.py', '--tests', '1 passed',
        )
        self.cli_as(child_context, *first_argv)
        first_state = json.loads(state_file.read_text(encoding='utf-8'))
        first_uid = first_state['_command_commit']['action_uid']
        first_bytes = report_file.read_bytes()
        first_registry = json.loads(registry_file.read_text(encoding='utf-8'))
        self.assertEqual(store.operation_count(), before_operations + 1)
        self.assertEqual(len(first_registry['outbox']), 1)

        replay = json.loads(self.cli_as(child_context, *first_argv).stdout)
        replay_state = json.loads(state_file.read_text(encoding='utf-8'))
        replay_registry = json.loads(registry_file.read_text(encoding='utf-8'))
        self.assertEqual(store.operation_count(), before_operations + 1)
        self.assertEqual(report_file.read_bytes(), first_bytes)
        self.assertEqual(len(list((report_dir / 'history').glob('T01-report-*.md'))), 0)
        self.assertEqual(len(replay_registry['outbox']), 1)
        self.assertEqual(replay_state['_command_commit']['action_uid'], first_uid)
        self.assertTrue(replay.get('replayed'), replay)

        second_argv = (
            'session-report', 'work', 'T01', '--session', 'S02',
            '--status', 'DONE', '--summary', 'second semantic report',
            '--files', 'services/one.py', '--tests', '1 passed',
        )
        self.cli_as(child_context, *second_argv)
        second_state = json.loads(state_file.read_text(encoding='utf-8'))
        second_uid = second_state['_command_commit']['action_uid']
        second_bytes = report_file.read_bytes()
        second_registry = json.loads(registry_file.read_text(encoding='utf-8'))
        self.assertNotEqual(second_uid, first_uid)
        self.assertEqual(store.operation_count(), before_operations + 2)
        self.assertIn(b'second semantic report', second_bytes)
        self.assertEqual(len(list((report_dir / 'history').glob('T01-report-*.md'))), 1)
        self.assertEqual(len(second_registry['outbox']), 2)

        third_argv = (
            'session-report', 'work', 'T01', '--session', 'S02',
            '--status', 'CHANGE_REQUEST', '--summary', 'second semantic report',
            '--files', 'services/one.py', '--tests', '1 passed',
        )
        self.cli_as(child_context, *third_argv)
        third_state = json.loads(state_file.read_text(encoding='utf-8'))
        third_registry = json.loads(registry_file.read_text(encoding='utf-8'))
        self.assertNotIn(
            third_state['_command_commit']['action_uid'],
            {first_uid, second_uid},
        )
        self.assertEqual(store.operation_count(), before_operations + 3)
        self.assertEqual(len(list((report_dir / 'history').glob('T01-report-*.md'))), 2)
        self.assertEqual(len(third_registry['outbox']), 3)
        self.assertEqual(
            third_registry['tasks']['T01']['report_status'],
            'CHANGE_REQUEST',
        )


    def test_cli_report_thread_alias_first_request_and_replay_share_identity(self):
        st = self.state()
        child_b = session_binding(
            self.project, st['work_uuid'], 'child', 'codex', 'alias-thread',
        )
        child_context = self.project / child_b.handle_path
        self.cli_as(
            child_context, 'session-register', 'work', '--session', 'S02',
            '--role', 'worker', '--host', 'codex', '--thread', 'alias-thread',
            '--task', 'T01', '--worktree', str(self.project / 'alias-worker'),
            '--write-mode', 'write', '--path', 'services',
            '--context', str(child_context),
        )

        argv = (
            'session-report', 'work', 'T01', '--session', 'alias-thread',
            '--status', 'DONE', '--summary', 'thread alias exact replay',
            '--files', 'services/one.py', '--tests', '1 passed',
        )
        state_file = self.project / '.gsp/runs/work/state.json'
        registry_file = self.project / '.gsp/runs/work/sessions/index.json'
        report_dir = self.project / self.state()['doc_dir'] / 'sessions'
        report_file = report_dir / 'T01-report.md'
        store = IdentityStore(self.project)
        before_operations = store.operation_count()

        self.cli_as(child_context, *argv)
        first_state = json.loads(state_file.read_text(encoding='utf-8'))
        first_bytes = report_file.read_bytes()
        first_registry = json.loads(registry_file.read_text(encoding='utf-8'))

        replay = json.loads(self.cli_as(child_context, *argv).stdout)
        replay_state = json.loads(state_file.read_text(encoding='utf-8'))
        replay_registry = json.loads(registry_file.read_text(encoding='utf-8'))

        self.assertTrue(replay.get('replayed'), replay)
        self.assertEqual(store.operation_count(), before_operations + 1)
        self.assertEqual(
            replay_state['_command_commit'],
            first_state['_command_commit'],
        )
        self.assertEqual(report_file.read_bytes(), first_bytes)
        self.assertEqual(
            len(list((report_dir / 'history').glob('T01-report-*.md'))),
            0,
        )
        self.assertEqual(replay_registry['outbox'], first_registry['outbox'])

    def test_cli_report_authorization_fails_before_stale_ready_refresh(self):
        st = self.state()
        child_b = session_binding(
            self.project, st['work_uuid'], 'child', 'codex', 'authorization-worker',
        )
        child_context = self.project / child_b.handle_path
        self.cli_as(
            child_context, 'session-register', 'work', '--session', 'S02',
            '--role', 'worker', '--host', 'codex', '--thread', 'authorization-worker',
            '--task', 'T01', '--worktree', str(self.project / 'authorization-worker'),
            '--write-mode', 'write', '--path', 'services',
            '--context', str(child_context),
        )
        self.cli(
            'task-add', 'work', 'T02', '--title', 'Second task',
            '--acceptance', 'must not be reportable by the T01 worker',
        )
        state_file = self.project / '.gsp/runs/work/state.json'
        registry_file = self.project / '.gsp/runs/work/sessions/index.json'
        state = self.state()
        state['status'] = 'ready'
        state['delivery_fingerprint'] = 'stale-before-report-authorization'
        state_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )
        before_state = state_file.read_bytes()
        before_registry = registry_file.read_bytes()

        missing = run(
            [
                sys.executable, str(CLI), '--project', str(self.project),
                'session-report', 'work', 'T01', '--session', 'S02',
                '--status', 'DONE', '--summary', 'missing caller must fail',
            ],
            capture_output=True, text=True, encoding='utf-8',
            env=self.cli_env(None),
        )
        self.assertNotEqual(missing.returncode, 0, missing.stdout + missing.stderr)
        self.assertIn('SESSION_CONTEXT_REQUIRED', missing.stderr)
        self.assertEqual(state_file.read_bytes(), before_state)
        self.assertEqual(registry_file.read_bytes(), before_registry)

        wrong_task = run(
            [
                sys.executable, str(CLI), '--project', str(self.project),
                'session-report', 'work', 'T02', '--session', 'S02',
                '--status', 'DONE', '--summary', 'wrong task must fail',
            ],
            capture_output=True, text=True, encoding='utf-8',
            env=self.cli_env(child_context),
        )
        self.assertNotEqual(
            wrong_task.returncode, 0, wrong_task.stdout + wrong_task.stderr,
        )
        self.assertIn('SESSION_TASK_MISMATCH', wrong_task.stderr)
        self.assertEqual(state_file.read_bytes(), before_state)
        self.assertEqual(registry_file.read_bytes(), before_registry)


if __name__ == '__main__':
    unittest.main()
