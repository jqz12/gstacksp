"""Read-only run progress: ownership, terminal states and lock-free status."""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from gsp import runtime
from gsp.command_execution import execute_command, progress_snapshot


class RunProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-progress-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run_dir = self.root / '.gsp/runs/work'
        self.evidence_id = 'a' * 32
        self.folder = self.run_dir / 'evidence' / self.evidence_id
        self.folder.mkdir(parents=True)
        (self.folder / 'stdout.txt').write_text('line\n', encoding='utf-8')
        (self.folder / 'stderr.txt').write_text('', encoding='utf-8')
        self.write_state()

    def write_state(self, evidence=None, inflight=None):
        state = {
            'id': 'work', 'project_id': 'fixture', 'root': str(self.root),
            'status': 'execute', 'tasks': [],
            'evidence': evidence if evidence is not None else [],
            'inflight': inflight if inflight is not None else {
                'id': self.evidence_id, 'task': 'T01', 'phase': 'command', 'at': 'now'},
        }
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / 'state.json').write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def result(self, status='completed'):
        document = {
            'schema': 1, 'evidence_id': self.evidence_id, 'status': status,
            'returncode': 0, 'launch_error': None, 'interruption': None,
            'stdout_path': str(self.folder / 'stdout.txt'),
            'stderr_path': str(self.folder / 'stderr.txt'),
            'log_integrity': {
                'stdout': {'bytes': 5, 'raw_bytes': 5, 'truncated': False},
                'stderr': {'bytes': 0, 'raw_bytes': 0, 'truncated': False}},
            'process_identity': None, 'parent': None,
        }
        (self.folder / 'command-result.json').write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def progress(self, status='running', parent=None):
        document = {
            'schema': 1, 'evidence_id': self.evidence_id, 'status': status,
            'phase': 'command', 'heartbeat_at': '2026-01-01T00:00:00+00:00',
            'elapsed_s': 1.0, 'parent': parent,
            'process_identity': None,
            'stdout': {'path': str(self.folder / 'stdout.txt'), 'bytes': 5,
                       'raw_bytes': 5, 'truncated': False},
            'stderr': {'path': str(self.folder / 'stderr.txt'), 'bytes': 0,
                       'raw_bytes': 0, 'truncated': False},
            'build_context': {'mode': 'off', 'root': None},
        }
        (self.folder / 'command-progress.json').write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def test_evidence_must_belong_to_the_run(self):
        self.write_state(evidence=[f'.gsp/runs/work/evidence/{"b" * 32}/receipt.json'],
                         inflight={})
        snapshot = progress_snapshot(self.run_dir, self.evidence_id)
        self.assertFalse(snapshot['found'])
        self.assertEqual(snapshot['error'], 'EVIDENCE_NOT_OWNED')

    def test_terminal_result_is_returned_immediately(self):
        self.result()
        snapshot = progress_snapshot(self.run_dir, self.evidence_id)
        self.assertTrue(snapshot['found'])
        self.assertTrue(snapshot['terminal'])
        self.assertEqual(snapshot['status'], 'completed')
        self.assertEqual(snapshot['stdout_path'], str(self.folder / 'stdout.txt'))

    def test_quiet_running_child_returns_without_waiting_for_completion(self):
        self.progress()
        started = time.monotonic()
        snapshot = progress_snapshot(self.run_dir, self.evidence_id, wait_seconds=.3)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertFalse(snapshot['terminal'])
        self.assertEqual(snapshot['status'], 'running')

    def test_parent_loss_is_not_reported_as_terminal_success(self):
        self.progress(parent={'pid': 2147483647, 'token': None})
        snapshot = progress_snapshot(self.run_dir, self.evidence_id)
        self.assertFalse(snapshot['terminal'])
        self.assertEqual(snapshot['status'], 'parent-lost')
        self.assertFalse(snapshot['parent_alive'])

    def test_wait_returns_after_the_child_finishes(self):
        box = {}

        def run_child():
            box.update(result=execute_command(
                [sys.executable, '-c', 'import time; time.sleep(0.5); print("done")'],
                self.root, dict(os.environ), 10, self.folder, {'task': 'T01'}))

        worker = threading.Thread(target=run_child)
        worker.start()
        try:
            snapshot = progress_snapshot(self.run_dir, self.evidence_id, wait_seconds=5)
        finally:
            worker.join(timeout=10)
        self.assertTrue(snapshot['terminal'])
        self.assertEqual(snapshot['status'], 'completed')
        self.assertEqual(box['result']['status'], 'completed')

    def test_run_status_is_readable_while_a_writer_lock_exists(self):
        self.result()
        lock = self.root / '.gsp/writer.lock'
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text('fixture lock placeholder', encoding='utf-8')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = runtime.main(['--project', str(self.root), 'run-status', 'work',
                                 '--evidence', self.evidence_id, '--wait', '0'])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload['terminal'])
        self.assertEqual(payload['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
