"""Cheap command preflight gates must not replace the full safety checks."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from gsp.runtime import Project, WorkflowError, dispatch, parser, read
from tests import test_topology_lifecycle as topology_lifecycle


def dispatch_result(fixture, *argv):
    return dispatch(fixture.project, parser().parse_args(list(argv)))


def build_ready(fixture):
    fixture.setup_git()
    fixture.prepare()
    fixture.cmd('topology-select', 'work', '--mode', 'in-place',
                '--quote', 'SYNTHETIC control root')
    fixture.cmd('advance', 'work', '--to', 'execute')
    fixture.cmd('task-start', 'work', 'T01')
    fixture.check('T01')
    fixture.cmd('task-done', 'work', 'T01')
    fixture.check('all')
    fixture.cmd('advance', 'work', '--to', 'review')
    document = fixture.project.paths('work')[1]
    (document / 'review.md').write_text(
        '# Review\nPreflight fixture reviewed; no blocking findings.\n', encoding='utf-8')
    fixture.cmd('review', 'work', '--reviewer', 'self', '--blocking', '0')
    fixture.cmd('advance', 'work', '--to', 'checking')
    fixture.cmd('advance', 'work', '--to', 'ready')


def work_record_path(fixture, uuid):
    for path in (fixture.root / 'docs/work').glob('*/*/record.json'):
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('uuid') == uuid:
            return path
    for path in (fixture.root / 'docs/work').glob('*/*/*/record.json'):
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('uuid') == uuid:
            return path
    raise AssertionError('fixture work record not found')


def set_closure_condition(fixture, condition):
    state = fixture.state()
    path = work_record_path(fixture, state['work_uuid'])
    record = json.loads(path.read_text(encoding='utf-8'))
    record['closure_condition'] = condition
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    from gsp.documents import Governance
    Governance(fixture.root).rebuild()


class CommandPreflightTests(unittest.TestCase):
    def setUp(self):
        self.fixture = topology_lifecycle.TopologyLifecycleTests(methodName='runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_close_missing_condition_has_zero_git_and_scan(self):
        build_ready(self.fixture)
        state_file = self.fixture.root / '.gsp/runs/work/state.json'
        before = state_file.read_bytes()

        def forbidden(*args, **kwargs):
            raise AssertionError('preflight must not launch Git or scan source')

        with patch('subprocess.run', forbidden):
            with patch('gsp.runtime.snapshot', forbidden):
                result, success = dispatch_result(
                    self.fixture, 'close', 'work', '--reason', 'missing closure condition')

        self.assertFalse(success)
        self.assertEqual(result['blockers'][0]['code'], 'WORK_CLOSURE_CONDITION_REQUIRED')
        self.assertEqual(state_file.read_bytes(), before)

    def test_ready_missing_review_reports_gate_before_full_validation(self):
        build_ready(self.fixture)
        document = self.fixture.project.paths('work')[1]
        (document / 'review.md').unlink()

        def forbidden(*args, **kwargs):
            raise AssertionError('ready preflight must stop before full validation')

        with patch.object(Project, 'state', forbidden):
            with patch('gsp.runtime.validate_delivery', forbidden):
                result, success = dispatch_result(self.fixture, 'ready', 'work')

        self.assertFalse(success)
        self.assertEqual(result['blockers'][0]['code'], 'REVIEW_MISSING')
        self.assertEqual(self.fixture.state()['status'], 'ready')

    def test_failed_preflight_does_not_mutate_state(self):
        build_ready(self.fixture)
        state_file = self.fixture.root / '.gsp/runs/work/state.json'
        before = state_file.read_bytes()
        result, success = dispatch_result(
            self.fixture, 'close', 'work', '--reason', 'still missing closure condition')
        self.assertFalse(success)
        self.assertEqual(result['blockers'][0]['code'], 'WORK_CLOSURE_CONDITION_REQUIRED')
        self.assertEqual(state_file.read_bytes(), before)

    def test_preflight_pass_still_rejects_tampered_evidence(self):
        build_ready(self.fixture)
        state = self.fixture.state()
        task = next(row for row in state['tasks'] if row.get('evidence'))
        relative = next(path for path in state['evidence']
                        if path.endswith('/' + task['evidence'] + '/receipt.json'))
        log = (self.fixture.root / relative).parent / 'stdout.txt'
        original = log.read_bytes()
        log.write_bytes(original + b'preflight must not hide tampering\n')
        try:
            with self.assertRaisesRegex(WorkflowError, 'EVIDENCE_TAMPERED'):
                dispatch_result(self.fixture, 'ready', 'work')
        finally:
            log.write_bytes(original)

    def test_preflight_pass_still_rejects_incomplete_tasks(self):
        build_ready(self.fixture)
        set_closure_condition(self.fixture, 'Fixture delivery was explicitly accepted')
        state_file = self.fixture.root / '.gsp/runs/work/state.json'
        state = self.fixture.project.ledger_state('work')
        state['tasks'][0]['status'] = 'running'
        self.fixture.project.write(state, 'fixture', 'incomplete task after prior ready',
                                   action_uid='fixture:incomplete-after-ready')
        with self.assertRaisesRegex(WorkflowError, 'TASKS_INCOMPLETE|CLOSE_REQUIRES_READY'):
            dispatch_result(self.fixture, 'close', 'work', '--reason', 'complete delivery')
        self.assertNotEqual(self.fixture.state()['status'], 'closed')

    def test_preflight_pass_still_rejects_head_change(self):
        build_ready(self.fixture)
        set_closure_condition(self.fixture, 'Fixture delivery was explicitly accepted')
        self.fixture.git('commit', '--allow-empty', '-m', 'outside preflight fixture')
        with self.assertRaisesRegex(WorkflowError, 'HEAD_CHANGED'):
            dispatch_result(self.fixture, 'close', 'work', '--reason', 'complete delivery')

    def test_preflight_preserves_controller_permission_gate(self):
        build_ready(self.fixture)
        state_file = self.fixture.root / '.gsp/runs/work/state.json'
        state = read(state_file)
        state['status'] = 'checking'
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        result, success = dispatch_result(self.fixture, 'ready', 'work')
        self.assertFalse(success)
        self.assertEqual(result['blockers'][0]['code'], 'CONTROLLER_REQUIRED')
        self.assertEqual(self.fixture.state()['status'], 'checking')

    def test_close_reads_closure_condition_from_work_stage_and_is_idempotent(self):
        build_ready(self.fixture)
        uuid = self.fixture.state()['work_uuid']
        self.fixture.cmd('work-stage', uuid, 'closed', '--reason',
                         'SYNTHETIC accepted delivery', '--closure-condition',
                         'Fixture evidence and delivery reviewed')
        first, success = dispatch_result(
            self.fixture, 'close', 'work', '--reason', 'release task boundary')
        self.assertTrue(success, first)
        second, success = dispatch_result(
            self.fixture, 'close', 'work', '--reason', 'repeat idempotent close')
        self.assertTrue(success, second)
        self.assertEqual(self.fixture.state()['status'], 'closed')


class DraftTaskRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = topology_lifecycle.TopologyLifecycleTests(methodName='runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_draft_task_registration_preserves_design_approval_path(self):
        self.fixture.cmd('new', 'work', '--title', 'Draft task path')
        self.fixture.cmd('task-add', 'work', 'T01', '--title', 'Draft implementation',
                         '--acceptance', 'Checks pass')
        state = self.fixture.state()
        self.assertEqual(state['status'], 'design')
        self.assertIsNone(state['design_approval'])

        self.fixture.project.document(state, 'design').write_text(
            '# Design\nDraft task registration keeps the approval path.\n', encoding='utf-8')
        self.fixture.cmd('approve', 'work', '--quote', 'SYNTHETIC design approved')
        self.assertEqual(self.fixture.state()['status'], 'plan')

        self.fixture.cmd('task-add', 'work', 'T02', '--title', 'Second task',
                         '--acceptance', 'Checks pass', '--depends', 'T01')
        self.assertEqual(self.fixture.state()['status'], 'plan')
        self.fixture.project.document(self.fixture.state(), 'plan').write_text(
            '# Plan\nT01 and T02 implementation and checks.\n', encoding='utf-8')
        self.fixture.cmd('seal-plan', 'work')
        self.fixture.cmd('advance', 'work', '--to', 'execute')
        self.assertEqual(self.fixture.state()['status'], 'execute')

    def test_task_add_after_seal_still_invalidates_plan(self):
        self.fixture.prepare()
        self.fixture.cmd('advance', 'work', '--to', 'execute')
        self.fixture.cmd('task-add', 'work', 'T02', '--title', 'Late task',
                         '--acceptance', 'Checks pass', '--depends', 'T01')
        state = self.fixture.state()
        self.assertEqual(state['status'], 'plan')
        self.assertIsNone(state['plan_seal'])


if __name__ == '__main__':
    unittest.main()
