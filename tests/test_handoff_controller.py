import json
import sys
import tempfile
import unittest
from pathlib import Path

from gsp.runtime import Project, dispatch, parser, read, WorkflowError, atomic, digest


class HandoffControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-handoff-')
        self.project = Project(Path(self.tmp.name))
        self.project.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def cmd(self, *argv):
        return dispatch(self.project, parser().parse_args(list(argv)))[0]

    def prepare_plan(self):
        self.cmd('new', 'demo', '--title', 'Demo', '--legacy-layout')
        state = read(self.project.meta / 'runs/demo/state.json')
        self.project.document(state, 'design').write_text('# A valid demo specification with acceptance', encoding='utf-8')
        self.cmd('approve', 'demo', '--quote', 'approved')
        self.project.document(state, 'plan').write_text('# Plan\n## Task T1: Demo\nConsumes: input\nProduces: output\nRun: unit\nExpected: pass\n', encoding='utf-8')
        self.cmd('task-add', 'demo', 'T1', '--title', 'Demo', '--acceptance', 'pass')
        return read(self.project.meta / 'runs/demo/state.json')

    def test_preview_blocks_execute_until_plan_is_sealed(self):
        self.prepare_plan()
        result = self.cmd('handoff', 'demo', '--to', 'execute')
        self.assertFalse(result['allowed'])
        self.assertEqual(result['blockers'][0]['code'], 'PLAN_NOT_SEALED')

    def test_execute_does_not_require_any_legacy_scope_mode(self):
        state = self.prepare_plan()
        state['plan_seal'] = {
            'sha256': digest(self.project.document(state, 'plan')),
            'tasks_sha256': self.project.task_definition_hash(state),
        }
        state_file = self.project.meta / 'runs/demo/state.json'
        for mode in ('pending', 'frozen', 'off'):
            with self.subTest(mode=mode):
                state['scope'] = {
                    'mode': mode,
                    'path': str(self.project.root / 'src') if mode == 'frozen' else None,
                    'quote': 'legacy scope',
                    'at': '2000-01-01T00:00:00+00:00',
                }
                atomic(state_file, state)
                result = self.cmd('handoff', 'demo', '--to', 'execute')
                self.assertTrue(result['allowed'], result)

    def test_advance_records_handoff_after_valid_plan(self):
        self.prepare_plan()
        state = read(self.project.meta / 'runs/demo/state.json')
        state['plan_seal'] = {'sha256': digest(self.project.document(state, 'plan')),
                              'tasks_sha256': self.project.task_definition_hash(state)}
        self.project.write(state, 'fixture-plan-seal', action_uid='fixture:plan-seal')
        result = self.cmd('advance', 'demo', '--to', 'execute')
        self.assertTrue(result['allowed'], result)
        self.assertEqual(result['to_stage'], 'execute')
        handoffs = self.project.meta / 'runs/demo/handoffs.jsonl'
        self.assertTrue(handoffs.is_file())
        record = json.loads(handoffs.read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(record['from_stage'], 'plan')
        self.assertEqual(record['to_stage'], 'execute')

    def test_invalid_transition_does_not_change_state_or_write_handoff(self):
        self.prepare_plan()
        result = self.cmd('handoff', 'demo', '--to', 'ready')
        self.assertFalse(result['allowed'])
        failed = self.cmd('advance', 'demo', '--to', 'ready')
        self.assertFalse(failed['allowed'])
        state = read(self.project.meta / 'runs/demo/state.json')
        self.assertEqual(state['status'], 'plan')
        self.assertFalse((self.project.meta / 'runs/demo/handoffs.jsonl').exists())

    def test_advance_response_loss_replays_the_committed_transition(self):
        self.prepare_plan()
        self.cmd('seal-plan','demo')
        self.cmd('advance','demo','--to','execute')
        run=self.project.meta/'runs/demo'
        tracked=[run/'state.json',run/'events.jsonl',run/'handoffs.jsonl']
        before={path:path.read_bytes() for path in tracked}
        marker=read(run/'state.json')['_command_commit']
        replay=self.cmd('advance','demo','--to','execute')
        self.assertTrue(replay['replayed'])
        self.assertEqual({path:path.read_bytes() for path in tracked},before)
        self.assertEqual(read(run/'state.json')['_command_commit'],marker)

    def test_controller_mode_rejects_direct_stage_commands(self):
        self.cmd('new', 'modern', '--title', 'Modern')
        state = read(self.project.meta / 'runs/modern/state.json')
        self.project.document(state, 'design').write_text('# A valid modern specification with acceptance', encoding='utf-8')
        self.cmd('approve', 'modern', '--quote', 'approved')
        self.project.document(state, 'plan').write_text('# Plan\n## Task T1: Modern\nConvergence: {"requires_slices":false,"single_outcome":true,"small":true}\nConsumes: input\nProduces: output\nRun: unit\nExpected: pass\n', encoding='utf-8')
        self.cmd('task-add', 'modern', 'T1', '--title', 'Modern', '--acceptance', 'pass')
        self.cmd('seal-plan', 'modern')
        with self.assertRaises(WorkflowError):
            self.cmd('task-start', 'modern', 'T1')
        self.cmd('advance', 'modern', '--to', 'execute')
        self.cmd('task-start', 'modern', 'T1')

