import json
from pathlib import Path
import tempfile
import unittest

from gsp.architecture import analyze_project, make_baseline
from gsp.architecture_inputs import freeze_architecture_inputs, validate_architecture_inputs
from gsp.core import WorkflowError


class ArchitectureInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='gsp-architecture-inputs-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        (self.root/'small.py').write_text('value=1\n',encoding='utf-8')
        self.baseline=self.root/'baseline.json'
        self.baseline.write_text(json.dumps(make_baseline(
            analyze_project(self.root,rules={'max_lines':3}),reason='existing project')),encoding='utf-8')
        self.policy=freeze_architecture_inputs(self.root,{
            'required':True,'baseline':'baseline.json','scope':['small.py']})
        self.contract={'architecture':self.policy}

    def test_unchanged_and_unrelated_file_do_not_expand_scope(self):
        (self.root/'unrelated.py').write_text('x=1\n'*9,encoding='utf-8')
        result=validate_architecture_inputs(self.root,self.contract)
        self.assertTrue(result['allowed'])
        self.assertEqual(['small.py'],result['coverage']['affected'])

    def test_changed_baseline_requires_explicit_reapproval(self):
        self.baseline.write_text(self.baseline.read_text()+'\n',encoding='utf-8')
        with self.assertRaisesRegex(WorkflowError,'ARCHITECTURE_BASELINE_CHANGED'):
            validate_architecture_inputs(self.root,self.contract)

    def test_new_violation_allows_diagnostics_but_blocks_acceptance(self):
        (self.root/'small.py').write_text('x=1\n'*8,encoding='utf-8')
        self.assertEqual('PENDING_QUALITY',validate_architecture_inputs(
            self.root,self.contract,phase='execute')['validation'])
        with self.assertRaisesRegex(WorkflowError,'ARCHITECTURE_CHANGE_BLOCKED'):
            validate_architecture_inputs(self.root,self.contract)

    def test_unknown_language_requires_review_and_actual_paths_extend_scope(self):
        (self.root/'changed.rs').write_text('fn main() {}',encoding='utf-8')
        result=validate_architecture_inputs(self.root,self.contract,actual_paths=['changed.rs'])
        self.assertFalse(result['allowed'])
        self.assertEqual(['ARCHITECTURE_MANUAL_REVIEW_REQUIRED'],result['obligations'])
        self.assertTrue(validate_architecture_inputs(self.root,self.contract,
            actual_paths=['changed.rs'],phase='execute')['allowed'])

    def test_scope_is_not_an_edit_allowlist_but_cannot_escape_repository(self):
        with self.assertRaisesRegex(WorkflowError,'ARCHITECTURE_PATH_ESCAPE'):
            validate_architecture_inputs(self.root,self.contract,actual_paths=['../outside.py'])


if __name__=='__main__':
    unittest.main()
