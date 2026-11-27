from process_helpers import docfolder
from process_helpers import document
"""Real unittest subprocesses in isolated fixture projects, no paid/live services."""
from pathlib import Path
import json
import os
import subprocess
from types import SimpleNamespace
from process_helpers import run, popen, session_binding
from gsp.codex_hooks import handle as codex_hook
from gsp.runtime import Project, WorkflowError, dispatch, parser, read
from gsp.sessions import register_session
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts/gsp.py'


class EndToEndTests(unittest.TestCase):
    def test_two_tasks_installed_hosts_and_real_regression(self):
        with tempfile.TemporaryDirectory(prefix='gsp-e2e-') as temp:
            project = Path(temp) / '项目 project space'
            project.mkdir()
            user_home = Path(temp) / 'isolated user profile'
            user_home.mkdir()
            context_env = {}

            def call(*args, script=CLI, ok=True):
                # Health also inspects user installs; keep the fixture independent of the machine profile.
                env = os.environ.copy()
                env.update({'HOME': str(user_home), 'USERPROFILE': str(user_home),
                            'CODEX_HOME': str(user_home / '.codex')})
                env.update(context_env)
                r = run([sys.executable, str(script), '--project', str(project), *args],
                        capture_output=True, encoding='utf-8', errors='replace', env=env)
                if ok: self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                else: self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                return r

            call('install', '--host', 'both', '--optional', 'health')
            call('install', '--host', 'both', '--optional', 'health')  # ownership-aware idempotence
            for base in ('.claude', '.agents'):
                skills = list((project / base / 'skills').glob('gsp-*/SKILL.md'))
                self.assertEqual(len(skills), 7)
                self.assertFalse((project / base / 'skills/gsp-retro').exists())
                # design-review is now an internalized workflow method.
                self.assertTrue((project / base / 'skills/gsp-workflow/methods/gsp-design-review/METHOD.md').is_file())
                installed_cli = project / base / 'skills/gsp-workflow/scripts/gsp.py'
                report = json.loads(call('health', '--host', 'both', script=installed_cli).stdout)
                self.assertTrue(report['static_ok'])
                self.assertFalse(report['live_verified'])
                self.assertTrue((installed_cli.parent.parent / 'THIRD_PARTY_NOTICES.md').is_file())
            call('init')
            self.assertIn('.gsp/', (project / '.gitignore').read_text(encoding='utf-8'))
            call('new', 'hello', '--title', 'Fixture greeting', '--mode', 'unattended', '--quote', 'SYNTHETIC TEST ONLY: use recommendations')
            hello = json.loads((project / '.gsp/runs/hello/state.json').read_text(encoding='utf-8'))
            hello.pop('convergence_schema', None)
            Project(project).write(
                hello, 'fixture-legacy-e2e', 'published legacy fixture behavior',
                action_uid='fixture:e2e:legacy-hello',
            )
            first = session_binding(project, hello['work_uuid'], 'main', 'codex', 'e2e-main')
            context_env['GSP_SESSION_CONTEXT'] = str(project / first.handle_path)
            register_session(Project(project), hello, SimpleNamespace(
                role='controller', write_mode='write', session=None, host='codex',
                thread='e2e-main', task=None, worktree=str(project), branch=None,
                path=['.'], takeover=False, reason='', workspace_group=None,
                context=str(project / first.handle_path),
            ))
            docs = docfolder(project,'hello')
            document(project, 'hello', 'design').write_text('# Fixture design\nNormalize whitespace and greet a name.\n', encoding='utf-8')
            call('approve', 'hello', '--quote', 'SYNTHETIC TEST ONLY: approve this fixture design')
            call('task-add', 'hello', 'T1', '--title', 'Normalize', '--acceptance', 'Whitespace is trimmed')
            call('task-add', 'hello', 'T2', '--title', 'Greet', '--acceptance', 'Greeting uses normalized name', '--depends', 'T1')
            document(project, 'hello', 'plan').write_text('# Fixture plan\nT1 normalize and test. T2 greet and integration test.\n', encoding='utf-8')
            call('seal-plan', 'hello')
            call('scope', 'hello', '--off', '--quote', 'SYNTHETIC TEST ONLY: no freeze')
            call('advance', 'hello', '--to', 'execute')
            call('task-start', 'hello', 'T2', ok=False)
            call('task-start', 'hello', 'T1')
            (project / 'app.py').write_text('def normalize(name):\n    return name.strip()\n', encoding='utf-8')
            test = project / 'test_app.py'
            test.write_text("import unittest\nfrom app import normalize\nclass AppTests(unittest.TestCase):\n def test_normalize(self): self.assertEqual(normalize(' Ada '), 'Ada')\n", encoding='utf-8')
            call('run', 'hello', '--task', 'T1', '--kind', 'tests', '--', sys.executable, '-m', 'unittest', '-v')
            call('task-done', 'hello', 'T1')
            call('task-start', 'hello', 'T2')
            with (project / 'app.py').open('a', encoding='utf-8') as stream:
                stream.write("\ndef greet(name):\n    return 'Hello ' + normalize(name)\n")
            with test.open('a', encoding='utf-8') as stream:
                stream.write(" def test_greet(self):\n  from app import greet\n  self.assertEqual(greet(' Ada '), 'Hello Ada')\n")
            call('run', 'hello', '--task', 'T2', '--kind', 'tests', '--', sys.executable, '-m', 'unittest', '-v')
            call('task-done', 'hello', 'T2')
            (docs / 'review.md').write_text('# Fixture review\nSelf reviewed normalize, greet and both real assertions; browser not applicable.\n', encoding='utf-8')
            call('ready', 'hello', ok=False)
            call('run', 'hello', '--task', 'all', '--kind', 'tests', '--', sys.executable, '-m', 'unittest', '-v')
            call('advance', 'hello', '--to', 'review')
            call('review', 'hello', '--reviewer', 'self', '--blocking', '0')
            call('advance', 'hello', '--to', 'checking')
            call('advance', 'hello', '--to', 'ready')
            call('ready', 'hello')
            self.assertIn('READY', (docs / 'delivery.md').read_text(encoding='utf-8'))
            state = json.loads(call('status', 'hello').stdout)
            receipts = [json.loads((project / p).read_text(encoding='utf-8')) for p in state['evidence']]
            self.assertEqual([r['tests_ran'] for r in receipts], [1, 2, 2])
            call('save', 'hello')
            call('resume', 'hello')

    def test_same_room_second_session_is_denied_before_overwrite(self):
        with tempfile.TemporaryDirectory(prefix='gsp-cross-overwrite-') as temp:
            root = Path(temp) / 'project'
            root.mkdir()
            subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=root, check=True)
            subprocess.run(['git', 'config', 'user.name', 'E2E Test'], cwd=root, check=True)
            subprocess.run(['git', 'config', 'user.email', 'e2e@example.invalid'], cwd=root, check=True)
            (root / 'README.md').write_text('# e2e\n', encoding='utf-8')
            subprocess.run(['git', 'add', 'README.md'], cwd=root, check=True)
            subprocess.run(['git', 'commit', '-qm', 'chore: baseline'], cwd=root, check=True)
            project = Project(root)
            project.initialize()
            dispatch(project, parser().parse_args(['new', 'cross', '--title', '同 room 覆盖防护', '--intent', 'cross-session']))
            dispatch(project, parser().parse_args(['task-add', 'cross', 'T01', '--title', 'unit', '--acceptance', 'unit contract']))
            dispatch(project, parser().parse_args(['topology-select', 'cross', '--mode', 'in-place', '--quote', 'SYNTHETIC in-place for same-room test']))
            cross_state = read(root / '.gsp/runs/cross/state.json')
            first_binding = session_binding(root, cross_state['work_uuid'], 'main', 'codex', 'first-main')
            dispatch(project, parser().parse_args([
                'session-register', 'cross', '--role', 'controller', '--host', 'codex',
                '--thread', 'first-main', '--write-mode', 'write', '--path', 'services',
                '--context', str(root / first_binding.handle_path)]))
            existing = root / 'services' / 'quant-control.service'
            existing.parent.mkdir(parents=True)
            existing.write_text('first session version\n', encoding='utf-8')
            allowed = codex_hook({
                'hook_event_name': 'PreToolUse', 'tool_name': 'Write', 'cwd': str(root),
                'session_id': 'first-main', 'tool_input': {'file_path': str(existing)},
            })
            self.assertEqual(allowed, {})
            state_path = root / '.gsp/runs/cross/state.json'
            registry_path = root / '.gsp/runs/cross/sessions/index.json'
            state_before = state_path.read_bytes()
            registry_before = registry_path.read_bytes() if registry_path.is_file() else None
            source_before = existing.read_bytes()
            with self.assertRaisesRegex(WorkflowError, 'SESSION_PATH_CONFLICT'):
                dispatch(project, parser().parse_args([
                    'session-register', 'cross', '--role', 'worker', '--host', 'claude',
                    '--thread', 'second-main', '--task', 'T01', '--write-mode', 'write',
                    '--path', 'services/quant-control.service']))
            self.assertEqual(existing.read_bytes(), source_before)
            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertEqual(
                registry_path.read_bytes() if registry_path.is_file() else None,
                registry_before,
            )
            registry = json.loads(registry_path.read_text(encoding='utf-8'))
            self.assertFalse(any(
                row.get('thread_id') == 'second-main'
                for row in registry.get('sessions', [])
            ))
            denied = codex_hook({
                'hook_event_name': 'PreToolUse', 'tool_name': 'Write', 'cwd': str(root),
                'session_id': 'second-main', 'tool_input': {'file_path': str(existing)},
            })
            self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')
            self.assertIn('SESSION_', denied['hookSpecificOutput']['permissionDecisionReason'])
            self.assertEqual(existing.read_text(encoding='utf-8'), 'first session version\n')

    def test_summary_parser_unknown_and_zero(self):
        sys.path.insert(0, str(ROOT))
        from gsp.runtime import test_count
        self.assertEqual(test_count('Ran 0 tests\nprevious run: 3 passed'), 0)
        self.assertIsNone(test_count('Looks good'))


if __name__ == '__main__':
    unittest.main()
