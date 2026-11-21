import unittest
import subprocess
import tempfile
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

    def test_environment_context_preserves_unrelated_empty_native_values(self):
        import json
        import os
        import sys
        from pathlib import Path

        script = """
import json
import os
import subprocess
import sys
from process_helpers import environment

with environment('CODEX_HOME', 'fixture-profile'):
    pass

probe = subprocess.run(
    [sys.executable, '-c', 'import json, os; print(json.dumps({k: os.environ.get(k) for k in (\"GSP_EMPTY_FIXTURE\", \"CODEX_HOME\")}))'],
    capture_output=True,
    text=True,
    check=True,
)
print(probe.stdout.strip())
"""
        env = os.environ.copy()
        env['GSP_EMPTY_FIXTURE'] = ''
        result = subprocess.run(
            [sys.executable, '-c', script],
            cwd=Path(__file__).parent,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {'GSP_EMPTY_FIXTURE': '', 'CODEX_HOME': None},
        )

    def test_commit_number_warning_is_non_blocking(self):
        from pathlib import Path
        from gsp.runtime import commit_number_warnings
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for args in (['init','-q'], ['config','user.name','Test'], ['config','user.email','test@example.com']):
                subprocess.run(['git',*args],cwd=root,check=True,capture_output=True)
            (root/'file.txt').write_text('x',encoding='utf-8')
            subprocess.run(['git','add','file.txt'],cwd=root,check=True)
            subprocess.run(['git','commit','-qm','chore: baseline'],cwd=root,check=True)
            old=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
            (root/'file.txt').write_text('y',encoding='utf-8')
            subprocess.run(['git','commit','-am','fix: change'],cwd=root,check=True,capture_output=True)
            new=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
            warnings=commit_number_warnings(root,old,new,'GSP-000001')
            self.assertEqual(warnings[0]['code'],'GIT_COMMIT_TASK_NUMBER_MISSING')
