"""Focused runs use the same imports and isolation as the full test runner."""
from contextlib import redirect_stderr, redirect_stdout
import io
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import run_tests


class SelectedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        tests = self.root / 'tests'
        tests.mkdir()
        for name in ('test_a', 'test_b', 'test_lock'):
            (tests / (name + '.py')).write_text('def test_ok(): pass\n')
        (tests / 'tiers.json').write_text('{"serial":["test_lock"]}')

    def test_only_selected_modules_run_and_serial_module_runs_first(self):
        calls = []

        def run(root, modules):
            calls.append(list(modules))
            return 'Ran 1 test in 0.001s\nOK\n', 0

        with patch.object(run_tests, '_run_shard', side_effect=run), redirect_stdout(io.StringIO()):
            code = run_tests.main(['--jobs', '2', '--modules', 'test_b', 'test_lock'], self.root)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [['test_lock'], ['test_b']])

    def test_unknown_module_refused_before_starting_a_process(self):
        with patch.object(run_tests, '_run_shard') as run, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                run_tests.main(['--modules', 'test_missing'], self.root)
        run.assert_not_called()

    def test_list_deduplicates_selected_modules_without_running(self):
        output = io.StringIO()
        with patch.object(run_tests, '_run_shard') as run, redirect_stdout(output):
            self.assertEqual(run_tests.main(['--modules', 'test_b', 'test_b', '--list'], self.root), 0)
        self.assertEqual(output.getvalue(), 'test_b\n')
        run.assert_not_called()
