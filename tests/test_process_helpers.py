import unittest
from unittest.mock import patch

import process_helpers


class HiddenProcessTests(unittest.TestCase):
    def test_git_identity_hides_every_git_query(self):
        from pathlib import Path
        from types import SimpleNamespace
        from gsp.runtime import git_identity
        with patch('gsp.runtime.subprocess.run', side_effect=[SimpleNamespace(returncode=0, stdout=str(Path.cwd())), SimpleNamespace(returncode=0, stdout='main'), SimpleNamespace(returncode=0, stdout='fixture-head')]) as launch:
            self.assertEqual(git_identity(Path.cwd())['head'], 'fixture-head')
            import os, subprocess
            expected = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            self.assertTrue(all(c.kwargs['creationflags'] == expected for c in launch.call_args_list))
            self.assertEqual(launch.call_count, 3)

    def test_windows_children_keep_existing_flags_and_add_no_window(self):
        with patch('process_helpers.os.name', 'nt'), patch.object(process_helpers.subprocess, 'CREATE_NO_WINDOW', 0x08000000, create=True):
            for wrapper, target in [(process_helpers.run, 'run'), (process_helpers.popen, 'Popen')]:
                with self.subTest(target=target), patch.object(process_helpers.subprocess, target) as launch:
                    wrapper(['fixture'], creationflags=512, cwd='fixture-root')
                    self.assertEqual(launch.call_args.kwargs['creationflags'], 512 | 0x08000000)
                    self.assertEqual(launch.call_args.kwargs['cwd'], 'fixture-root')

    def test_non_windows_does_not_add_flags(self):
        with patch('process_helpers.os.name', 'posix'), patch.object(process_helpers.subprocess, 'run') as launch:
            process_helpers.run(['fixture'])
            self.assertNotIn('creationflags', launch.call_args.kwargs)
