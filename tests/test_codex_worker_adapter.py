import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gsp.codex_worker_adapter import worker_argv, parse_native_events, run_worker


class CodexWorkerAdapterTests(unittest.TestCase):
    def test_resume_requires_exact_uuid_and_explicit_model(self):
        ident = '01a21b31-2815-72f2-9964-88ee78efc61b'
        job = {'phase': 'execute', 'schema': 'schema.json', 'result': 'result.json'}
        argv = worker_argv(job, 'codex.exe', ident)
        self.assertEqual(argv[argv.index('resume') + 1], ident)
        self.assertEqual(argv[argv.index('--model') + 1], 'group/deepseek-2')
        self.assertIn('model_reasoning_effort="max"', argv)
        self.assertNotIn('--last', argv)
        with self.assertRaisesRegex(ValueError, 'NATIVE_THREAD_ID_REQUIRED'):
            worker_argv(job, 'codex.exe', 'latest')

    def test_native_completion_not_acceptance_and_usage_is_not_double_counted(self):
        event = {'type': 'turn.completed', 'usage': {'input_tokens': 20, 'output_tokens': 4,
                                                    'cached_input_tokens': 10}}
        text = '\n'.join(json.dumps(row) for row in [
            {'type': 'thread.started', 'thread_id': 'thread-one'},
            {**event, 'response_id': 'response-one'}, {**event, 'response_id': 'response-one'}])
        parsed = parse_native_events(text)
        self.assertEqual(parsed['usage']['input_tokens'], 20)
        self.assertEqual(parsed['usage']['output_tokens'], 4)
        self.assertEqual(parsed['usage']['cached_input_tokens'], 10)
        self.assertFalse(parsed['accepted'])
        self.assertEqual(parsed['actual_model'], 'unknown')

    def test_shared_executor_receives_prompt_and_exact_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ident = '01a21b31-2815-72f2-9964-88ee78efc61b'
            output = '\n'.join(json.dumps(row) for row in (
                {'type': 'thread.started', 'thread_id': ident},
                {'type': 'turn.completed', 'response_id': 'response-one',
                 'usage': {'input_tokens': 1, 'output_tokens': 1}},
            )) + '\n'
            (root / 'stdout.txt').write_text(output, encoding='utf-8')
            (root / 'stderr.txt').write_text('', encoding='utf-8')
            result = self.evidence(root)
            job = {'phase': 'execute', 'schema': 'schema.json', 'result': str(root / 'result.json')}
            with patch('gsp.codex_worker_adapter.execute_command', return_value=result) as execute:
                value = run_worker(
                    job, root, {}, 30, root, 'full immutable task contract',
                    executable='codex.exe', thread_id=ident,
                )
            self.assertEqual(execute.call_args.kwargs['input_data'], b'full immutable task contract')
            self.assertEqual(execute.call_args.kwargs['cancel_check'], None)
            self.assertEqual(value['requested_model'], 'group/deepseek-2')
            self.assertEqual(value['native_session_id'], ident)
            self.assertEqual(value['status'], 'completed')
            self.assertFalse(value['accepted'])

    def test_missing_completion_event_cannot_be_completed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ident = '01a21b31-2815-72f2-9964-88ee78efc61b'
            (root / 'stdout.txt').write_text(json.dumps({
                'type': 'thread.started', 'thread_id': ident,
            }) + '\n', encoding='utf-8')
            (root / 'stderr.txt').write_text('', encoding='utf-8')
            job = {'phase': 'execute', 'schema': 'schema.json',
                   'result': str(root / 'result.json')}
            with patch('gsp.codex_worker_adapter.execute_command',
                       return_value=self.evidence(root)):
                value = run_worker(
                    job, root, {}, 30, root, 'contract',
                    executable='codex.exe', thread_id=ident,
                )
        self.assertEqual(value['status'], 'evidence-incomplete')
        self.assertIn('missing-turn-completed', value['warnings'])
        self.assertFalse(value['accepted'])

    def test_log_hash_mismatch_cannot_be_completed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ident = '01a21b31-2815-72f2-9964-88ee78efc61b'
            output = '\n'.join(json.dumps(row) for row in (
                {'type': 'thread.started', 'thread_id': ident},
                {'type': 'turn.completed', 'response_id': 'response-one'},
            )) + '\n'
            (root / 'stdout.txt').write_text(output, encoding='utf-8')
            (root / 'stderr.txt').write_text('changed after execution\n',
                                               encoding='utf-8')
            execution = self.evidence(root)
            execution['log_integrity']['stderr']['sha256'] = '0' * 64
            job = {'phase': 'execute', 'schema': 'schema.json',
                   'result': str(root / 'result.json')}
            with patch('gsp.codex_worker_adapter.execute_command',
                       return_value=execution):
                value = run_worker(
                    job, root, {}, 30, root, 'contract',
                    executable='codex.exe', thread_id=ident,
                )
        self.assertEqual(value['status'], 'evidence-incomplete')
        self.assertFalse(value['accepted'])

    def test_resume_response_for_different_thread_cannot_be_completed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            requested = '01a21b31-2815-72f2-9964-88ee78efc61b'
            observed = '01a21b31-2815-72f2-9964-88ee78efc61c'
            output = '\n'.join(json.dumps(row) for row in (
                {'type': 'thread.started', 'thread_id': observed},
                {'type': 'turn.completed', 'response_id': 'response-one'},
            )) + '\n'
            (root / 'stdout.txt').write_text(output, encoding='utf-8')
            (root / 'stderr.txt').write_text('', encoding='utf-8')
            job = {'phase': 'execute', 'schema': 'schema.json',
                   'result': str(root / 'result.json')}
            with patch('gsp.codex_worker_adapter.execute_command',
                       return_value=self.evidence(root)):
                value = run_worker(
                    job, root, {}, 30, root, 'contract',
                    executable='codex.exe', thread_id=requested,
                )
        self.assertEqual(value['status'], 'native-thread-mismatch')
        self.assertFalse(value['accepted'])

    @staticmethod
    def evidence(root):
        streams = {}
        for name in ('stdout', 'stderr'):
            path = root / (name + '.txt')
            data = path.read_bytes()
            streams[name] = {
                'path': str(path), 'sha256': hashlib.sha256(data).hexdigest(),
                'bytes': len(data), 'raw_bytes': len(data), 'truncated': False,
            }
        return {
            'status': 'completed', 'returncode': 0,
            'stdout_path': streams['stdout']['path'],
            'stderr_path': streams['stderr']['path'],
            'log_integrity': streams, 'process_alive': False,
        }
