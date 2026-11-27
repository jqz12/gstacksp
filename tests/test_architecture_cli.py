import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from gsp.architecture import main


class ArchitectureCliTests(unittest.TestCase):
    def test_reviewed_baseline_checks_and_partial_scope_remains_explicit(self):
        with tempfile.TemporaryDirectory(prefix='gsp-architecture-cli-') as folder:
            root = Path(folder)
            (root / 'app.py').write_text('value = 1\n', encoding='utf-8')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(['--project', folder, '--propose-baseline', 'legacy project intake'])
            self.assertEqual(code, 0)
            baseline = json.loads(output.getvalue())
            self.assertEqual(baseline['review_status'], 'PROPOSED_NOT_ACCEPTED')
            (root / 'baseline.json').write_text(json.dumps(baseline), encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--project', folder, '--baseline', 'baseline.json']), 0)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(['--project', folder, '--baseline', 'baseline.json', '--changed', 'app.py'])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())['validation'], 'PARTIAL')
