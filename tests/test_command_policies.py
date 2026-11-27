import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gsp.command_policies import (command_policy,parser,SESSION_CONTEXT_WRITE_COMMANDS,
    SESSION_CONTEXT_REPORT_COMMANDS,SESSION_CONTEXT_ACCEPT_COMMANDS)
from gsp.cli import main


class CommandPolicyTests(unittest.TestCase):
    def test_every_work_mutation_declares_write_authority(self):
        for command in SESSION_CONTEXT_WRITE_COMMANDS|SESSION_CONTEXT_REPORT_COMMANDS|SESSION_CONTEXT_ACCEPT_COMMANDS:
            self.assertIn('write',command_policy(command).capabilities,command)
        self.assertIn('write',command_policy('quality').capabilities)

    def test_brief_resume_is_zero_write_and_zero_git_without_schema_creation(self):
        with tempfile.TemporaryDirectory(prefix='gsp-brief-') as folder:
            root=Path(folder)
            run=root/'.gsp/runs/work'
            run.mkdir(parents=True)
            (root/'.gsp/project.json').write_text(json.dumps({'id':'brief-project','root':str(root)}),encoding='utf-8')
            (run/'state.json').write_text(json.dumps({'id':'work','project_id':'brief-project',
                'root':str(root),'status':'ready','tasks':[]}),encoding='utf-8')
            before={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
            output=io.StringIO()
            with patch('subprocess.run',side_effect=AssertionError('brief called Git')), \
                 patch('pathlib.Path.mkdir',side_effect=AssertionError('brief created directory')), \
                 patch('sqlite3.connect',side_effect=AssertionError('brief opened database')), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(0,main(['--project',str(root),'resume','work','--brief']))
            self.assertEqual('NOT_CHECKED',json.loads(output.getvalue())['validation'])
            after={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(before,after)

    def test_explicit_strict_resume_keeps_compatibility_semantics(self):
        args=parser().parse_args(['resume','work'])
        self.assertFalse(command_policy('resume',args).readonly)
        self.assertEqual('writer',command_policy('resume',args).lock)


if __name__=='__main__':
    unittest.main()
