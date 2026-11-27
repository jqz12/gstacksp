"""Streaming command execution: early logs, heartbeats, quotas and failures."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gsp.command_execution import execute_command


class CommandExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-command-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / ('a' * 32)
        self.folder.mkdir()

    def run_script(self, script, timeout=10, metadata=None, **kwargs):
        return execute_command(
            [sys.executable, '-c', script], self.root, dict(os.environ),
            timeout, self.folder, metadata or {'task': 'T01'}, **kwargs)

    def read(self, name):
        path = self.folder / name
        return path.read_text(encoding='utf-8', errors='replace') if path.is_file() else ''

    def test_output_is_visible_before_the_child_exits(self):
        script = ("import sys,time\n"
                  "print('stream-visible', flush=True)\n"
                  "time.sleep(1.2)\n"
                  "print('stream-done', flush=True)\n")
        box = {}
        worker = threading.Thread(target=lambda: box.update(result=self.run_script(script)))
        worker.start()
        try:
            deadline = time.monotonic() + 4
            seen = False
            while time.monotonic() < deadline:
                if 'stream-visible' in self.read('stdout.txt'):
                    seen = True
                    break
                time.sleep(.05)
            self.assertTrue(seen, 'stdout was not visible while the child was still running')
            self.assertTrue(worker.is_alive())
        finally:
            worker.join(timeout=10)
        self.assertIn('stream-done', self.read('stdout.txt'))
        self.assertEqual(box['result']['returncode'], 0)

    def test_quiet_child_still_emits_heartbeat(self):
        script = "import time\ntime.sleep(0.8)\n"
        box = {}
        with patch('gsp.command_execution.HEARTBEAT_SECONDS', 0.15):
            worker = threading.Thread(target=lambda: box.update(result=self.run_script(script)))
            worker.start()
            try:
                deadline = time.monotonic() + 3
                first = None
                second = None
                while time.monotonic() < deadline:
                    path = self.folder / 'command-progress.json'
                    if path.is_file():
                        progress = json.loads(path.read_text(encoding='utf-8'))
                        if first is None:
                            first = progress.get('heartbeat_at')
                        elif progress.get('heartbeat_at') != first:
                            second = progress.get('heartbeat_at')
                            break
                    time.sleep(.05)
                self.assertIsNotNone(second, 'quiet child did not update heartbeat')
            finally:
                worker.join(timeout=10)
        self.assertEqual(box['result']['status'], 'completed')

    def test_timeout_is_not_pass(self):
        result = self.run_script("import time\ntime.sleep(5)\n", timeout=.4)
        self.assertEqual(result['status'], 'timeout')
        self.assertEqual(result['interruption'], 'timeout')
        self.assertEqual(result['launch_error'], 'TimeoutExpired')
        self.assertNotEqual(result['returncode'], 0)
        self.assertTrue((self.folder / 'command-result.json').is_file())

    def test_cancel_check_terminates_without_waiting_for_timeout(self):
        calls = {'count': 0}

        def cancel_check():
            calls['count'] += 1
            return ('STOP_REQUESTED fixture stop' if calls['count'] >= 2 else None)

        started = time.monotonic()
        result = self.run_script(
            "import time\ntime.sleep(10)\n", timeout=9,
            cancel_check=cancel_check,
        )
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['interruption'], 'cancelled')
        self.assertEqual(result['cancel_reason'], 'STOP_REQUESTED fixture stop')
        self.assertFalse(result['process_alive'])
        self.assertLess(time.monotonic() - started, 5)

    def test_launch_failure_is_not_pass(self):
        result = execute_command(['definitely-not-a-real-executable-xyz'], self.root,
                                 dict(os.environ), 5, self.folder, {'task': 'T01'})
        self.assertEqual(result['status'], 'launch-failed')
        self.assertTrue(result['launch_error'])
        self.assertIsNone(result['returncode'])
        self.assertTrue((self.folder / 'stdout.txt').is_file())
        self.assertTrue((self.folder / 'stderr.txt').is_file())

    def test_nonzero_exit_is_failed_not_completed(self):
        result = self.run_script("import sys\nsys.exit(3)\n")
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['returncode'], 3)

    def test_startup_hook_failure_terminates_the_launched_child(self):
        script = "import time\ntime.sleep(5)\n"

        def fail_attach(process):
            raise RuntimeError(f'attach failed for {process.pid}')

        result = self.run_script(script, timeout=10, on_started=fail_attach)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['launch_error'], 'RuntimeError')
        self.assertFalse(result['process_alive'])
        self.assertLess(result['duration_s'], 4.5)

    def test_keyboard_interrupt_is_not_pass(self):
        with patch('gsp.command_execution.subprocess.Popen', side_effect=KeyboardInterrupt):
            result = self.run_script("print('never')")
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['interruption'], 'keyboard-interrupt')
        self.assertIsNone(result['returncode'])

    def test_secret_split_across_chunks_is_redacted(self):
        script = ("import sys,time\n"
                  "sys.stdout.write('api_')\n"
                  "sys.stdout.flush()\n"
                  "time.sleep(.2)\n"
                  "sys.stdout.write('key=SUPERSECRETVALUE\\n')\n"
                  "sys.stdout.flush()\n")
        result = self.run_script(script)
        text = self.read('stdout.txt')
        self.assertIn('<REDACTED>', text)
        self.assertNotIn('SUPERSECRETVALUE', text)
        self.assertEqual(result['status'], 'completed')

    def test_long_line_is_not_lost(self):
        script = ("import sys\n"
                  "sys.stdout.write('x' * 100000 + '\\n')\n"
                  "sys.stdout.flush()\n")
        result = self.run_script(script)
        self.assertFalse(result['log_integrity']['stdout']['truncated'])
        self.assertEqual(len(self.read('stdout.txt').strip()), 100000)
        self.assertEqual(result['status'], 'completed')

    def test_stdout_and_stderr_are_drained_together(self):
        script = ("import sys\n"
                  "for index in range(240):\n"
                  "    sys.stdout.write(f'out-{index}\\n')\n"
                  "    sys.stderr.write(f'err-{index}\\n')\n"
                  "    sys.stdout.flush()\n"
                  "    sys.stderr.flush()\n")
        result = self.run_script(script)
        self.assertIn('out-0', self.read('stdout.txt'))
        self.assertIn('out-239', self.read('stdout.txt'))
        self.assertIn('err-0', self.read('stderr.txt'))
        self.assertIn('err-239', self.read('stderr.txt'))
        self.assertEqual(result['status'], 'completed')

    def test_utf8_bytes_split_across_reads_are_decoded(self):
        script = ("import sys,time\n"
                  "data = '测'.encode('utf-8')\n"
                  "sys.stdout.buffer.write(data[:1]); sys.stdout.buffer.flush()\n"
                  "time.sleep(.2)\n"
                  "sys.stdout.buffer.write(data[1:] + b'\\n'); sys.stdout.buffer.flush()\n")
        result = self.run_script(script)
        self.assertIn('测', self.read('stdout.txt'))
        self.assertEqual(result['status'], 'completed')

    def test_stream_quota_truncates_without_blocking(self):
        script = ("import sys\n"
                  "sys.stdout.write('y' * 8192)\n"
                  "sys.stdout.flush()\n")
        with patch('gsp.command_execution.MAX_STREAM_BYTES', 1024):
            result = self.run_script(script)
        stdout = result['log_integrity']['stdout']
        self.assertTrue(stdout['truncated'])
        self.assertLessEqual(stdout['bytes'], 1024)
        self.assertGreater(stdout['raw_bytes'], 1024)
        self.assertEqual(result['status'], 'completed')

    def test_metadata_redacts_argv_and_never_stores_environment(self):
        result = self.run_script(
            "print('ok')",
            metadata={'task': 'T01', 'argv': ['tool', 'token=SUPERSECRET']})
        progress = json.loads((self.folder / 'command-progress.json').read_text(encoding='utf-8'))
        self.assertNotIn('SUPERSECRET', json.dumps(progress))
        self.assertIn('<REDACTED>', json.dumps(progress))
        self.assertNotIn('env', progress)
        self.assertEqual(result['status'], 'completed')

    def test_default_stdin_remains_a_closed_device(self):
        result = self.run_script(
            "import sys\n"
            "print(repr(sys.stdin.buffer.read()))\n")
        self.assertEqual(result['status'], 'completed')
        self.assertIn("b''", self.read('stdout.txt'))
        self.assertIsNone(result['input'])

    def test_bounded_input_is_sent_through_a_pipe(self):
        result = self.run_script(
            "import sys\n"
            "data = sys.stdin.buffer.read()\n"
            "sys.stdout.write(data.decode('utf-8') + '\\n')\n",
            input_data='hello from piped stdin',
        )
        self.assertEqual(result['status'], 'completed')
        self.assertIn('hello from piped stdin', self.read('stdout.txt'))
        self.assertTrue(result['input']['complete'])
        self.assertEqual(result['input']['written'], result['input']['bytes'])

    def test_child_early_close_unblocks_input_writer(self):
        result = self.run_script(
            "import sys\n"
            "sys.stdin.buffer.read(1)\n",
            timeout=5,
            input_data=b'x' * (256 * 1024),
        )
        self.assertEqual(result['status'], 'failed')
        self.assertLess(result['duration_s'], 4.5)
        self.assertIsNotNone(result['input'])
        self.assertFalse(result['input']['complete'])

    def test_timeout_closes_the_input_pipe_and_terminates_child(self):
        result = self.run_script(
            "import time\n"
            "time.sleep(10)\n",
            timeout=.5,
            input_data=b'x' * (256 * 1024),
        )
        self.assertEqual(result['status'], 'timeout')
        self.assertLess(result['duration_s'], 5)
        self.assertIsNotNone(result['input'])

    def test_input_larger_than_the_limit_is_rejected_before_launch(self):
        from gsp.command_execution import MAX_INPUT_BYTES
        from gsp.runtime import WorkflowError

        with patch('gsp.command_execution.subprocess.Popen') as popen:
            with self.assertRaisesRegex(WorkflowError, 'COMMAND_INPUT_TOO_LARGE'):
                self.run_script(
                    "print('never')",
                    input_data=b'x' * (MAX_INPUT_BYTES + 1),
                )
        popen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
