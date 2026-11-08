"""Durable controller behavior, real subprocess fixtures, no paid model calls."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gsp.runtime import main


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gsp-driver-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cli('init')
        self.cli('new', 'work', '--title', 'Fixture feature', '--mode', 'unattended', '--quote', 'SYNTHETIC TEST: run locally')
        self.doc = self.root / 'docs/gsp/tasks/work'
        (self.doc / 'design.md').write_text('# Fixture design\nImplement add(a,b) and verify integer addition.\n', encoding='utf-8')
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
        self.assertEqual(state['status'], 'blocked')
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
        (self.doc / 'design.md').write_text('# Another design\nChange the feature.\n', encoding='utf-8')
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
        report = self.root / 'recovery.md'; report.write_text('Fixture child exited; result malformed; no code changes.')
        self.cli('drive-recover', 'work', '--evidence', str(report), '--reason', 'Fixture inspected actual exit')
        self.cli('drive-run', 'work')
        self.assertEqual(self.status()['dispatches'], 1)
        self.assertEqual(self.status()['reason'], 'DISPATCH_LIMIT')


if __name__ == '__main__':
    unittest.main()
