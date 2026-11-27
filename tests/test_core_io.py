import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gsp.core import WorkflowError, atomic, inside, read


class CoreIoTests(unittest.TestCase):
    def test_atomic_unicode_replacement_and_invalid_state(self):
        with tempfile.TemporaryDirectory(prefix='gsp-core-') as folder:
            path = Path(folder) / 'state.json'
            atomic(path, {'status': '等待恢复'})
            self.assertEqual(read(path), {'status': '等待恢复'})
            atomic(path, '{bad-json')
            with self.assertRaisesRegex(WorkflowError, 'INVALID_STATE'):
                read(path)
            self.assertEqual([p.name for p in Path(folder).iterdir()], ['state.json'])
            self.assertFalse(inside(folder, Path(folder).parent))

    def test_publish_retry_does_not_repeat_value_production(self):
        import os
        with tempfile.TemporaryDirectory(prefix='gsp-core-') as folder:
            original = os.replace
            attempts = []
            def replace(source, destination):
                attempts.append(source)
                if len(attempts) < 3:
                    raise PermissionError(errno.EACCES, 'transient reader')
                return original(source, destination)
            with patch('gsp.core.os.replace', side_effect=replace):
                atomic(Path(folder) / 'state.json', {'one': 'candidate'})
            self.assertEqual(len(set(attempts)), 1)
            self.assertEqual(read(Path(folder) / 'state.json'), {'one': 'candidate'})
