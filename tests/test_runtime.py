from process_helpers import docfolder
from process_helpers import document
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from process_helpers import run, popen, session_binding
import sys
import tempfile
import time
import signal
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts' / 'gsp.py'


class LegacyWorkflowTests(unittest.TestCase):
    """Persisted tasks from before the controller/topology schema still work.

    Current task creation and stage order are covered separately by
    test_end_to_end, test_handoff_controller and test_topology_lifecycle.
    """
    def test_ignored_test_script_change_invalidates_ready_on_status(self):
        folder=self.task()
        script=self.project/'.gsp/local-tests.py'
        script.write_text("print('Ran 1 test')",encoding='utf-8')
        self.cli('run','login','--task','T1','--',sys.executable,str(script))
        self.cli('task-done','login','T1')
        self.cli('run','login','--task','all','--',sys.executable,str(script))
        (folder/'review.md').write_text('# Review\nFixture reviewed.',encoding='utf-8')
        self.cli('review','login','--reviewer','self','--blocking','0')
        self.cli('ready','login')
        script.write_text("print('Ran 2 tests')",encoding='utf-8')
        self.assertEqual(json.loads(self.cli('status','login').stdout)['status'],'needs-verification')

    def test_observed_commit_preserves_ready_only_for_unchanged_inputs(self):
        def git(*args):
            result = run(['git', '-C', str(self.project), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', '-c', 'core.hooksPath=', *args], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout
        git('init', '-b', 'main')
        git('commit', '--allow-empty', '-m', 'baseline')
        folder = self.task()
        self.passing()
        self.cli('task-done', 'login', 'T1')
        self.cli('run', 'login', '--task', 'all', '--', sys.executable, '-c', "print('Ran 1 test')")
        (folder/'review.md').write_text('# Review\nActual fixture reviewed, no blockers.', encoding='utf-8')
        self.cli('review', 'login', '--reviewer', 'self', '--blocking', '0')
        self.cli('ready', 'login')
        delivery = (folder/'delivery.md').read_bytes()
        git('add', '.')
        git('commit', '-m', 'validated input')
        self.cli('observe-head', 'login', '--reason', 'Checked commit contains the already validated working tree')
        self.assertEqual(json.loads(self.cli('status', 'login').stdout)['status'], 'ready')
        self.assertEqual((folder/'delivery.md').read_bytes(), delivery)
        self.cli('save', 'login'); self.cli('resume', 'login')
        self.assertEqual(git('status', '--porcelain'), b'')
        (self.project/'changed.txt').write_text('new input')
        git('add', '.'); git('commit', '-m', 'changed input')
        self.cli('observe-head', 'login', '--reason', 'Changed input requires fresh verification')
        self.assertEqual(json.loads(self.cli('status', 'login').stdout)['status'], 'needs-verification')
        self.cli('ready', 'login', ok=False)

    def test_fresh_context_reviewer_is_recorded_without_independence_claim(self):
        folder = self.task()
        (folder / 'review.md').write_text('# Review\nFresh context, same provider.\n', encoding='utf-8')
        self.cli('review', 'login', '--reviewer', 'fresh-context', '--blocking', '0')
        state = json.loads(self.cli('status', 'login').stdout)
        self.assertEqual(state['review']['reviewer'], 'fresh-context')

    def test_first_git_repository_requires_explicit_reconciliation(self):
        self.task(with_git=False)
        result=run(['git','-C',str(self.project),'init','-b','main'],capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.reissue_context()
        self.assertIn('BRANCH_MISMATCH',self.cli('status','login',ok=False).stderr)
        self.cli('decision','login','--text','Do not silently adopt','--source','agent',ok=False)
        self.cli('observe-head','login','--reason','SYNTHETIC checked newly initialized repository at the same physical project root')
        state=json.loads(self.cli('status','login').stdout)
        self.assertEqual(state['git']['root'],str(self.project.resolve()))
        self.assertIsNone(state['head_reconciliation']['previous']['root'])

    def test_first_parent_git_repository_cannot_be_adopted_as_project_root(self):
        self.task(with_git=False)
        result=run(['git','-C',self.tmp.name,'init','-b','main'],capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.reissue_context()
        self.assertIn('CONTROL_ROOT_TOPOLOGY_UNSUPPORTED',self.cli('observe-head','login','--reason','SYNTHETIC parent repository mismatch',ok=False).stderr)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-test-')
        self.project = Path(self.tmp.name) / 'project with spaces'
        self.project.mkdir()
        self.context = None

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, ok=True):
        r = run([sys.executable, str(CLI), '--project', str(self.project), *args],
                text=True, encoding='utf-8', capture_output=True, env=self.cli_env())
        if ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        else:
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def cli_env(self):
        env = os.environ.copy()
        env.pop('GSP_SESSION_CONTEXT', None)
        if self.context is not None:
            env['GSP_SESSION_CONTEXT'] = str(self.context)
        env.update({
            'HOME': str(self.tmp.name),
            'USERPROFILE': str(self.tmp.name),
            'CODEX_HOME': str(Path(self.tmp.name) / '.codex'),
        })
        return env

    def task(self, with_git=True):
        if with_git and not (self.project / '.git').exists():
            result = run(
                ['git', '-C', str(self.project), 'init', '-q', '-b', 'main'],
                capture_output=True, text=True, encoding='utf-8', env=self.cli_env(),
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            (self.project / '.gitignore').write_text('.gsp/\n', encoding='utf-8')
            for argv in (
                ['add', '.gitignore'],
                ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', 'chore: fixture baseline'],
            ):
                result = run(
                    ['git', '-C', str(self.project), *argv],
                    capture_output=True, text=True, encoding='utf-8', env=self.cli_env(),
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.cli('init')
        self.cli('new', 'login', '--title', 'Login')
        state_file = self.project / '.gsp/runs/login/state.json'
        legacy = json.loads(state_file.read_text(encoding='utf-8'))
        binding = session_binding(
            self.project, legacy['work_uuid'], 'main', 'codex', 'legacy-main',
        )
        self.context = self.project / binding.handle_path
        legacy.pop('controller_required', None)
        legacy.pop('convergence_schema', None)
        legacy.pop('git_topology', None)
        # This fixture deliberately represents a task from before journaling.
        # Do not retain a modern commit marker over different legacy bytes.
        legacy.pop('_command_commit', None)
        state_file.write_text(json.dumps(legacy), encoding='utf-8')
        folder = docfolder(self.project,'login')
        document(self.project, 'login', 'design').write_text('# Design\nLogin returns a session.\n', encoding='utf-8')
        self.cli('approve', 'login', '--quote', 'I approve this design')
        self.cli('task-add', 'login', 'T1', '--title', 'Implement login', '--acceptance', 'Unit tests pass')
        document(self.project, 'login', 'plan').write_text('# Plan\nT1: implement and test login.\n', encoding='utf-8')
        self.cli('seal-plan', 'login')
        self.cli('task-start', 'login', 'T1')
        return folder

    def reissue_context(self):
        """Bind the existing work to the repository visible after Git init."""
        state_file = self.project / '.gsp/runs/login/state.json'
        work_uid = json.loads(state_file.read_text(encoding='utf-8'))['work_uuid']
        binding = session_binding(
            self.project, work_uid, 'main', 'codex', 'legacy-main-rebound',
        )
        self.context = self.project / binding.handle_path

    def passing(self, kind='tests'):
        return self.cli('run', 'login', '--task', 'T1', '--kind', kind, '--', sys.executable,
                        '-c', "print('Ran 1 test'); print('OK')")

    def completed(self):
        folder = self.task()
        self.passing()
        self.cli('task-done', 'login', 'T1')
        self.cli('run', 'login', '--task', 'all', '--kind', 'tests', '--', sys.executable, '-c', "print('Ran 1 test')")
        (folder / 'review.md').write_text('# Review\nChecked fixture, no blocking findings.\n', encoding='utf-8')
        self.cli('review', 'login', '--reviewer', 'self', '--blocking', '0')
        self.cli('ready', 'login')
        return folder

    def test_design_change_revokes_approval(self):
        folder = self.task()
        document(self.project, 'login', 'design').write_text('Changed requirement', encoding='utf-8')
        self.cli('task-start', 'login', 'T1', ok=False)

    def test_zero_tests_and_failure_do_not_complete(self):
        self.task()
        self.cli('run', 'login', '--task', 'T1', '--kind', 'tests', '--', sys.executable,
                 '-c', "print('Ran 0 tests'); print('OK')", ok=False)
        self.cli('task-done', 'login', 'T1', ok=False)
        self.cli('run', 'login', '--task', 'T1', '--kind', 'tests', '--', sys.executable,
                 '-c', "print('Ran 1 test'); raise SystemExit(1)", ok=False)
        self.cli('task-done', 'login', 'T1', ok=False)

    def test_changed_code_invalidates_evidence(self):
        self.task()
        self.passing()
        (self.project / 'app.py').write_text('print(1)', encoding='utf-8')
        self.cli('task-done', 'login', 'T1', ok=False)

    def test_plan_change_blocks_execution(self):
        folder = self.task()
        document(self.project, 'login', 'plan').write_text('new plan', encoding='utf-8')
        self.passing_failure = self.cli('task-start', 'login', 'T1', ok=False)

    def test_task_start_needs_no_legacy_scope_decision(self):
        self.task()
        state = json.loads((self.project / '.gsp/runs/login/state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['scope']['mode'], 'pending')
        self.assertEqual(state['tasks'][0]['status'], 'running')

    def test_scope_command_is_noop_and_legacy_scope_has_no_directory_authority(self):
        self.task()
        state_file = self.project / '.gsp/runs/login/state.json'
        active_file = self.project / '.gsp/active.json'
        original_state = state_file.read_bytes()
        original_active = active_file.read_bytes()
        result = json.loads(self.cli(
            'scope', 'login', '--path', 'src', '--quote', 'legacy directory freeze',
        ).stdout)
        self.assertTrue(result['deprecated'])
        self.assertTrue(result['no_op'])
        self.assertFalse(result['write_grant'])
        self.assertEqual(state_file.read_bytes(), original_state)
        self.assertEqual(active_file.read_bytes(), original_active)

        src = self.project / 'src'
        src.mkdir()
        state = json.loads(state_file.read_text(encoding='utf-8'))
        for mode in ('pending', 'frozen', 'off'):
            with self.subTest(mode=mode):
                legacy = dict(state)
                legacy['scope'] = {
                    'mode': mode,
                    'path': str(src) if mode == 'frozen' else None,
                    'quote': 'legacy mode',
                    'at': '2000-01-01T00:00:00+00:00',
                }
                state_file.write_text(json.dumps(legacy), encoding='utf-8')
                preview = json.loads(
                    self.cli('check-path', 'login', 'outside.py').stdout
                )
                self.assertTrue(preview['preview'])
                self.assertFalse(preview['write_grant'])
                self.assertEqual(preview['conflicts'], [])
        state_file.write_bytes(original_state)

    def test_scope_noop_bypasses_context_replay_and_all_ledger_writes(self):
        from gsp.runtime import parser, request_action_uid

        self.cli('init')
        self.cli('new', 'login', '--title', 'Login')
        folder = docfolder(self.project, 'login')
        self.cli('new', 'other', '--title', '另一个活动工作项')
        self.context = None
        state_file = self.project / '.gsp/runs/login/state.json'
        active_file = self.project / '.gsp/active.json'
        events_file = state_file.parent / 'events.jsonl'
        dashboard = self.project / 'docs/gsp/README.md'
        record = folder / 'record.json'
        args = parser().parse_args([
            'scope', 'login', '--path', 'src',
            '--quote', 'legacy directory freeze',
        ])
        for mode in ('pending', 'frozen', 'off'):
            with self.subTest(mode=mode):
                state = json.loads(state_file.read_text(encoding='utf-8'))
                state['scope'] = {
                    'mode': mode,
                    'path': 'src' if mode == 'frozen' else None,
                    'quote': 'legacy fixture',
                    'at': '2000-01-01T00:00:00+00:00',
                }
                state['_command_commit'] = {
                    'schema': 99,
                    'command': 'scope',
                    'request_digest': request_action_uid('scope', args),
                    'action_uid': 'fixture-action',
                    'detail': 'legacy fixture',
                }
                state_file.write_text(
                    json.dumps(state, ensure_ascii=False, indent=2) + '\n',
                    encoding='utf-8',
                )
                before = {
                    'state': state_file.read_bytes(),
                    'active': active_file.read_bytes(),
                    'events': events_file.read_bytes() if events_file.exists() else None,
                    'dashboard': dashboard.read_bytes() if dashboard.exists() else None,
                    'record': record.read_bytes() if record.exists() else None,
                }
                result = json.loads(self.cli(
                    'scope', 'login', '--path', 'src',
                    '--quote', 'legacy directory freeze',
                ).stdout)
                self.assertTrue(result['deprecated'])
                self.assertTrue(result['no_op'])
                self.assertFalse(result['write_grant'])
                self.assertEqual(state_file.read_bytes(), before['state'])
                self.assertEqual(active_file.read_bytes(), before['active'])
                self.assertEqual(
                    events_file.read_bytes() if events_file.exists() else None,
                    before['events'],
                )
                self.assertEqual(
                    dashboard.read_bytes() if dashboard.exists() else None,
                    before['dashboard'],
                )
                self.assertEqual(
                    record.read_bytes() if record.exists() else None,
                    before['record'],
                )

    def test_legacy_scope_paths_do_not_gate_shell_changes(self):
        self.task()
        (self.project / 'src').mkdir()
        self.cli('scope', 'login', '--path', 'src', '--quote', 'Only modify src')
        preview = json.loads(self.cli('check-path', 'login', 'outside.py').stdout)
        self.assertFalse(preview['write_grant'])
        result = self.cli('run', 'login', '--task', 'T1', '--kind', 'tests', '--', sys.executable,
                 '-c', "from pathlib import Path; Path('outside.py').write_text('x'); print('Ran 1 test')", ok=False)
        # The child executes without a scope whitelist. A changed validation
        # input still invalidates its test receipt, independently of scope.
        self.assertEqual((self.project / 'outside.py').read_text(encoding='utf-8'), 'x')
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt['status'], 'inputs-changed')
        self.assertEqual(receipt['scope_violations'], [])

    def test_ready_needs_review_and_current_evidence(self):
        folder = self.task()
        self.passing()
        self.cli('task-done', 'login', 'T1')
        self.cli('ready', 'login', ok=False)
        (folder / 'review.md').write_text('# Review\nChecked login, no blocking findings.\n', encoding='utf-8')
        self.cli('review', 'login', '--reviewer', 'self', '--blocking', '0')
        self.cli('ready', 'login', ok=False)
        self.cli('run', 'login', '--task', 'all', '--kind', 'tests', '--', sys.executable,
                 '-c', "print('Ran 1 test'); print('OK')")
        self.cli('ready', 'login')
        self.assertIn('READY', (folder / 'delivery.md').read_text(encoding='utf-8'))
        (self.project / 'changed.txt').write_text('new', encoding='utf-8')
        self.cli('ready', 'login', ok=False)

    def test_resume_detects_project_identity(self):
        self.task()
        self.cli('save', 'login')
        self.cli('resume', 'login')
        path = self.project / '.gsp/project.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['id'] = 'different'
        path.write_text(json.dumps(data), encoding='utf-8')
        self.cli('resume', 'login', ok=False)

    def test_unattended_needs_explicit_quote(self):
        self.cli('init')
        self.cli('new', 'night', '--title', 'Night', '--mode', 'unattended', ok=False)
        self.cli('new', 'night', '--title', 'Night', '--mode', 'unattended',
                 '--quote', 'Use recommended decisions tonight')

    def test_authority_is_scoped(self):
        self.task()
        self.cli('authorize', 'login', 'push', '--scope', 'feature/login', '--quote', 'Push feature/login')
        self.cli('authority', 'login', 'push', '--scope', 'feature/login')
        self.cli('authority', 'login', 'merge', '--scope', 'feature/login', ok=False)
        self.cli('authority', 'login', 'push', '--scope', 'main', ok=False)

    def test_dependency_cycle_rejected(self):
        self.task()
        self.cli('task-add', 'login', 'T2', '--title', 'second', '--acceptance', 'tests', '--depends', 'T2', ok=False)

    def test_unauthorized_install_overwrite_refused(self):
        dest = self.project / '.agents/skills/gsp-workflow'
        dest.mkdir(parents=True)
        (dest / 'SKILL.md').write_text('user content', encoding='utf-8')
        self.cli('install', '--host', 'codex', ok=False)
        self.assertEqual((dest / 'SKILL.md').read_text(encoding='utf-8'), 'user content')

    def test_reserved_all_task_rejected(self):
        self.task()
        self.cli('task-add', 'login', 'all', '--title', 'invalid', '--acceptance', 'tests', ok=False)

    def test_frozen_scope_record_does_not_block_new_task(self):
        self.task()
        (self.project / 'src').mkdir()
        self.cli('scope', 'login', '--path', 'src', '--quote', 'Only src')
        self.cli('new', 'second', '--title', 'Second')
        self.assertTrue((self.project / '.gsp/runs/second/state.json').is_file())

    def test_hook_uses_ownership_not_legacy_scope_from_child_cwd(self):
        self.task()
        (self.project / 'src').mkdir()
        self.cli('scope', 'login', '--path', 'src', '--quote', 'Only src')
        payload = {'cwd': str(self.project / 'src'), 'tool_name': 'Edit',
                   'tool_input': {'file_path': str(self.project / 'outside.py')}}
        r = run([sys.executable, str(CLI), '--project', str(self.project), 'hook'],
                input=json.dumps(payload), text=True, encoding='utf-8', capture_output=True,
                env=self.cli_env())
        self.assertEqual(json.loads(r.stdout), {})
        protected = {'cwd': str(self.project), 'tool_name': 'Edit',
                     'tool_input': {'file_path': str(self.project / '.gsp/project.json')}}
        r = run([sys.executable, str(CLI), '--project', str(self.project), 'hook'],
                input=json.dumps(protected), text=True, encoding='utf-8', capture_output=True,
                env=self.cli_env())
        self.assertEqual(json.loads(r.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('CONTROL_PATH_PROTECTED', json.loads(r.stdout)['hookSpecificOutput']['permissionDecisionReason'])

    def test_hook_accepts_new_path_in_owned_workspace_with_legacy_scope(self):
        self.task()
        scope = self.project / '源码'
        scope.mkdir()
        self.cli('scope', 'login', '--path', '源码', '--quote', 'Only this source directory')
        payload = {'cwd': str(self.project), 'tool_name': 'Edit',
                   'tool_input': {'file_path': str(self.project / '登录.py')}}
        r = run([sys.executable, str(CLI), '--project', str(self.project), 'hook'],
                input=json.dumps(payload, ensure_ascii=False), text=True,
                encoding='utf-8', capture_output=True, env=self.cli_env())
        self.assertEqual(json.loads(r.stdout), {})

    def test_new_contract_rejects_old_receipt(self):
        folder = self.task()
        self.passing()
        self.cli('task-done', 'login', 'T1')
        self.cli('run', 'login', '--task', 'all', '--kind', 'tests', '--', sys.executable, '-c', "print('Ran 1 test')")
        (folder / 'review.md').write_text('# Review\nOriginal design reviewed without blockers.\n', encoding='utf-8')
        self.cli('review', 'login', '--reviewer', 'self', '--blocking', '0')
        self.cli('ready', 'login')
        document(self.project, 'login', 'design').write_text('# New design\nNow requires expiration too.\n', encoding='utf-8')
        self.assertIn('APPROVAL_STAGE_INVALID', self.cli('approve', 'login', '--quote', 'Premature approval', ok=False).stderr)
        current = json.loads((self.project / '.gsp/runs/login/state.json').read_text(encoding='utf-8'))
        self.cli('documents-adopt', 'login', '--spec', current['documents']['design'], '--plan', current['documents']['plan'],
                 '--reason', 'SYNTHETIC explicitly reopen the revised document contract')
        self.cli('approve', 'login', '--quote', 'Approve expiration')
        self.cli('seal-plan', 'login')
        self.cli('task-start', 'login', 'T1')
        self.assertIn('EVIDENCE_CONTRACT_CHANGED', self.cli('task-done', 'login', 'T1', ok=False).stderr)
        self.assertIn('TASKS_INCOMPLETE', self.cli('ready', 'login', ok=False).stderr)

    def test_install_preserves_user_edited_instruction_block(self):
        self.cli('install', '--host', 'codex')
        file = self.project / 'AGENTS.md'
        changed = file.read_text(encoding='utf-8').replace('## GstackSP', '## GstackSP\nUser custom rule')
        file.write_text(changed, encoding='utf-8')
        self.cli('install', '--host', 'codex', ok=False)
        self.assertEqual(file.read_text(encoding='utf-8'), changed)

    def test_test_output_zero_is_not_hidden_by_history(self):
        self.task()
        self.cli('run', 'login', '--task', 'T1', '--kind', 'tests', '--', sys.executable,
                 '-c', "print('Ran 0 tests'); print('previous run: 1 passed')", ok=False)

    def test_launch_failure_is_recorded_as_not_run(self):
        self.task()
        r = self.cli('run', 'login', '--task', 'T1', '--kind', 'tests', '--',
                     'gsp-no-such-executable-7d21', ok=False)
        receipt = json.loads(r.stdout)
        self.assertEqual(receipt['status'], 'not-run')
        self.assertIsNone(receipt['exit'])
        self.cli('task-done', 'login', 'T1', ok=False)

    def test_unknown_execution_blocks_rerun_until_investigated(self):
        folder = self.task()
        state_file = self.project / '.gsp/runs/login/state.json'
        state = json.loads(state_file.read_text(encoding='utf-8'))
        state['inflight'] = {'id': 'interrupted-fixture', 'argv': ['unknown'], 'task': 'T1'}
        from gsp.runtime import Project
        Project(self.project).write(state, 'fixture', 'unknown execution',
                                   action_uid='fixture:unknown-execution')
        self.cli('resume', 'login', ok=False)
        self.cli('run', 'login', '--task', 'T1', '--kind', 'tests', '--', sys.executable, '-c',
                 "print('Ran 1 test')", ok=False)
        self.cli('recover-run', 'login', '--evidence', 'missing.md', '--outcome', 'failed', ok=False)
        (folder / 'recovery.md').write_text('Fixture investigation: process stopped; no result available.', encoding='utf-8')
        self.cli('recover-run', 'login', '--evidence', str((folder/'recovery.md').relative_to(self.project)), '--outcome', 'failed')
        self.passing()

    def test_head_change_cannot_be_absorbed_by_decision(self):
        def git(*args):
            r = run(['git', '-C', str(self.project), '-c', 'user.name=Fixture',
                                '-c', 'user.email=fixture@example.invalid', *args], capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        git('init', '-b', 'main')
        git('-c', 'core.hooksPath=', 'commit', '--allow-empty', '-m', 'baseline')
        self.task()
        git('-c', 'core.hooksPath=', 'commit', '--allow-empty', '-m', 'outside change')
        self.cli('decision', 'login', '--text', 'note', '--source', 'agent', ok=False)
        self.cli('resume', 'login', ok=False)
        self.cli('observe-head', 'login', '--reason', 'Fixture checked empty commit on same branch')
        self.cli('resume', 'login')

    def test_failed_recovery_invalidates_previous_final_checks(self):
        folder = self.task()
        self.passing()
        self.cli('task-done', 'login', 'T1')
        self.cli('run', 'login', '--task', 'all', '--kind', 'tests', '--', sys.executable, '-c', "print('Ran 1 test')")
        (folder / 'review.md').write_text('# Review\nChecked all fixture changes, no blockers.\n', encoding='utf-8')
        self.cli('review', 'login', '--reviewer', 'self', '--blocking', '0')
        self.cli('ready', 'login')
        file = self.project / '.gsp/runs/login/state.json'
        state = json.loads(file.read_text(encoding='utf-8'))
        state['inflight'] = {'id': 'new-unknown-all', 'task': 'all', 'argv': ['test']}
        from gsp.runtime import Project
        Project(self.project).write(state, 'fixture', 'unknown final execution',
                                   action_uid='fixture:unknown-final-execution')
        (folder / 'recovery.md').write_text('Fixture process failed, no new passing result.', encoding='utf-8')
        self.cli('recover-run', 'login', '--evidence', str((folder/'recovery.md').relative_to(self.project)), '--outcome', 'failed')
        self.cli('ready', 'login', ok=False)

    def test_ready_task_does_not_block_new_task_without_scope_release(self):
        self.completed()
        (self.project / 'src').mkdir()
        self.cli('scope', 'login', '--path', 'src', '--quote', 'legacy freeze')
        self.cli('new', 'next', '--title', 'Next')
        self.assertTrue((self.project / '.gsp/runs/next/state.json').is_file())

    def test_close_releases_ready_task_active_and_scope(self):
        folder = self.task()
        self.passing()
        self.cli('task-done', 'login', 'T1')
        self.cli('run', 'login', '--task', 'all', '--kind', 'tests', '--', sys.executable, '-c', "print('Ran 1 test')")
        (folder / 'review.md').write_text('# Review\nChecked all changes without blockers.\n', encoding='utf-8')
        self.cli('review', 'login', '--reviewer', 'self', '--blocking', '0')
        self.cli('ready', 'login')
        current = json.loads((self.project / '.gsp/runs/login/state.json').read_text(encoding='utf-8'))
        self.assertIn('WORK_CLOSURE_CONDITION_REQUIRED', self.cli('close', 'login', '--reason', 'Missing fixture condition', ok=False).stderr)
        self.cli('work-stage', current['work_uuid'], 'closed', '--reason', 'SYNTHETIC accepted delivery',
                 '--closure-condition', 'Fixture evidence and delivery reviewed')
        self.cli('close', 'login', '--reason', 'Fixture delivery accepted and task boundary can be released')
        self.assertFalse((self.project / '.gsp/active.json').exists())
        state=json.loads((self.project / '.gsp/runs/login/state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['scope']['mode'], 'off')
        self.assertIn('close_reason', state)
        state_path = self.project / '.gsp/runs/login/state.json'
        closed_state = state_path.read_bytes()
        noop = json.loads(self.cli('scope', 'login', '--path', '.', '--quote', 'legacy freeze').stdout)
        self.assertTrue(noop['no_op'])
        self.assertEqual(state_path.read_bytes(), closed_state)
        self.assertFalse((self.project / '.gsp/active.json').exists())

    def test_real_interruption_preserves_unknown_execution(self):
        folder = self.task()
        program = "from pathlib import Path; import os,time; Path('.gsp/child.pid').write_text(str(os.getpid())); time.sleep(20)"
        runner = popen([sys.executable, str(CLI), '--project', str(self.project), 'run', 'login',
                                   '--task', 'T1', '--kind', 'tests', '--', sys.executable, '-c', program],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=self.cli_env())
        pid_file = self.project / '.gsp/child.pid'
        try:
            deadline = time.monotonic() + 10
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.exists(), 'fixture child did not start')
            runner.kill()
            runner.communicate(timeout=5)
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.communicate(timeout=5)
            if pid_file.exists():
                try: os.kill(int(pid_file.read_text()), signal.SIGTERM)
                except ProcessLookupError: pass
        state = json.loads((self.project / '.gsp/runs/login/state.json').read_text(encoding='utf-8'))
        self.assertTrue(state['inflight'])
        lock = self.project / '.gsp/writer.lock'
        suspension = self.project / '.gsp/writer-suspended.json'
        self.assertTrue(suspension.exists())
        self.assertFalse(lock.exists())
        self.cli('resume', 'login', ok=False)
        result = self.cli('resume', 'login', ok=False)
        self.assertIn('EXECUTION_UNKNOWN', result.stderr)

    def test_review_rework_reopens_task_and_revalidates_dependents(self):
        folder = self.task()
        self.cli('task-add', 'login', 'T2', '--title', 'Use login', '--acceptance', 'Integration works', '--depends', 'T1')
        self.cli('seal-plan', 'login')
        self.passing()
        self.cli('task-done', 'login', 'T1')
        self.cli('task-start', 'login', 'T2')
        self.cli('run', 'login', '--task', 'T2', '--kind', 'tests', '--', sys.executable, '-c', "print('Ran 1 test')")
        self.cli('task-done', 'login', 'T2')
        self.cli('run', 'login', '--task', 'all', '--kind', 'tests', '--', sys.executable, '-c', "print('Ran 2 tests')")
        (folder / 'review.md').write_text('# Review\nOriginal fixture changes reviewed.\n', encoding='utf-8')
        self.cli('review', 'login', '--reviewer', 'self', '--blocking', '0')
        self.cli('ready', 'login')

        original = json.loads(self.cli('status', 'login').stdout)
        self.cli('task-reopen', 'login', 'T1', '--reason', 'Review found an uncovered login edge case')
        reopened = json.loads(self.cli('status', 'login').stdout)
        tasks = {t['id']: t for t in reopened['tasks']}
        self.assertEqual(tasks['T1']['status'], 'pending')
        self.assertEqual(tasks['T2']['status'], 'needs-verification')
        self.assertEqual(tasks['T1']['history'][-1]['evidence'], original['tasks'][0]['evidence'])
        self.cli('task-start', 'login', 'T2', ok=False)
        self.cli('ready', 'login', ok=False)
        self.cli('task-start', 'login', 'T1')
        self.passing()
        self.cli('task-done', 'login', 'T1')
        self.cli('task-start', 'login', 'T2')
        self.cli('run', 'login', '--task', 'T2', '--kind', 'tests', '--', sys.executable, '-c', "print('Ran 1 test')")
        self.cli('task-done', 'login', 'T2')
        self.cli('ready', 'login', ok=False)
        self.cli('run', 'login', '--task', 'all', '--kind', 'tests', '--', sys.executable, '-c', "print('Ran 2 tests')")
        self.cli('ready', 'login', ok=False)
        self.cli('review', 'login', '--reviewer', 'self', '--blocking', '0')
        self.cli('ready', 'login')

    def test_health_reports_missing_installed_skill(self):
        self.cli('install', '--host', 'codex')
        self.cli('health', '--host', 'codex')
        (self.project / '.agents/skills/gsp-ship/SKILL.md').unlink()
        report = json.loads(self.cli('health', '--host', 'codex', ok=False).stdout)
        self.assertFalse(report['static_ok'])
        self.assertTrue(any(r['status'] == 'FAIL' and 'gsp-ship' in str(r['detail']) for r in report['checks']))

    def test_health_reports_changed_installed_runtime(self):
        self.cli('install', '--host', 'codex')
        file = self.project / '.agents/skills/gsp-workflow/scripts/gsp/runtime.py'
        with file.open('a', encoding='utf-8') as stream:
            stream.write('\n# fixture local modification\n')
        report = json.loads(self.cli('health', '--host', 'codex', ok=False).stdout)
        self.assertFalse(report['static_ok'])

    def test_new_work_invalidates_and_archives_ready_report(self):
        folder = self.completed()
        original = (folder / 'delivery.md').read_text(encoding='utf-8')
        self.cli('task-add', 'login', 'T2', '--title', 'Extra', '--acceptance', 'Extra works')
        state = json.loads(self.cli('status', 'login').stdout)
        self.assertNotEqual(state['status'], 'ready')
        self.assertNotIn('\n\nREADY：', (folder / 'delivery.md').read_text(encoding='utf-8'))
        self.assertNotEqual(json.loads((folder/'record.json').read_text(encoding='utf-8'))['stage'],'ready')
        archived = list(folder.glob('delivery-history-*.md'))
        self.assertTrue(archived)
        self.assertEqual(archived[0].read_text(encoding='utf-8'), original)
        import re
        for link in re.findall(r'\]\(([^)]+)\)', original):
            self.assertTrue((archived[0].parent / link).is_file(), link)

    def test_changed_design_invalidates_human_report_on_status(self):
        folder = self.completed()
        document(self.project, 'login', 'design').write_text('# Different design\nNow requires another user flow.\n', encoding='utf-8')
        state = json.loads(self.cli('status', 'login').stdout)
        self.assertNotEqual(state['status'], 'ready')
        self.assertNotIn('\n\nREADY：', (folder / 'delivery.md').read_text(encoding='utf-8'))
        self.assertNotEqual(json.loads((folder/'record.json').read_text(encoding='utf-8'))['stage'],'ready')
        from gsp.runtime import Project
        Project(self.project).dashboard(state)
        self.assertIn('needs-verification',(self.project/'docs/gsp/README.md').read_text(encoding='utf-8'))

    def test_blocking_review_revokes_human_ready_report(self):
        folder = self.completed()
        self.cli('review', 'login', '--reviewer', 'self', '--blocking', '1')
        self.assertNotIn('\n\nREADY：', (folder / 'delivery.md').read_text(encoding='utf-8'))

    def test_new_branch_can_start_new_task_without_scope_authority(self):
        def git(*args):
            r = run(['git', '-C', str(self.project), '-c', 'user.name=Fixture',
                                '-c', 'user.email=fixture@example.invalid', *args], capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        git('init', '-b', 'main')
        git('-c', 'core.hooksPath=', 'commit', '--allow-empty', '-m', 'baseline')
        self.task()
        git('checkout', '-b', 'next-feature')
        self.cli('new', 'second', '--title', 'Next feature')
        self.cli('resume', 'login', ok=False)  # the old task still cannot resume on another branch

    def test_installer_preflights_parent_file_conflict(self):
        file = self.project / '.agents/skills/gsp-execute'
        file.parent.mkdir(parents=True)
        file.write_text('user-owned file', encoding='utf-8')
        self.cli('install', '--host', 'codex', ok=False)
        self.assertFalse((file.parent / 'gsp-workflow').exists())
        self.assertEqual(file.read_text(encoding='utf-8'), 'user-owned file')
        file.unlink()
        self.cli('install', '--host', 'codex')

    def test_legacy_frozen_scope_does_not_block_new_task_after_branch_switch(self):
        def git(*args):
            r = run(['git', '-C', str(self.project), '-c', 'user.name=Fixture',
                                '-c', 'user.email=fixture@example.invalid', *args], capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        git('init', '-b', 'main')
        git('-c', 'core.hooksPath=', 'commit', '--allow-empty', '-m', 'baseline')
        self.task()
        folder = self.project / 'src'
        folder.mkdir()
        self.cli('scope', 'login', '--path', 'src', '--quote', 'fixture freeze')
        git('checkout', '-b', 'next-feature')
        self.cli('new', 'second', '--title', 'Next feature')
        self.cli('resume', 'login', ok=False)


if __name__ == '__main__':
    unittest.main()
