import argparse
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from gsp import runtime


class CargoGateTests(unittest.TestCase):
    def test_ignored_local_test_entry_is_bound_and_edits_revoke_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);script=root/'.gsp/run-tests.py';script.parent.mkdir();script.write_text('print(1)')
            inputs=runtime.command_input_hashes(root,['python','.gsp/run-tests.py'])
            self.assertIn('.gsp/run-tests.py',inputs)
            receipt={'command_inputs':inputs}
            runtime.verify_command_inputs(root,receipt)
            script.write_text('print(2)')
            with self.assertRaisesRegex(runtime.WorkflowError,'COMMAND_INPUT_CHANGED'):
                runtime.verify_command_inputs(root,receipt)

    def test_summary_never_masks_failed_process_or_failed_target(self):
        for exit_code,summary,expected in [
            (0,'test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out;\ntest result: ok. 0 passed; 0 failed; 2 ignored; 0 measured; 0 filtered out;','pass'),
            (101,'test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out;','fail'),
            (0,'test result: FAILED. 3 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out;','failed-output'),
            (0,'test result: ok. 0 passed; 0 failed; 7 ignored; 0 measured; 0 filtered out;','zero-tests'),
            (101,'error: could not compile crate','fail')]:
            with self.subTest(exit=exit_code,expected=expected),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);project=runtime.Project(root)
                state={'id':'cargo','tasks':[{'id':'T1','status':'running'}],'scope':{'mode':'off'},'evidence':[]}
                args=argparse.Namespace(task='T1',argv=['cargo','test'],timeout=30,kind='tests',runner=lambda a,t:subprocess.CompletedProcess(a,exit_code,summary,''))
                with patch.object(runtime,'snapshot',return_value={}),patch.object(project,'write'),patch.object(runtime,'contract_hash',return_value='contract'):
                    receipt,ok=runtime.run_check(project,state,root/'.gsp/runs/cargo',args)
                self.assertEqual(receipt['status'],expected)
                self.assertEqual(ok,expected=='pass')
