import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gsp.runtime import atomic, read, WorkflowError
from types import SimpleNamespace


class StateRetryTests(unittest.TestCase):
    def test_driver_load_inherits_read_retry(self):
        from gsp.driver import Driver
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = SimpleNamespace(root=root, meta=root/'.gsp', paths=lambda task: (root/'.gsp/runs'/task, root/'docs'/task), identity=lambda: {'id': 'fixture'})
            driver = Driver(project, 'work')
            atomic(driver.file, {'project_id': 'fixture', 'root': str(root)})
            original = Path.read_text
            attempts = []
            def busy(path, *args, **kwargs):
                attempts.append(path)
                if len(attempts) <= 3: raise PermissionError(errno.EACCES, 'busy')
                return original(path, *args, **kwargs)
            with patch.object(Path, 'read_text', busy), patch('gsp.runtime.time.sleep'):
                self.assertEqual(driver.load()['project_id'], 'fixture')
                self.assertEqual(len(attempts), 4)

    def test_read_recovers_after_three_access_denials(self):
        with patch.object(Path, 'read_text', side_effect=[PermissionError(errno.EACCES, 'busy')] * 3 + ['{"value": 7}']), patch('gsp.runtime.time.sleep') as sleep:
            self.assertEqual(read('state.json'), {'value': 7})
            self.assertEqual(sleep.call_count, 3)
            self.assertTrue(all(call.args == (.05,) for call in sleep.call_args_list))

    def test_read_denial_is_bounded_and_invalid_json_is_not_retried(self):
        for failure, calls in [(PermissionError(errno.EACCES, 'busy'), 4), (FileNotFoundError(errno.ENOENT, 'missing'), 1)]:
            with self.subTest(failure=failure), patch.object(Path, 'read_text', side_effect=failure) as reader, patch('gsp.runtime.time.sleep'):
                with self.assertRaises(WorkflowError): read('state.json')
                self.assertEqual(reader.call_count, calls)
        with patch.object(Path, 'read_text', return_value='{bad') as reader:
            with self.assertRaises(WorkflowError): read('state.json')
            self.assertEqual(reader.call_count, 1)

    def test_atomic_retries_publication_without_losing_old_state(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'state.json'
            atomic(target, {'old': True})
            import os
            replace = os.replace
            with patch('gsp.runtime.os.replace') as publish, patch('gsp.runtime.time.sleep') as sleep:
                # Complete the final replacement with the same temporary file.
                def attempt(src, dst):
                    if publish.call_count <= 3: raise PermissionError(errno.EACCES, 'busy')
                    return replace(src, dst)
                publish.side_effect = attempt
                atomic(target, {'new': True})
                self.assertEqual(sleep.call_count, 3)
            self.assertEqual(read(target), {'new': True})
            with patch('gsp.runtime.os.replace', side_effect=PermissionError(errno.EACCES, 'busy')) as publish, patch('gsp.runtime.time.sleep'):
                with self.assertRaises(PermissionError): atomic(target, {'lost': True})
                self.assertEqual(publish.call_count, 4)
            self.assertEqual(read(target), {'new': True})
            self.assertEqual(list(Path(folder).iterdir()), [target])
