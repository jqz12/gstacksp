import importlib.util
import json
import os
from pathlib import Path
import subprocess
from process_helpers import run, popen
import sys
import tempfile
import time
import signal
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts' / 'gsp.py'


class WorkflowTests(unittest.TestCase):
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
        self.task()
        result=run(['git','-C',str(self.project),'init','-b','main'],capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('BRANCH_MISMATCH',self.cli('status','login',ok=False).stderr)
        self.cli('decision','login','--text','Do not silently adopt','--source','agent',ok=False)
        self.cli('observe-head','login','--reason','SYNTHETIC checked newly initialized repository at the same physical project root')
        state=json.loads(self.cli('status','login').stdout)
        self.assertEqual(state['git']['root'],str(self.project.resolve()))
        self.assertIsNone(state['head_reconciliation']['previous']['root'])

    def test_first_parent_git_repository_cannot_be_adopted_as_project_root(self):
        self.task()
        result=run(['git','-C',self.tmp.name,'init','-b','main'],capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('BRANCH_MISMATCH',self.cli('observe-head','login','--reason','SYNTHETIC parent repository mismatch',ok=False).stderr)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-test-')
        self.project = Path(self.tmp.name) / 'project with spaces'
        self.project.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, ok=True):
        r = run([sys.executable, str(CLI), '--project', str(self.project), *args],
                           text=True, encoding='utf-8', capture_output=True)
        if ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        else:
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def task(self):
        self.cli('init')
        self.cli('new', 'login', '--title', 'Login')
        folder = self.project / 'docs/gsp/tasks/login'
        (folder / 'design.md').write_text('# Design\nLogin returns a session.\n', encoding='utf-8')
        self.cli('approve', 'login', '--quote', 'I approve this design')
        self.cli('task-add', 'login', 'T1', '--title', 'Implement login', '--acceptance', 'Unit tests pass')
        (folder / 'plan.md').write_text('# Plan\nT1: implement and test login.\n', encoding='utf-8')
        self.cli('seal-plan', 'login')
        self.cli('scope', 'login', '--off', '--quote', 'No freeze for this task')
        self.cli('task-start', 'login', 'T1')
        return folder

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
        (folder / 'design.md').write_text('Changed requirement', encoding='utf-8')
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
        (folder / 'plan.md').write_text('new plan', encoding='utf-8')
        self.passing_failure = self.cli('task-start', 'login', 'T1', ok=False)

    def test_scope_paths_and_shell_monitor(self):
        self.task()
        (self.project / 'src').mkdir()
        self.cli('scope', 'login', '--path', 'src', '--quote', 'Only modify src')
        self.cli('check-path', 'login', 'src/new.py')
        self.cli('check-path', 'login', 'src-old/new.py', ok=False)
        self.cli('check-path', 'login', '../outside.py', ok=False)
        self.cli('run', 'login', '--task', 'T1', '--kind', 'tests', '--', sys.executable,
                 '-c', "from pathlib import Path; Path('outside.py').write_text('x'); print('Ran 1 test')", ok=False)

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

    def test_new_task_cannot_bypass_frozen_task(self):
        self.task()
        (self.project / 'src').mkdir()
        self.cli('scope', 'login', '--path', 'src', '--quote', 'Only src')
        self.cli('new', 'second', '--title', 'Second', ok=False)

    def test_hook_finds_root_from_child_cwd(self):
        self.task()
        (self.project / 'src').mkdir()
        self.cli('scope', 'login', '--path', 'src', '--quote', 'Only src')
        payload = {'cwd': str(self.project / 'src'), 'tool_name': 'Edit',
                   'tool_input': {'file_path': str(self.project / 'outside.py')}}
        r = run([sys.executable, str(CLI), '--project', str(self.project), 'hook'],
                           input=json.dumps(payload), text=True, encoding='utf-8', capture_output=True)
        self.assertEqual(json.loads(r.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_hook_accepts_utf8_chinese_scope(self):
        self.task()
        scope = self.project / '源码'
        scope.mkdir()
        self.cli('scope', 'login', '--path', '源码', '--quote', 'Only this source directory')
        payload = {'cwd': str(self.project), 'tool_name': 'Edit',
                   'tool_input': {'file_path': str(scope / '登录.py')}}
        r = run([sys.executable, str(CLI), '--project', str(self.project), 'hook'],
                           input=json.dumps(payload, ensure_ascii=False), text=True,
                           encoding='utf-8', capture_output=True)
        self.assertEqual(json.loads(r.stdout), {})

    def test_new_contract_rejects_old_receipt(self):
        folder = self.task()
        self.passing()
        self.cli('task-done', 'login', 'T1')
        self.cli('run', 'login', '--task', 'all', '--kind', 'tests', '--', sys.executable, '-c', "print('Ran 1 test')")
        (folder / 'review.md').write_text('# Review\nOriginal design reviewed without blockers.\n', encoding='utf-8')
        self.cli('review', 'login', '--reviewer', 'self', '--blocking', '0')
        self.cli('ready', 'login')
        (folder / 'design.md').write_text('# New design\nNow requires expiration too.\n', encoding='utf-8')
        self.cli('approve', 'login', '--quote', 'Approve expiration')
        self.cli('seal-plan', 'login')
        (folder / 'review.md').write_text('# Review\nChecked revised design without blockers.\n', encoding='utf-8')
        self.cli('review', 'login', '--reviewer', 'self', '--blocking', '0')
        r = self.cli('ready', 'login', ok=False)
        self.assertIn('FINAL_REGRESSION_REQUIRED', r.stderr)

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
        state_file.write_text(json.dumps(state), encoding='utf-8')
        self.cli('resume', 'login', ok=False)
        self.cli('run', 'login', '--task', 'T1', '--kind', 'tests', '--', sys.executable, '-c',
                 "print('Ran 1 test')", ok=False)
        self.cli('recover-run', 'login', '--evidence', 'missing.md', '--outcome', 'failed', ok=False)
        (folder / 'recovery.md').write_text('Fixture investigation: process stopped; no result available.', encoding='utf-8')
        self.cli('recover-run', 'login', '--evidence', 'docs/gsp/tasks/login/recovery.md', '--outcome', 'failed')
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
        file.write_text(json.dumps(state), encoding='utf-8')
        (folder / 'recovery.md').write_text('Fixture process failed, no new passing result.', encoding='utf-8')
        self.cli('recover-run', 'login', '--evidence', 'docs/gsp/tasks/login/recovery.md', '--outcome', 'failed')
        self.cli('ready', 'login', ok=False)

    def test_ready_task_freeze_still_requires_release(self):
        self.cli('init')
        self.cli('new', 'other', '--title', 'Other')
        folder = self.task()
        self.passing()
        self.cli('task-done', 'login', 'T1')
        self.cli('run', 'login', '--task', 'all', '--kind', 'tests', '--', sys.executable, '-c', "print('Ran 1 test')")
        (folder / 'review.md').write_text('# Review\nChecked all changes without blockers.\n', encoding='utf-8')
        self.cli('review', 'login', '--reviewer', 'self', '--blocking', '0')
        self.cli('ready', 'login')
        self.cli('scope', 'login', '--path', '.', '--quote', 'Freeze project')
        self.cli('scope', 'other', '--off', '--quote', 'No freeze for other', ok=False)

    def test_real_interruption_preserves_unknown_execution(self):
        folder = self.task()
        program = "from pathlib import Path; import os,time; Path('.gsp/child.pid').write_text(str(os.getpid())); time.sleep(20)"
        runner = popen([sys.executable, str(CLI), '--project', str(self.project), 'run', 'login',
                                   '--task', 'T1', '--kind', 'tests', '--', sys.executable, '-c', program],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
        self.assertTrue(lock.exists())
        self.cli('resume', 'login', ok=False)
        # The fixture runner is verified dead above; remove only its exact local lock.
        lock.unlink()
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
        archived = list(folder.glob('delivery-history-*.md'))
        self.assertTrue(archived)
        self.assertEqual(archived[0].read_text(encoding='utf-8'), original)
        import re
        for link in re.findall(r'\]\(([^)]+)\)', original):
            self.assertTrue((archived[0].parent / link).is_file(), link)

    def test_changed_design_invalidates_human_report_on_status(self):
        folder = self.completed()
        (folder / 'design.md').write_text('# Different design\nNow requires another user flow.\n', encoding='utf-8')
        state = json.loads(self.cli('status', 'login').stdout)
        self.assertNotEqual(state['status'], 'ready')
        self.assertNotIn('\n\nREADY：', (folder / 'delivery.md').read_text(encoding='utf-8'))

    def test_blocking_review_revokes_human_ready_report(self):
        folder = self.completed()
        self.cli('review', 'login', '--reviewer', 'self', '--blocking', '1')
        self.assertNotIn('\n\nREADY：', (folder / 'delivery.md').read_text(encoding='utf-8'))

    def test_new_branch_can_start_new_unfrozen_task(self):
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
        file = self.project / '.agents/skills/gsp-health'
        file.parent.mkdir(parents=True)
        file.write_text('user-owned file', encoding='utf-8')
        self.cli('install', '--host', 'codex', ok=False)
        self.assertFalse((file.parent / 'gsp-workflow').exists())
        self.assertEqual(file.read_text(encoding='utf-8'), 'user-owned file')
        file.unlink()
        self.cli('install', '--host', 'codex')

    def test_frozen_scope_can_be_released_after_branch_switch(self):
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
        self.cli('new', 'second', '--title', 'Next feature', ok=False)
        self.cli('scope', 'login', '--off', '--quote', 'fixture release')
        self.cli('new', 'second', '--title', 'Next feature')
        self.cli('resume', 'login', ok=False)


if __name__ == '__main__':
    unittest.main()
