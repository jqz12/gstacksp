"""Control-plane command budgets on the real five-task lifecycle fixture."""
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from gsp.runtime import (Project, WorkflowError, current_context, dispatch,
                         parser, validate_delivery)
from gsp.validation_context import ValidationContext, activate, deactivate
from tests import test_topology_lifecycle as topology_lifecycle


def git_command(args, kwargs):
    command = args[0] if args else kwargs.get('args')
    if not isinstance(command, (list, tuple)) or not command:
        return False
    return Path(str(command[0])).stem.lower() == 'git'


class FiveTaskControlPlaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = topology_lifecycle.TopologyLifecycleTests(methodName='runTest')
        cls.fixture = fixture
        fixture.setUp()
        cls.addClassCleanup(fixture.doCleanups)
        fixture.setup_git()
        fixture.prepare(intent='full-stack')
        fixture.cmd('task-add', 'work', 'T05', '--title', 'Synthetic integration',
                    '--role', 'integration', '--acceptance', 'Fixture passes',
                    '--depends', 'T01')
        for index in range(2, 5):
            fixture.cmd('task-add', 'work', f'T{index:02}', '--title', f'Synthetic child {index}',
                        '--role', 'backend', '--owned-by', 'T05',
                        '--acceptance', 'Fixture passes')
        fixture.cmd('seal-plan', 'work')
        fixture.cmd('topology-select', 'work', '--mode', 'in-place',
                    '--quote', 'SYNTHETIC control root')
        for index in range(1, 6):
            fixture.cmd('topology-select', 'work', '--task', f'T{index:02}',
                        '--mode', 'worktree', '--quote', 'SYNTHETIC isolated child')
        fixture.cmd('advance', 'work', '--to', 'execute')
        for index in range(1, 6):
            task = f'T{index:02}'
            fixture.cmd('task-start', 'work', task)
            fixture.check(task)
            fixture.cmd('task-done', 'work', task)
        fixture.check('all')
        fixture.cmd('advance', 'work', '--to', 'review')
        document = fixture.project.paths('work')[1]
        (document / 'review.md').write_text(
            '# Review\nSynthetic five-task fixture reviewed without blocking findings.\n',
            encoding='utf-8')
        fixture.cmd('review', 'work', '--reviewer', 'self', '--blocking', '0')
        fixture.cmd('advance', 'work', '--to', 'checking')
        _, cls.advance_git_calls = cls.counted('advance', 'work', '--to', 'ready')

    @classmethod
    def counted(cls, *argv):
        calls = []
        real = subprocess.run

        def counting(*args, **kwargs):
            if git_command(args, kwargs):
                command = args[0] if args else kwargs.get('args')
                calls.append([str(value) for value in command])
            return real(*args, **kwargs)

        with patch('subprocess.run', counting):
            result = cls.fixture.cmd(*argv)
        return result, calls

    def state(self):
        return self.fixture.state()

    def test_advance_ready_git_budget(self):
        self.assertLessEqual(len(self.advance_git_calls), 45,
                             [row[:4] for row in self.advance_git_calls])

    def test_five_task_ready_status_git_budget(self):
        _, calls = self.counted('status', 'work')
        self.assertLessEqual(len(calls), 35, [row[:4] for row in calls])

    def test_status_has_no_duplicate_cache_misses_per_phase(self):
        misses = []
        real = ValidationContext.get

        def recording(self, namespace, key, loader):
            if (namespace, key) not in self._cache:
                misses.append((self.phase, namespace, key))
            return real(self, namespace, key, loader)

        with patch.object(ValidationContext, 'get', recording):
            self.fixture.cmd('status', 'work')
        self.assertEqual(len(misses), len(set(misses)))

    def test_five_task_ready_resume_git_budget(self):
        _, calls = self.counted('resume', 'work')
        self.assertLessEqual(len(calls), 35, [row[:4] for row in calls])

    def test_five_task_ready_command_git_budget(self):
        _, calls = self.counted('ready', 'work')
        self.assertLessEqual(len(calls), 45, [row[:4] for row in calls])

    def test_brief_has_zero_git_and_never_claims_validation(self):
        state_file = self.fixture.root / '.gsp/runs/work/state.json'
        before = state_file.read_bytes()

        def forbidden(*args, **kwargs):
            raise AssertionError('brief must not launch Git or scan the project')

        with patch('subprocess.run', forbidden):
            with patch.object(Project, 'state', forbidden):
                result, ok = dispatch(
                    self.fixture.project,
                    parser().parse_args(['status', 'work', '--brief']))
        self.assertTrue(ok)
        self.assertEqual(result['recorded_status'], 'ready')
        self.assertEqual(result['validation'], 'NOT_CHECKED')
        self.assertTrue(result['not_authoritative'])
        self.assertEqual(state_file.read_bytes(), before)

    def test_changed_source_rejected_by_next_ready_command(self):
        workspace = self.fixture.project.verification_root(self.state())
        source = workspace / 'source.txt'
        original = source.read_bytes()
        source.write_bytes(original + b'\nchanged between validation phases\n')
        try:
            with self.assertRaisesRegex(WorkflowError,
                                        'FINAL_REGRESSION_REQUIRED|EVIDENCE_STALE_OR_FAILED'):
                dispatch(self.fixture.project,
                         parser().parse_args(['ready', 'work']))
            self.assertEqual(self.state()['status'], 'ready')
        finally:
            source.write_bytes(original)

    def test_uncommitted_edit_between_validation_phases_rejected(self):
        project = self.fixture.project
        state = self.state()
        doc = project.paths('work')[1]
        context = ValidationContext(project, run_id='work')
        previous = activate(context)
        workspace = project.verification_root(state)
        source = workspace / 'source.txt'
        original = source.read_bytes()
        try:
            validate_delivery(project, state, doc, context=context)
            source.write_bytes(original + b'\nuncommitted edit\n')
            context.begin_phase('final')
            with self.assertRaisesRegex(
                    WorkflowError,
                    'FINAL_REGRESSION_REQUIRED|EVIDENCE_STALE_OR_FAILED|GIT_CHILD_UNCOMMITTED'):
                validate_delivery(project, state, doc, context=context)
        finally:
            source.write_bytes(original)
            deactivate(previous)

    def test_log_changed_after_initial_hash_rejected(self):
        project = self.fixture.project
        state = self.state()
        doc = project.paths('work')[1]
        task = next(row for row in state['tasks'] if row.get('evidence'))
        relative = next(path for path in state['evidence']
                        if path.endswith('/' + task['evidence'] + '/receipt.json'))
        log = (project.root / relative).parent / 'stdout.txt'
        original = log.read_bytes()
        context = ValidationContext(project, run_id='work')
        previous = activate(context)
        try:
            validate_delivery(project, state, doc, context=context)
            log.write_bytes(original + b'evidence tampered after initial hash\n')
            context.begin_phase('final')
            with self.assertRaisesRegex(WorkflowError, 'EVIDENCE_TAMPERED'):
                validate_delivery(project, state, doc, context=context)
        finally:
            log.write_bytes(original)
            deactivate(previous)

    def test_command_context_does_not_leak(self):
        self.fixture.cmd('status', 'work')
        self.assertIsNone(current_context())


class RunScanPreservationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = topology_lifecycle.TopologyLifecycleTests(methodName='runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_run_preserves_pre_and_post_scans(self):
        from gsp import runtime
        self.fixture.prepare()
        self.fixture.cmd('advance', 'work', '--to', 'execute')
        self.fixture.cmd('task-start', 'work', 'T01')
        calls = []
        real = runtime.snapshot

        def counting(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        with patch('gsp.runtime.snapshot', counting):
            receipt = self.fixture.check()
        self.assertEqual(receipt['status'], 'pass')
        self.assertGreaterEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
