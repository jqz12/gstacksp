import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

from gsp.runtime import inspect_lock, recover_lock, locked, process_token, WorkflowError

class LockRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'; (self.root / '.gsp').mkdir(parents=True)

    def test_normal_exit_removes_metadata(self):
        with locked(self.root):
            self.assertEqual(inspect_lock(self.root)['status'], 'live')
        self.assertFalse((self.root / '.gsp/writer.lock').exists())
        self.assertEqual(inspect_lock(self.root)['status'], 'absent')

    def test_dead_lock_can_be_recovered_with_evidence(self):
        lock = self.root / '.gsp/writer.lock'
        lock.write_text(json.dumps({'schema': 2, 'pid': os.getpid(), 'process_token': process_token(), 'at': 'x'}), encoding='utf-8')
        evidence = self.root / 'incident.md'; evidence.write_text('owner exited', encoding='utf-8')
        self.assertEqual(inspect_lock(self.root)['status'], 'dead')
        result = recover_lock(self.root, evidence, 'owner confirmed exited')
        self.assertEqual(result['status'], 'recovered')
        self.assertFalse(lock.exists())
        self.assertTrue(Path(result['backup']).exists())

    def test_unknown_and_malformed_locks_are_refused(self):
        lock = self.root / '.gsp/writer.lock'; evidence = self.root / 'incident.md'; evidence.write_text('x', encoding='utf-8')
        lock.write_text('{bad', encoding='utf-8')
        self.assertEqual(inspect_lock(self.root)['status'], 'malformed')
        with self.assertRaises(WorkflowError): recover_lock(self.root, evidence, 'bad')
        lock.write_text(json.dumps({'schema': 2, 'pid': 99999999, 'process_token': 'proc-start:old'}), encoding='utf-8')
        self.assertEqual(inspect_lock(self.root)['status'], 'unknown')
        with self.assertRaises(WorkflowError): recover_lock(self.root, evidence, 'unknown')

    def test_documents_only_has_no_default_report_side_effect(self):
        cli = Path(__file__).resolve().parents[1] / 'scripts/gsp.py'
        result = subprocess.run([sys.executable, str(cli), '--project', str(self.root), 'health', '--documents-only'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'docs/gsp/reports').exists())

if __name__ == '__main__': unittest.main()
