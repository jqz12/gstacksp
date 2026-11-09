"""Presentation-only install scope regression; no real installation or global writes."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gsp.runtime import main, parser

class InstallHintTests(unittest.TestCase):
    def call(self,*args):
        out,err=io.StringIO(),io.StringIO()
        with redirect_stdout(out),redirect_stderr(err):
            code=main(list(args))
        self.assertEqual(code,0,err.getvalue())
        return json.loads(out.getvalue()),err.getvalue()
    def test_project_install_explains_scopes_and_actual_targets(self):
        with tempfile.TemporaryDirectory() as folder, patch('gsp.install.install',return_value={'installed':[]}) as install:
            result,notice=self.call('--project',folder,'install','--host','both')
            self.assertEqual(result,{'installed':[]})
            self.assertIn('--scope project',notice);self.assertIn('--scope user',notice)
            self.assertIn(str(Path(folder)/'.agents/skills'),notice)
            self.assertIn(str(Path(folder)/'.claude/skills'),notice)
            install.assert_called_once_with(Path(folder).resolve(),'both',[])
    def test_user_install_notice_uses_explicit_profile_and_keeps_json(self):
        with tempfile.TemporaryDirectory() as folder, patch('gsp.user_install.UserInstall.apply',return_value={'dry_run':True}) as apply:
            result,notice=self.call('install','--scope','user','--host','codex','--profile',folder,'--dry-run')
            self.assertEqual(result,{'dry_run':True})
            self.assertIn(str(Path(folder)/'skills'),notice)
            self.assertIn('does not create',notice)
            self.assertIn('not automatically removed',notice)
            apply.assert_called_once_with('install',[],True)
            self.assertEqual(list(Path(folder).iterdir()),[])
    def test_help_explains_global_is_current_user(self):
        out=io.StringIO()
        with redirect_stdout(out),self.assertRaises(SystemExit):parser().parse_args(['install','--help'])
        self.assertIn('current user',out.getvalue())
