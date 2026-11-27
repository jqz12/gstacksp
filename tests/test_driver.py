from process_helpers import docfolder
from process_helpers import document
from process_helpers import session_binding, temporary_env
"""Durable controller behavior, real subprocess fixtures, no paid model calls."""
from contextlib import closing, redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from process_helpers import run

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gsp.driver import Driver
from gsp.runtime import Project, WorkflowError, main


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gsp-driver-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cli('init')
        self.cli(
            'new', 'work', '--title', 'Fixture feature', '--mode',
            'unattended', '--quote', 'SYNTHETIC TEST: run locally',
            '--intent', 'cross-session',
        )
        state = json.loads(
            (self.root / '.gsp/runs/work/state.json').read_text(encoding='utf-8'))
        # drive-* is the explicit legacy compatibility entry. Modern native
        # scheduling and independent review have their own gate tests.
        state.pop('convergence_schema', None)
        Project(self.root).write(state, 'fixture', 'legacy headless fixture',
                                 action_uid='fixture:legacy-driver')
        self.binding = session_binding(
            self.root, state['work_uuid'], thread='driver-tests-main',
            register_controller=True,
        )
        self.context = self.root / self.binding.handle_path
        self.session_env = temporary_env({
            'GSP_SESSION_CONTEXT': str(self.context),
        }).start()
        self.addCleanup(self.session_env.__exit__, None, None, None)
        self.doc = docfolder(self.root,'work')
        document(self.root, 'work', 'design').write_text('# Fixture design\nImplement add(a,b) and verify integer addition.\n', encoding='utf-8')
        self.cli('approve', 'work', '--quote', 'SYNTHETIC TEST: approve fixture')
        self.cli('scope', 'work', '--off', '--quote', 'SYNTHETIC TEST: fixture choice')
        self.checks = self.root / '.gsp/checks.json'
        self.checks.write_text(json.dumps([sys.executable, '-B', '-m', 'unittest', '-v']))
        self.command = self.root / '.gsp/host.json'
        self.command.write_text(json.dumps([sys.executable, str(ROOT / 'tests/fixtures/driver_worker.py')]))

    def cli(self, *args, ok=True):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try: code = main(['--project', str(self.root), *args])
            except SystemExit as exc: code = exc.code
        if ok:
            self.assertEqual(code, 0, out.getvalue() + err.getvalue())
            return json.loads(out.getvalue())
        self.assertNotEqual(code, 0, out.getvalue() + err.getvalue())
        return err.getvalue()

    def prepare(self, mode='ok', jobs=20, timeout=10, fixes=2, seconds=180):
        (self.root / '.gsp/fixture-mode.txt').write_text(mode)
        config_file = self.root / '.gsp/config.json'
        config = json.loads(config_file.read_text()); config['max_fix_rounds'] = fixes
        config_file.write_text(json.dumps(config))
        return self.cli('drive-init', 'work', '--checks', str(self.checks), '--host-command', str(self.command),
                        '--quote', 'SYNTHETIC TEST: use fixture command', '--max-dispatches', str(jobs),
                        '--timeout', str(timeout), '--max-seconds', str(seconds))

    def status(self):
        return self.cli('drive-status', 'work')

    def recovery_report(self, text='Fixture child exited; no code changes.'):
        report = self.root / 'recovery.md'
        report.write_text(text, encoding='utf-8')
        return report

    def driver_data(self):
        return json.loads((self.root / '.gsp/runs/work/driver/state.json').read_text(encoding='utf-8'))

    def operation_rows(self):
        path = self.root / '.gsp/orchestration/registry.sqlite'
        if not path.exists():
            return ()
        with closing(sqlite3.connect(str(path))) as db:
            return tuple(db.execute(
                'SELECT operation_uid,logical_key,phase,expected_state_digest,'
                'new_state_digest,result_ref FROM operations ORDER BY operation_uid'
            ).fetchall())

    def effect_snapshot(self, exclude_documents=()):
        state = self.root / '.gsp/runs/work/state.json'
        driver = self.root / '.gsp/runs/work/driver/state.json'
        project_events = self.root / '.gsp/runs/work/events.jsonl'
        driver_events = self.root / '.gsp/runs/work/driver/events.jsonl'
        leases = {}
        for name in ('driver.lock', 'writer.lock'):
            path = self.root / '.gsp' / name
            leases[name] = path.read_bytes() if path.exists() else None
        return {
            'state': state.read_bytes(),
            'driver': driver.read_bytes(),
            'documents': tuple(
                (path.name, path.read_bytes()) for path in sorted(self.doc.glob('*.md'))
                if path.name not in set(exclude_documents)
            ),
            'project_events': project_events.read_bytes() if project_events.exists() else b'',
            'driver_events': driver_events.read_bytes() if driver_events.exists() else b'',
            'operations': self.operation_rows(),
            'leases': leases,
        }

    def external_cli(self, *args, env):
        completed = run(
            [sys.executable, str(ROOT / 'scripts/gsp.py'), '--project', str(self.root), *args],
            cwd=self.root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=120,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f'exit={completed.returncode}\nstdout={completed.stdout}\nstderr={completed.stderr}',
        )
        return json.loads(completed.stdout)

    def interrupted_sealed_recovery(self, published):
        from gsp.hosts import identity as process_identity

        self.prepare(jobs=1)
        self.cli('drive-run', 'work')
        self.assertEqual(self.status()['reason'], 'DISPATCH_LIMIT')
        plan = document(self.root, 'work', 'plan')
        original = plan.read_bytes()
        lease = self.root / '.gsp/driver.lock'
        lease.write_text(json.dumps({
            **process_identity(999999), 'task': 'work', 'nonce': 'sealed-plan-dead-owner',
        }), encoding='utf-8')
        report = self.recovery_report('Fixture inspected sealed-plan recovery.')
        reason = 'Fixture inspected sealed-plan recovery'
        operations_before = len(self.operation_rows())
        if published:
            real_save = Driver.save

            def fail_completion(driver, event, *args, **kwargs):
                if event == 'recovered':
                    raise WorkflowError('SYNTHETIC_PUBLISHED_PENDING')
                return real_save(driver, event, *args, **kwargs)

            with patch.object(Driver, 'save', new=fail_completion):
                self.assertIn(
                    'SYNTHETIC_PUBLISHED_PENDING',
                    self.cli(
                        'drive-recover', 'work', '--evidence', str(report),
                        '--reason', reason, ok=False,
                    ),
                )
        else:
            def interrupt_before_publication(project, state, event, detail='', action_uid=None):
                raise WorkflowError('SYNTHETIC_PRE_PUBLICATION')

            with patch('gsp.driver.Project.write', new=interrupt_before_publication):
                self.assertIn(
                    'SYNTHETIC_PRE_PUBLICATION',
                    self.cli(
                        'drive-recover', 'work', '--evidence', str(report),
                        '--reason', reason, ok=False,
                    ),
                )
        self.assertIsNotNone(self.driver_data().get('pending_recovery'))
        if not published:
            self.assertTrue(lease.is_file())
        return plan, original, report, reason, operations_before

    def test_init_persists_original_limits(self):
        self.prepare(fixes=1)
        state = self.status()
        self.assertEqual(state['config']['max_fix_rounds'], 1)
        self.assertEqual(state['dispatches'], 0)
        self.assertIn('DRIVER_EXISTS', self.cli('drive-init', 'work', '--checks', str(self.checks), '--quote', 'fixture', ok=False))

    def test_host_not_reading_large_prompt_still_times_out(self):
        self.command.write_text(json.dumps([sys.executable,'-c','import time; time.sleep(20)']))
        self.prepare(timeout=.4)
        started=time.monotonic()
        with patch('gsp.driver.prompt',return_value='x'*200000):
            self.cli('drive-run','work')
        self.assertLess(time.monotonic()-started,10)
        self.assertEqual(self.status()['reason'],'EXECUTION_UNKNOWN')

    def test_codex_adapter_uses_stage_sandbox_and_structured_output(self):
        from gsp.hosts import codex_argv
        job={'phase':'review','schema':'schema.json','result':'response.json'}
        argv=codex_argv(job,'codex.exe')
        self.assertEqual(argv[argv.index('--sandbox')+1],'read-only')
        self.assertEqual(argv[argv.index('-a')+1],'never')
        self.assertIn('--output-schema',argv)
        job['phase']='execute'
        argv=codex_argv(job,'codex.exe')
        self.assertEqual(argv[argv.index('--sandbox')+1],'workspace-write')
        self.assertFalse(any('bypass' in v for v in argv))

    def test_nonfinite_time_limits_cannot_disable_stopping(self):
        for flag,value in (('--timeout','nan'),('--max-seconds','inf')):
            self.assertIn('INVALID_DRIVER_LIMIT',self.cli('drive-init','work','--checks',str(self.checks),
                '--quote','SYNTHETIC TEST: invalid limit',flag,value,ok=False))
        self.assertFalse((self.root/'.gsp/runs/work/driver').exists())

    def test_fixture_process_completes_entire_pipeline(self):
        self.prepare()
        self.cli('drive-run', 'work')
        state = self.status()
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['dispatches'], 3)  # plan, actual implementation, review
        self.assertEqual(self.cli('status', 'work')['status'], 'ready')
        self.assertEqual(self.cli('status', 'work')['review']['reviewer'], 'self')
        self.cli('resume', 'work')
        self.assertIn('completed', (self.doc / 'driver-report.md').read_text(encoding='utf-8'))

    def test_failed_check_triggers_persisted_fix_round(self):
        self.prepare('fail-once')
        self.cli('drive-run', 'work')
        state = self.status()
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['fix_rounds']['task:T1'], 1)

    def test_fix_limit_is_consumed_not_only_documented(self):
        self.prepare('always-fail', fixes=1)
        self.cli('drive-run', 'work')
        state = self.status()
        self.assertEqual(state['status'], 'blocked',state)
        self.assertEqual(state['reason'], 'FIX_LIMIT')
        self.assertEqual(state['fix_rounds']['task:T1'], 1)
        before = state['dispatches']
        self.cli('drive-run', 'work')
        self.assertEqual(self.status()['dispatches'], before)

    def test_review_findings_drive_fix_and_fresh_review(self):
        self.prepare('review-fail-once')
        self.cli('drive-run', 'work')
        state = self.status()
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['fix_rounds']['review'], 1)

    def test_budget_survives_restarting_loop(self):
        self.prepare(jobs=1)
        self.cli('drive-run', 'work')
        before = self.status()
        self.assertEqual(before['reason'], 'DISPATCH_LIMIT')
        self.cli('drive-run', 'work')
        self.assertEqual(self.status()['dispatches'], 1)

    def test_technical_recommendation_does_not_wait_for_user(self):
        self.prepare('technical')
        self.cli('drive-run', 'work')
        self.assertEqual(self.status()['status'], 'completed')
        self.assertIn('Use the simple sum', (self.doc / 'decisions.md').read_text(encoding='utf-8'))

    def test_gate_answer_is_bound_and_single_use(self):
        self.prepare('question')
        self.cli('drive-run', 'work')
        state = self.status()
        self.assertEqual(state['status'], 'waiting')
        gate = state['gate']
        self.assertIn('GATE_MISMATCH', self.cli('drive-answer', 'work', '--gate', 'wrong', '--answer', 'Use sum', '--quote', 'fixture', ok=False))
        self.cli('drive-answer', 'work', '--gate', gate, '--answer', 'Use sum', '--quote', 'SYNTHETIC TEST: choose sum')
        self.cli('drive-answer', 'work', '--gate', gate, '--answer', 'Again', '--quote', 'fixture', ok=False)
        self.cli('drive-run', 'work')
        self.assertEqual(self.status()['status'], 'completed')

    def test_pending_job_is_not_redispatched(self):
        self.prepare()
        first = self.cli('drive-next', 'work')
        second = self.cli('drive-next', 'work')
        self.assertEqual(first['job']['id'], second['job']['id'])
        self.assertEqual(second['action'], 'awaiting_result')
        self.assertEqual(self.status()['dispatches'], 1)

    def test_changed_design_stops_before_host_dispatch(self):
        self.prepare()
        document(self.root, 'work', 'design').write_text('# Another design\nChange the feature.\n', encoding='utf-8')
        self.cli('drive-run', 'work')
        self.assertEqual(self.status()['dispatches'], 0)
        self.assertEqual(self.status()['status'], 'waiting')

    def test_completed_run_cannot_hide_later_input_changes(self):
        self.prepare(); self.cli('drive-run', 'work')
        (self.root / 'app.py').write_text('def add(a,b): return 0\n', encoding='utf-8')
        state = self.status()
        self.assertEqual(state['status'], 'waiting')
        self.assertEqual(state['reason'], 'STATE_CHANGED')
        self.assertEqual(state['dispatches'], 3)

    def test_expired_deadline_does_not_reset_on_restart(self):
        self.prepare()
        file = self.root / '.gsp/runs/work/driver/state.json'
        data = json.loads(file.read_text(encoding='utf-8')); data['deadline'] = time.time()-1
        file.write_text(json.dumps(data), encoding='utf-8')
        self.cli('drive-run', 'work')
        self.assertEqual(self.status()['reason'], 'DEADLINE_EXCEEDED')
        self.assertEqual(self.status()['dispatches'], 0)

    def test_host_claim_does_not_bypass_real_checks(self):
        self.prepare('always-fail', fixes=0)
        self.cli('drive-run', 'work')
        self.assertEqual(self.cli('status', 'work')['tasks'][0]['status'], 'running')
        self.assertNotEqual(self.cli('status', 'work')['status'], 'ready')

    def test_invalid_host_result_is_unknown_not_pass(self):
        self.prepare('invalid')
        self.cli('drive-run', 'work')
        state = self.status()
        self.assertEqual(state['status'], 'waiting')
        self.assertEqual(state['reason'], 'EXECUTION_UNKNOWN')
        self.assertEqual(state['dispatches'], 1)

    def test_control_metadata_change_is_not_accepted(self):
        self.prepare('tamper')
        self.cli('drive-run', 'work')
        self.assertEqual(self.status()['reason'], 'CONTROL_CHANGED')

    def test_timeout_persists_unknown_execution(self):
        self.prepare('slow', timeout=.3)
        self.cli('drive-run', 'work')
        state = self.status()
        self.assertEqual(state['reason'], 'EXECUTION_UNKNOWN')
        self.assertIsNotNone(state['pending'])

    def test_unknown_cannot_be_answered_as_ordinary_gate(self):
        self.prepare('invalid')
        self.cli('drive-run', 'work')
        state = self.status()
        self.assertIn('RECOVERY_REQUIRED', self.cli('drive-answer', 'work', '--gate', state['gate'], '--answer', 'continue', '--quote', 'fixture', ok=False))

    def test_recovery_keeps_consumed_dispatch_budget(self):
        self.prepare('invalid', jobs=1)
        self.cli('drive-run', 'work')
        report = self.recovery_report('Fixture child exited; result malformed; no code changes.')
        self.cli('drive-recover', 'work', '--evidence', str(report), '--reason', 'Fixture inspected actual exit')
        self.cli('drive-run', 'work')
        self.assertEqual(self.status()['dispatches'], 1)
        self.assertEqual(self.status()['reason'], 'DISPATCH_LIMIT')

    def test_recovery_same_reason_incidents_and_exact_repeats_are_distinct(self):
        self.prepare('invalid', jobs=1)
        self.cli('drive-run', 'work')
        original = self.status()
        operations_before = len(self.operation_rows())
        report = self.recovery_report()
        reason = 'Fixture inspected the same failed attempt'

        self.cli('drive-recover', 'work', '--evidence', str(report), '--reason', reason)
        first = self.status()
        first_workflow = json.loads(
            (self.root / '.gsp/runs/work/state.json').read_text(encoding='utf-8')
        )
        self.assertEqual(first_workflow['verification_epoch'], 1)
        self.assertEqual(len(first['recoveries']), 1)
        self.assertEqual(first['dispatches'], original['dispatches'])
        self.assertEqual(first['deadline'], original['deadline'])

        completed = self.effect_snapshot()
        repeated = self.cli('drive-recover', 'work', '--evidence', str(report), '--reason', reason)
        self.assertEqual(repeated['action'], 'no-change')
        self.assertEqual(self.effect_snapshot(), completed)

        self.cli('drive-run', 'work')
        second_incident = self.status()
        self.assertEqual(second_incident['reason'], 'DISPATCH_LIMIT')
        self.assertEqual(second_incident['dispatches'], original['dispatches'])
        self.assertEqual(second_incident['deadline'], original['deadline'])

        self.cli('drive-recover', 'work', '--evidence', str(report), '--reason', reason)
        recovered = self.status()
        workflow = json.loads((self.root / '.gsp/runs/work/state.json').read_text(encoding='utf-8'))
        self.assertEqual(workflow['verification_epoch'], 2)
        self.assertEqual(len(recovered['recoveries']), 2)
        self.assertNotEqual(
            recovered['recoveries'][0]['incident_uid'],
            recovered['recoveries'][1]['incident_uid'],
        )
        self.assertEqual(len(self.operation_rows()), operations_before + 2)
        self.assertEqual(recovered['dispatches'], original['dispatches'])
        self.assertEqual(recovered['deadline'], original['deadline'])

    def test_external_cli_smoke_distinct_recovery_incidents_are_isolated(self):
        (self.root / '.gsp/fixture-mode.txt').write_text('invalid', encoding='utf-8')
        config_file = self.root / '.gsp/config.json'
        config = json.loads(config_file.read_text(encoding='utf-8'))
        config['max_fix_rounds'] = 2
        config_file.write_text(json.dumps(config), encoding='utf-8')
        home = self.root / 'isolated-home'
        codex_home = home / '.codex'
        home.mkdir()
        codex_home.mkdir()
        env = dict(os.environ)
        env.update({'HOME': str(home), 'USERPROFILE': str(home), 'CODEX_HOME': str(codex_home)})

        self.external_cli(
            'drive-init', 'work', '--checks', str(self.checks),
            '--host-command', str(self.command), '--quote',
            'SYNTHETIC TEST: external CLI uses fixture command',
            '--max-dispatches', '1', '--timeout', '10', '--max-seconds', '180',
            env=env,
        )
        self.external_cli('drive-run', 'work', env=env)
        unknown = self.external_cli('drive-status', 'work', env=env)
        self.assertEqual(unknown['reason'], 'EXECUTION_UNKNOWN')
        operations_before = len(self.operation_rows())
        report = self.recovery_report()
        reason = 'External fixture inspected actual exit'

        self.external_cli(
            'drive-recover', 'work', '--evidence', str(report), '--reason', reason, env=env
        )
        first_completed = self.effect_snapshot()
        repeated = self.external_cli(
            'drive-recover', 'work', '--evidence', str(report), '--reason', reason, env=env
        )
        self.assertEqual(repeated['action'], 'no-change')
        self.assertEqual(self.effect_snapshot(), first_completed)

        self.external_cli('drive-run', 'work', env=env)
        limited = self.external_cli('drive-status', 'work', env=env)
        self.assertEqual(limited['reason'], 'DISPATCH_LIMIT')
        self.external_cli(
            'drive-recover', 'work', '--evidence', str(report), '--reason', reason, env=env
        )
        recovered = self.external_cli('drive-status', 'work', env=env)
        workflow = json.loads((self.root / '.gsp/runs/work/state.json').read_text(encoding='utf-8'))
        self.assertEqual(workflow['verification_epoch'], 2)
        self.assertEqual(len(recovered['recoveries']), 2)
        self.assertEqual(len(self.operation_rows()), operations_before + 2)
        self.assertEqual(recovered['dispatches'], 1)

    def test_recovery_retry_changed_request_has_zero_new_effects(self):
        self.prepare('invalid', jobs=1)
        self.cli('drive-run', 'work')
        operations_before = len(self.operation_rows())
        report = self.recovery_report()
        reason = 'Fixture inspected pending recovery'

        def interrupt_before_publication(project, state, event, detail='', action_uid=None):
            raise WorkflowError('SYNTHETIC_BEFORE_PUBLICATION')

        with patch('gsp.driver.Project.write', new=interrupt_before_publication):
            self.assertIn(
                'SYNTHETIC_BEFORE_PUBLICATION',
                self.cli(
                    'drive-recover', 'work', '--evidence', str(report),
                    '--reason', reason, ok=False,
                ),
            )
        pending = self.driver_data().get('pending_recovery')
        self.assertIsNotNone(pending)
        before = self.effect_snapshot()

        report.write_text('Changed investigation bytes.', encoding='utf-8')
        self.assertIn(
            'RECOVERY_INPUT_CHANGED',
            self.cli(
                'drive-recover', 'work', '--evidence', str(report),
                '--reason', reason, ok=False,
            ),
        )
        self.assertEqual(self.effect_snapshot(), before)

        self.recovery_report()
        self.assertIn(
            'RECOVERY_INPUT_CHANGED',
            self.cli(
                'drive-recover', 'work', '--evidence', str(report),
                '--reason', reason + ' changed', ok=False,
            ),
        )
        self.assertEqual(self.effect_snapshot(), before)

        self.cli('drive-recover', 'work', '--evidence', str(report), '--reason', reason)
        workflow = json.loads((self.root / '.gsp/runs/work/state.json').read_text(encoding='utf-8'))
        self.assertEqual(workflow['verification_epoch'], 1)
        self.assertEqual(len(self.driver_data()['recoveries']), 1)
        self.assertEqual(len(self.operation_rows()), operations_before + 1)

    def test_recovery_crash_before_workflow_publication_reuses_frozen_intent(self):
        from gsp.hosts import identity as process_identity

        self.prepare('invalid', jobs=1)
        self.cli('drive-run', 'work')
        original = self.status()
        operations_before = len(self.operation_rows())
        report = self.recovery_report()
        reason = 'Fixture inspected before-publication crash'

        def interrupt_before_publication(project, state, event, detail='', action_uid=None):
            raise WorkflowError('SYNTHETIC_BEFORE_PUBLICATION')

        with patch('gsp.driver.Project.write', new=interrupt_before_publication):
            self.assertIn(
                'SYNTHETIC_BEFORE_PUBLICATION',
                self.cli(
                    'drive-recover', 'work', '--evidence', str(report),
                    '--reason', reason, ok=False,
                ),
            )
        self.assertIsNotNone(self.driver_data().get('pending_recovery'))

        lease = self.root / '.gsp/driver.lock'
        lease.write_text(json.dumps({
            **process_identity(os.getpid()), 'task': 'work', 'nonce': 'synthetic-live-owner',
        }), encoding='utf-8')
        try:
            before = self.effect_snapshot()
            self.assertIn(
                'DRIVER_BUSY',
                self.cli(
                    'drive-recover', 'work', '--evidence', str(report),
                    '--reason', reason, ok=False,
                ),
            )
            self.assertEqual(self.effect_snapshot(), before)
        finally:
            lease.unlink(missing_ok=True)

        self.cli('drive-recover', 'work', '--evidence', str(report), '--reason', reason)
        recovered = self.status()
        workflow = json.loads((self.root / '.gsp/runs/work/state.json').read_text(encoding='utf-8'))
        self.assertEqual(workflow['verification_epoch'], 1)
        self.assertEqual(len(recovered['recoveries']), 1)
        self.assertEqual(len(self.operation_rows()), operations_before + 1)
        self.assertEqual(recovered['dispatches'], original['dispatches'])
        self.assertEqual(recovered['deadline'], original['deadline'])

    def test_recovery_crash_after_file_publication_before_db_commit_reuses_operation(self):
        from gsp.identity_store import IdentityStore, IdentityStoreError

        self.prepare('invalid', jobs=1)
        self.cli('drive-run', 'work')
        original = self.status()
        operations_before = len(self.operation_rows())
        report = self.recovery_report()
        reason = 'Fixture inspected file-written crash'
        real_finish = IdentityStore.finish_operation
        failed = False

        def fail_once(self, *args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise IdentityStoreError('SYNTHETIC_FILE_WRITTEN')
            return real_finish(self, *args, **kwargs)

        with patch.object(IdentityStore, 'finish_operation', new=fail_once):
            self.assertIn(
                'SYNTHETIC_FILE_WRITTEN',
                self.cli(
                    'drive-recover', 'work', '--evidence', str(report),
                    '--reason', reason, ok=False,
                ),
            )
        self.assertIsNotNone(self.driver_data().get('pending_recovery'))
        self.assertEqual(len(self.operation_rows()), operations_before + 1)

        self.cli('drive-recover', 'work', '--evidence', str(report), '--reason', reason)
        recovered = self.status()
        workflow = json.loads((self.root / '.gsp/runs/work/state.json').read_text(encoding='utf-8'))
        self.assertEqual(workflow['verification_epoch'], 1)
        self.assertEqual(len(recovered['recoveries']), 1)
        self.assertEqual(len(self.operation_rows()), operations_before + 1)
        self.assertEqual(recovered['dispatches'], original['dispatches'])
        self.assertEqual(recovered['deadline'], original['deadline'])

    def test_recovery_crash_after_workflow_commit_before_driver_publication_reuses_operation(self):
        self.prepare('invalid', jobs=1)
        self.cli('drive-run', 'work')
        original = self.status()
        operations_before = len(self.operation_rows())
        report = self.recovery_report()
        reason = 'Fixture inspected workflow-committed crash'
        real_save = Driver.save

        def fail_completion(driver, event, *args, **kwargs):
            if event == 'recovered':
                raise WorkflowError('SYNTHETIC_DRIVER_PUBLICATION')
            return real_save(driver, event, *args, **kwargs)

        with patch.object(Driver, 'save', new=fail_completion):
            self.assertIn(
                'SYNTHETIC_DRIVER_PUBLICATION',
                self.cli(
                    'drive-recover', 'work', '--evidence', str(report),
                    '--reason', reason, ok=False,
                ),
            )
        self.assertIsNotNone(self.driver_data().get('pending_recovery'))
        self.assertEqual(len(self.operation_rows()), operations_before + 1)

        self.cli('drive-recover', 'work', '--evidence', str(report), '--reason', reason)
        recovered = self.status()
        workflow = json.loads((self.root / '.gsp/runs/work/state.json').read_text(encoding='utf-8'))
        self.assertEqual(workflow['verification_epoch'], 1)
        self.assertEqual(len(recovered['recoveries']), 1)
        self.assertEqual(len(self.operation_rows()), operations_before + 1)
        self.assertEqual(recovered['dispatches'], original['dispatches'])
        self.assertEqual(recovered['deadline'], original['deadline'])

    def test_recovery_crash_after_driver_publication_before_response_is_noop(self):
        self.prepare('invalid', jobs=1)
        self.cli('drive-run', 'work')
        original = self.status()
        operations_before = len(self.operation_rows())
        report = self.recovery_report()
        reason = 'Fixture inspected response-loss crash'
        real_recover = Driver.recover

        def lose_response(driver, args):
            real_recover(driver, args)
            raise WorkflowError('SYNTHETIC_RESPONSE_LOST')

        with patch.object(Driver, 'recover', new=lose_response):
            self.assertIn(
                'SYNTHETIC_RESPONSE_LOST',
                self.cli(
                    'drive-recover', 'work', '--evidence', str(report),
                    '--reason', reason, ok=False,
                ),
            )
        completed = self.effect_snapshot()

        repeated = self.cli(
            'drive-recover', 'work', '--evidence', str(report), '--reason', reason,
        )
        self.assertEqual(repeated['action'], 'no-change')
        self.assertEqual(self.effect_snapshot(), completed)
        recovered = self.status()
        workflow = json.loads((self.root / '.gsp/runs/work/state.json').read_text(encoding='utf-8'))
        self.assertEqual(workflow['verification_epoch'], 1)
        self.assertEqual(len(recovered['recoveries']), 1)
        self.assertEqual(len(self.operation_rows()), operations_before + 1)
        self.assertEqual(recovered['dispatches'], original['dispatches'])
        self.assertEqual(recovered['deadline'], original['deadline'])

    def test_pending_recovery_rejects_plan_drift_before_publication_without_effects(self):
        plan, original, report, reason, operations_before = self.interrupted_sealed_recovery(False)
        before = self.effect_snapshot(exclude_documents={'plan.md'})
        drifted = original + b'\n# physical sealed-plan drift\n'
        plan.write_bytes(drifted)

        self.assertIn(
            'RECOVERY_PLAN_CHANGED',
            self.cli(
                'drive-recover', 'work', '--evidence', str(report),
                '--reason', reason, ok=False,
            ),
        )
        self.assertEqual(plan.read_bytes(), drifted)
        self.assertEqual(self.effect_snapshot(exclude_documents={'plan.md'}), before)
        self.assertIsNotNone(self.driver_data().get('pending_recovery'))
        self.assertTrue((self.root / '.gsp/driver.lock').is_file())

        plan.write_bytes(original)
        self.cli('drive-recover', 'work', '--evidence', str(report), '--reason', reason)
        workflow = json.loads((self.root / '.gsp/runs/work/state.json').read_text(encoding='utf-8'))
        recovered = self.status()
        self.assertEqual(workflow['verification_epoch'], 1)
        self.assertEqual(len(recovered['recoveries']), 1)
        self.assertEqual(len(self.operation_rows()), operations_before + 1)
        self.assertFalse((self.root / '.gsp/driver.lock').exists())

    def test_pending_recovery_rejects_plan_drift_after_workflow_publication_without_effects(self):
        plan, original, report, reason, operations_before = self.interrupted_sealed_recovery(True)
        before = self.effect_snapshot(exclude_documents={'plan.md'})
        drifted = original + b'\n# published physical plan drift\n'
        plan.write_bytes(drifted)

        self.assertIn(
            'RECOVERY_PLAN_CHANGED',
            self.cli(
                'drive-recover', 'work', '--evidence', str(report),
                '--reason', reason, ok=False,
            ),
        )
        self.assertEqual(plan.read_bytes(), drifted)
        self.assertEqual(self.effect_snapshot(exclude_documents={'plan.md'}), before)
        self.assertIsNotNone(self.driver_data().get('pending_recovery'))

        plan.write_bytes(original)
        self.cli('drive-recover', 'work', '--evidence', str(report), '--reason', reason)
        workflow = json.loads((self.root / '.gsp/runs/work/state.json').read_text(encoding='utf-8'))
        recovered = self.status()
        self.assertEqual(workflow['verification_epoch'], 1)
        self.assertEqual(len(recovered['recoveries']), 1)
        self.assertEqual(len(self.operation_rows()), operations_before + 1)


if __name__ == '__main__':
    unittest.main()
