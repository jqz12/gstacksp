"""Sharded test runner: merged summaries, module coverage and fast-tier guard."""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / 'tests'
sys.path.insert(0, str(ROOT))
from scripts.run_tests import _child_env, discover_modules, main, merge_summaries, pack_shards

RAN = re.compile(r'^Ran\s+\d+\s+tests?\s+in\s+[0-9.]+s', re.MULTILINE)
GUARD = {
    'subprocess': r'\bsubprocess\b',
    'self.cli(': r'self\.cli\s*\(',
    'self.cmd(': r'self\.cmd\s*\(',
    'dispatch(': r'\bdispatch\s*\(',
    'main(': r'\bmain\s*\(',
    'run(': r'\brun\s*\(',
    'Popen(': r'\bPopen\s*\(',
    'powershell': r'\bpowershell\b',
}


class RunTestsTests(unittest.TestCase):
    def test_child_environment_does_not_inherit_controller_authority(self):
        with patch('scripts.run_tests.os.environ', {'GSP_SESSION_CONTEXT': 'real-controller.json'}):
            child = _child_env(ROOT)
            self.assertNotIn('GSP_SESSION_CONTEXT', child)
            self.assertEqual(os.environ['GSP_SESSION_CONTEXT'], 'real-controller.json')

    def test_windows_git_longpaths_is_process_local_and_preserves_overrides(self):
        values = {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'example.key',
                  'GIT_CONFIG_VALUE_0': 'kept'}
        with patch('scripts.run_tests.os.environ', values), patch('scripts.run_tests.os.name', 'nt'):
            child = _child_env(ROOT)
            self.assertEqual(child['GIT_CONFIG_COUNT'], '2')
            self.assertEqual(child['GIT_CONFIG_KEY_0'], 'example.key')
            self.assertEqual(child['GIT_CONFIG_VALUE_0'], 'kept')
            self.assertEqual(child['GIT_CONFIG_KEY_1'], 'core.longpaths')
            self.assertEqual(child['GIT_CONFIG_VALUE_1'], 'true')
            self.assertEqual(os.environ['GIT_CONFIG_COUNT'], '1')

    def test_merge_summaries(self):
        texts = [
            'Ran 3 tests in 0.010s\n\nOK\n',
            'Ran 5 tests in 0.500s\n\nFAILED (failures=1, errors=2)\n',
            'Ran 4 tests in 0.200s\n\nOK (skipped=1)\n',
        ]
        self.assertEqual(merge_summaries(texts),
                         {'ran': 12, 'failures': 1, 'errors': 2, 'skipped': 1})
        self.assertEqual(merge_summaries([]),
                         {'ran': 0, 'failures': 0, 'errors': 0, 'skipped': 0})
        self.assertEqual(merge_summaries(['no summary here'])['errors'], 1)

    def test_single_job_matches_discover_modules(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / 'scripts/run_tests.py'), '--jobs', '1', '--list'],
            cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        listed = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        expected = sorted(path.stem for path in TESTS.glob('test_*.py') if path.is_file())
        self.assertEqual(listed, expected)
        self.assertEqual(discover_modules(ROOT, 'full'), expected)

    def test_fast_modules_are_subprocess_free(self):
        data = json.loads((TESTS / 'tiers.json').read_text(encoding='utf-8'))
        fast = data.get('fast') or []
        self.assertTrue(fast)
        for name in fast:
            path = TESTS / f'{name}.py'
            self.assertTrue(path.is_file(), name)
            text = path.read_text(encoding='utf-8')
            for label, pattern in GUARD.items():
                self.assertNotRegex(text, pattern, f'{name} starts a {label} process')

    def test_shards_cover_every_module_once(self):
        modules = discover_modules(ROOT, 'full')
        shards = pack_shards(TESTS, modules, 3)
        self.assertEqual(len(shards), 3)
        flattened = [name for shard in shards for name in shard]
        self.assertEqual(sorted(flattened), modules)
        self.assertEqual(pack_shards(TESTS, modules, 3), shards)

    def test_weights_override_definition_count(self):
        with tempfile.TemporaryDirectory(prefix='gsp-run-weights-') as folder:
            tests = Path(folder) / 'tests'
            tests.mkdir()
            for name in ('test_heavy', 'test_b', 'test_c', 'test_d'):
                (tests / f'{name}.py').write_text(
                    'import unittest\nclass T(unittest.TestCase):\n    def test_one(self): pass\n',
                    encoding='utf-8')
            weights = {'test_heavy': 100, 'test_b': 1, 'test_c': 1, 'test_d': 1}
            shards = pack_shards(tests, ['test_heavy', 'test_b', 'test_c', 'test_d'], 2, weights)
        sets = sorted(sorted(shard) for shard in shards)
        self.assertIn(['test_heavy'], sets)
        self.assertEqual(sorted(name for shard in shards for name in shard),
                         ['test_b', 'test_c', 'test_d', 'test_heavy'])

    def test_shards_execute_concurrently(self):
        with tempfile.TemporaryDirectory(prefix='gsp-run-concurrent-') as folder:
            root = Path(folder)
            tests = root / 'tests'
            tests.mkdir()
            for name in ('test_alpha', 'test_beta'):
                (tests / f'{name}.py').write_text('import unittest\n', encoding='utf-8')
            state = {'active': 0, 'peak': 0}
            lock = threading.Lock()

            def fake_shard(_root, _modules):
                with lock:
                    state['active'] += 1
                    state['peak'] = max(state['peak'], state['active'])
                time.sleep(0.4)
                with lock:
                    state['active'] -= 1
                return 'Ran 1 tests in 0.1s\n\nOK\n', 0

            import scripts.run_tests as runner
            out = io.StringIO()
            with patch.object(runner, '_run_shard', fake_shard), redirect_stdout(out):
                code = runner.main(['--jobs', '2'], root=root)
        self.assertEqual(code, 0, out.getvalue())
        self.assertGreaterEqual(state['peak'], 2)
        self.assertIn('Ran 2 tests', out.getvalue())

    def test_declared_serial_modules_run_alone_first(self):
        with tempfile.TemporaryDirectory(prefix='gsp-run-serial-') as folder:
            root = Path(folder)
            tests = root / 'tests'
            tests.mkdir()
            for name in ('test_alpha', 'test_beta', 'test_gamma'):
                (tests / f'{name}.py').write_text('import unittest\n', encoding='utf-8')
            (tests / 'tiers.json').write_text(
                json.dumps({'serial': ['test_alpha']}), encoding='utf-8')
            state = {'active': 0, 'peak': 0}
            calls = []
            lock = threading.Lock()

            def fake_shard(_root, modules):
                with lock:
                    state['active'] += 1
                    state['peak'] = max(state['peak'], state['active'])
                    calls.append((tuple(modules), state['active']))
                time.sleep(0.3)
                with lock:
                    state['active'] -= 1
                return 'Ran 1 tests in 0.1s\n\nOK\n', 0

            import scripts.run_tests as runner
            out = io.StringIO()
            with patch.object(runner, '_run_shard', fake_shard), redirect_stdout(out):
                code = runner.main(['--jobs', '2'], root=root)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(calls[0], (('test_alpha',), 1))
        remaining = sorted(name for modules, _ in calls[1:] for name in modules)
        self.assertEqual(remaining, ['test_beta', 'test_gamma'])
        self.assertIn('Ran 3 tests', out.getvalue())

    def test_one_module_run_propagates_failure(self):
        with tempfile.TemporaryDirectory(prefix='gsp-run-tests-') as folder:
            root = Path(folder)
            tests = root / 'tests'
            tests.mkdir()
            (tests / 'test_fail.py').write_text(
                'import unittest\n'
                'class FailureTests(unittest.TestCase):\n'
                '    def test_fails(self):\n'
                "        self.fail('boom')\n",
                encoding='utf-8')
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(['--jobs', '1'], root=root)
        text = out.getvalue() + err.getvalue()
        self.assertEqual(code, 1)
        self.assertIn('boom', text)
        self.assertEqual(len(RAN.findall(text)), 1, text)
        self.assertIn('FAILED (failures=1, errors=0)', text)


if __name__ == '__main__':
    unittest.main()
