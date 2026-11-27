"""Driver startup short transactions really reacquire a suspended mutex."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from gsp.runtime import locked, suspend_writer_lock


class NestedWriterMutexTests(unittest.TestCase):
    def test_suspension_preserves_the_original_reentrant_depth(self):
        with tempfile.TemporaryDirectory(prefix='gsp-nested-depth-') as folder:
            root = Path(folder)
            with locked(root):
                with locked(root):
                    with suspend_writer_lock(root):
                        with locked(root):
                            pass

    def test_nested_startup_transaction_excludes_a_second_process(self):
        with tempfile.TemporaryDirectory(prefix='gsp-nested-writer-') as folder:
            root = Path(folder)
            environment = dict(os.environ)
            environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
            code = ('from gsp.runtime import locked; from pathlib import Path; '
                    'import sys\nwith locked(Path(sys.argv[1])): print("admitted")\n')
            with locked(root):
                with suspend_writer_lock(root):
                    with locked(root):
                        result = subprocess.run([sys.executable, '-B', '-c', code, folder],
                            env=environment, capture_output=True, text=True, timeout=15)
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn('WORKFLOW_BUSY', result.stderr)
