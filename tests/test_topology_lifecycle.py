"""Real local repositories and receipts through the public dispatch path."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gsp.runtime import Project, WorkflowError, dispatch, parser, read
from tests.process_helpers import run


class TopologyLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-lifecycle-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = Project(self.root)
        self.project.initialize()

    def cmd(self, *argv):
        args = parser().parse_args(list(argv[:argv.index('--')])) if '--' in argv else parser().parse_args(list(argv))
        if '--' in argv:
            args.argv = list(argv[argv.index('--') + 1:])
        result, ok = dispatch(self.project, args)
        self.assertTrue(ok, result)
        return result

    def git(self, *argv, cwd=None):
        result = run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                      '-c', 'core.hooksPath=', *argv], cwd=cwd or self.root,
                     capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def setup_git(self):
        self.git('init', '-q', '-b', 'main')
        (self.root / 'source.txt').write_text('baseline', encoding='utf-8')
        self.git('add', 'source.txt', '.gitignore')
        self.git('commit', '-qm', 'baseline')

    def prepare(self, intent='single-task'):
        self.cmd('new', 'work', '--title', '拓扑恢复', '--intent', intent)
        state = self.state()
        self.project.document(state, 'design').write_text('# Design\nFixture topology lifecycle.', encoding='utf-8')
        self.cmd('approve', 'work', '--quote', 'SYNTHETIC fixture approved')
        self.cmd('task-add', 'work', 'T01', '--title', 'Implementation', '--acceptance', 'Checks pass')
        self.project.document(state, 'plan').write_text('# Plan\nT01 implementation and checks.', encoding='utf-8')
        self.cmd('seal-plan', 'work')
        self.cmd('scope', 'work', '--off', '--quote', 'SYNTHETIC no freeze')
        return state

    def state(self):
        return read(self.root / '.gsp/runs/work/state.json')

    def check(self, task='T01'):
        workspace = (self.project.verification_root(self.state()) if task == 'all'
                     else self.project.workspace_root(self.state(), task_id=task))
        program = workspace / '.gsp/fixture_test.py'
        program.parent.mkdir(exist_ok=True)
        program.write_text('import unittest\nclass Check(unittest.TestCase):\n'
                           ' def test_arithmetic(self): self.assertEqual(2 + 2, 4)\n'
                           'unittest.main()\n', encoding='utf-8')
        return self.cmd('run', 'work', '--task', task, '--kind', 'tests', '--', sys.executable, str(program))

    def test_non_git_controller_completes_without_skipping_review(self):
        self.prepare()
        self.cmd('advance', 'work', '--to', 'execute')
        self.cmd('task-start', 'work', 'T01')
        self.check()
        self.cmd('task-done', 'work', 'T01')
        self.assertFalse(self.cmd('handoff', 'work', '--to', 'review')['allowed'])
        self.check('all')
        self.assertEqual(self.state()['status'], 'execute')
        self.cmd('advance', 'work', '--to', 'review')
        doc = self.project.paths('work')[1]
        (doc / 'review.md').write_text('# Review\nFixture lifecycle reviewed; no blocking findings.', encoding='utf-8')
        self.cmd('review', 'work', '--reviewer', 'self', '--blocking', '0')
        self.cmd('advance', 'work', '--to', 'checking')
        (self.root / 'late.txt').write_text('changed input', encoding='utf-8')
        blocked = self.cmd('handoff', 'work', '--to', 'ready')
        self.assertFalse(blocked['allowed'])
        self.assertEqual(self.state()['status'], 'checking')
        (self.root / 'late.txt').unlink()
        self.cmd('advance', 'work', '--to', 'ready')
        self.cmd('ready', 'work')
        self.assertEqual(self.cmd('status', 'work')['status'], 'ready')
        self.assertIn('READY', (doc / 'delivery.md').read_text(encoding='utf-8'))

    def test_worktree_run_resume_reuse_and_deleted_workspace(self):
        self.setup_git()
        self.prepare()
        self.cmd('topology-select', 'work', '--mode', 'worktree', '--quote', 'SYNTHETIC isolate')
        binding = self.state()['git_topology']
        self.cmd('topology-select', 'work', '--mode', 'worktree', '--quote', 'SYNTHETIC reuse same task')
        self.assertEqual(self.state()['git_topology']['workspace_root'], binding['workspace_root'])
        self.cmd('advance', 'work', '--to', 'execute')
        self.cmd('task-start', 'work', 'T01')
        receipt = self.check()
        self.assertEqual(receipt['workspace_root'], binding['workspace_root'])
        self.cmd('resume', 'work')
        self.git('worktree', 'remove', '--force', binding['workspace_root'])
        with self.assertRaisesRegex(WorkflowError, 'GIT_WORKSPACE_MISSING'):
            self.cmd('resume', 'work')

    def test_new_branch_cannot_displace_another_live_task(self):
        self.setup_git()
        self.prepare()
        self.cmd('topology-select', 'work', '--mode', 'in-place', '--quote', 'SYNTHETIC main')
        self.cmd('new', 'other', '--title', '另外任务')
        with self.assertRaisesRegex(WorkflowError, 'GIT_WORKTREE_CONFLICT'):
            self.cmd('topology-select', 'other', '--mode', 'branch', '--quote', 'SYNTHETIC independent branch')
        self.assertEqual(self.git('branch', '--show-current'), 'main')

    def test_worktree_records_parent_branch_and_rejects_changed_base_on_reuse(self):
        self.setup_git()
        self.git('switch', '-c', 'foundation')
        (self.root / 'parent.txt').write_text('parent feature', encoding='utf-8')
        self.git('add', 'parent.txt')
        self.git('commit', '-qm', 'parent feature')
        parent_head = self.git('rev-parse', 'HEAD')
        self.git('switch', 'main')
        self.prepare()
        self.cmd('topology-select', 'work', '--mode', 'worktree', '--base', 'foundation',
                 '--quote', 'SYNTHETIC branch from parent feature')
        binding = self.state()['git_topology']
        self.assertEqual(binding['base_head'], parent_head)
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=Path(binding['workspace_root'])), parent_head)
        self.assertEqual((Path(binding['workspace_root']) / 'parent.txt').read_text(encoding='utf-8'),
                         'parent feature')
        with self.subTest('parent name describes the explicit base'):
            self.assertEqual(binding['base_branch'], 'foundation')
        self.assertTrue(self.cmd('topology-select', 'work', '--mode', 'worktree', '--base', 'foundation',
                                 '--quote', 'SYNTHETIC same base')['reused'])
        state_file = self.root / '.gsp/runs/work/state.json'
        before = state_file.read_bytes()
        with self.subTest('changed base requires explicit rebind'):
            with self.assertRaisesRegex(WorkflowError, 'GIT_REBIND_REQUIRED'):
                self.cmd('topology-select', 'work', '--mode', 'worktree', '--base', 'main',
                         '--quote', 'SYNTHETIC changed base')
            self.assertEqual(state_file.read_bytes(), before)

    def test_branch_sha_base_does_not_claim_current_branch_as_parent(self):
        self.setup_git()
        base = self.git('rev-parse', 'HEAD')
        self.prepare()
        self.cmd('topology-select', 'work', '--mode', 'branch', '--base', base,
                 '--quote', 'SYNTHETIC exact commit base')
        binding = self.state()['git_topology']
        self.assertEqual(binding['base_head'], base)
        self.assertIsNone(binding['base_branch'])

    def test_worktree_child_resolves_same_control_root(self):
        self.setup_git()
        self.prepare()
        self.cmd('topology-select', 'work', '--mode', 'worktree', '--quote', 'SYNTHETIC isolate')
        workspace = Path(self.state()['git_topology']['workspace_root'])
        child_project = Project(workspace)
        self.assertEqual(child_project.root, self.root)
        self.assertEqual(child_project.identity()['id'], self.project.identity()['id'])

    def test_health_requires_topology_and_rechecks_deleted_child_read_only(self):
        from gsp.document_health import git_route
        self.setup_git()
        self.prepare()
        report = git_route(self.root)
        self.assertEqual(report['route_id'], 'git.topology.required')
        self.assertTrue(report['blocking'])
        self.cmd('topology-select', 'work', '--mode', 'in-place', '--quote', 'SYNTHETIC control')
        self.cmd('topology-select', 'work', '--task', 'T01', '--mode', 'worktree', '--quote', 'SYNTHETIC child')
        path = self.state()['tasks'][0]['topology']['workspace_root']
        state_file = self.root / '.gsp/runs/work/state.json'
        before = state_file.read_bytes()
        self.assertFalse(git_route(self.root)['blocking'])
        self.git('worktree', 'remove', '--force', path)
        report = git_route(self.root)
        self.assertTrue(report['blocking'])
        self.assertIn('GIT_WORKSPACE_MISSING', [r['code'] for r in report['findings']])
        self.assertEqual(state_file.read_bytes(), before)
        with self.assertRaisesRegex(WorkflowError, 'GIT_WORKSPACE_MISSING'):
            self.cmd('status', 'work')

    def test_broken_git_cannot_be_adopted_as_non_git(self):
        from gsp.git_topology import probe
        from gsp.document_health import git_route
        self.setup_git()
        (self.root / '.git/HEAD').write_text('invalid HEAD', encoding='utf-8')
        inventory = probe(self.root)
        self.assertEqual(inventory['status'], 'blocked')
        self.assertIn('GIT_PROBE_FAILED', [r['code'] for r in inventory['risks']])
        self.assertEqual(git_route(self.root)['status'], 'unknown')
        self.prepare()
        self.assertTrue(self.state()['git_topology']['enforce'])
        with self.assertRaisesRegex(WorkflowError, 'GIT_TOPOLOGY_REQUIRED'):
            self.cmd('task-start', 'work', 'T01')

    def test_existing_branch_switch_interruption_preserves_inflight(self):
        from gsp import runtime
        self.setup_git()
        self.prepare()
        branch = 'feature/' + self.state()['number'] + '-恢复已有分支'
        self.git('branch', branch)
        original = runtime._git_run
        def interrupt_after_switch(root, *argv):
            result = original(root, *argv)
            if argv == ('switch', branch):
                raise KeyboardInterrupt('fixture after Git completed')
            return result
        with patch.object(runtime, '_git_run', side_effect=interrupt_after_switch):
            with self.assertRaises(KeyboardInterrupt):
                self.cmd('topology-select', 'work', '--mode', 'branch', '--branch', branch, '--quote', 'SYNTHETIC choose existing')
        self.assertEqual(self.state()['inflight']['kind'], 'topology')
        self.assertEqual(self.git('branch', '--show-current'), branch)
        recovery = self.project.paths('work')[1] / 'recovery.md'
        recovery.write_text('# Recovery\nGit switch completed; no process remains. Rebind after invalidating previous checks.', encoding='utf-8')
        self.cmd('recover-run', 'work', '--evidence', str(recovery), '--outcome', 'failed')
        self.cmd('topology-select', 'work', '--mode', 'branch', '--branch', branch, '--rebind', '--quote', 'SYNTHETIC inspected recovery')
        self.assertNotIn('inflight', self.state())
        self.assertEqual(self.state()['git_topology']['branch'], branch)

    def test_driver_rejects_bound_worktree_before_dispatch_or_check(self):
        from gsp.driver import Driver
        self.setup_git()
        self.prepare()
        self.cmd('topology-select', 'work', '--mode', 'worktree', '--quote', 'SYNTHETIC Native')
        driver = Driver(self.project, 'work')
        with patch.object(driver, 'run_owned_check') as runner:
            with self.assertRaisesRegex(WorkflowError, 'DRIVER_TOPOLOGY_UNSUPPORTED'):
                driver.validate_workflow()
            with self.assertRaisesRegex(WorkflowError, 'DRIVER_TOPOLOGY_UNSUPPORTED'):
                driver.check(self.state(), 'T01')
            runner.assert_not_called()

    def test_child_branch_must_include_its_own_local_task_number(self):
        self.setup_git()
        self.prepare()
        self.cmd('topology-select', 'work', '--mode', 'in-place', '--quote', 'SYNTHETIC control')
        wrong = 'feature/' + self.state()['number'] + '-T02-错误子任务'
        with self.assertRaisesRegex(WorkflowError, 'GIT_BRANCH_TASK_MISMATCH'):
            self.cmd('topology-select', 'work', '--task', 'T01', '--mode', 'worktree',
                     '--branch', wrong, '--quote', 'SYNTHETIC invalid child number')
        self.assertNotIn(wrong, self.git('branch', '--list'))

    def test_duplicate_numeric_task_alias_is_rejected(self):
        self.prepare()
        with self.assertRaisesRegex(WorkflowError, 'TASK_EXISTS'):
            self.cmd('task-add', 'work', 'T1', '--title', 'duplicate T01', '--acceptance', 'never created')

    def test_explicit_rebind_can_recover_missing_ready_workspace(self):
        self.setup_git()
        self.prepare()
        self.cmd('topology-select', 'work', '--mode', 'worktree', '--quote', 'SYNTHETIC isolate')
        state = self.state()
        path = state['git_topology']['workspace_root']
        # Fault fixture: a historical READY binding whose directory disappears.
        # This intentionally tests recovery loading, not proof of delivery.
        from gsp.runtime import atomic
        state['status'] = 'ready'
        atomic(self.root / '.gsp/runs/work/state.json', state)
        self.git('worktree', 'remove', '--force', path)
        self.cmd('topology-select', 'work', '--mode', 'worktree', '--rebind',
                 '--quote', 'SYNTHETIC inspected deleted workspace and existing branch')
        self.assertEqual(self.state()['status'], 'execute')
        self.assertEqual(self.state().get('verification_epoch', 0), 0)

    def test_two_sessions_keep_separate_frozen_tasks_and_hook_context(self):
        from gsp.codex_hooks import handle
        from gsp.document_health import git_route
        self.setup_git()
        self.prepare()
        self.cmd('topology-select', 'work', '--mode', 'in-place', '--quote', 'SYNTHETIC main writer')
        self.cmd('scope', 'work', '--path', '.', '--quote', 'SYNTHETIC main freeze')
        original = self.state()['scope']
        self.cmd('new', 'other', '--title', '第二会话任务', '--intent', 'cross-session', '--size', 'large')
        self.cmd('topology-select', 'other', '--mode', 'worktree', '--quote', 'SYNTHETIC second isolated writer')
        other = read(self.root / '.gsp/runs/other/state.json')
        workspace = Path(other['git_topology']['workspace_root'])
        self.cmd('scope', 'other', '--path', '.', '--quote', 'SYNTHETIC second freeze')
        self.cmd('session-register', 'other', '--role', 'controller', '--host', 'codex',
                 '--thread', 'other-main', '--worktree', str(workspace),
                 '--write-mode', 'write', '--path', '.')
        self.assertEqual(self.state()['scope'], original)
        for cwd, expected in ((self.root, 'work'), (workspace, 'other')):
            result = handle({'cwd': str(cwd), 'hook_event_name': 'SessionStart', 'session_id': expected})
            self.assertIn('Active task ' + expected + ',', result['hookSpecificOutput']['additionalContext'])
            result = handle({'cwd': str(cwd), 'hook_event_name': 'PreToolUse', 'session_id': expected,
                             'gsp_session_id': 'S01' if expected == 'other' else None,
                             'tool_name': 'Edit', 'tool_input': {'file_path': str(cwd / 'source.txt')}})
            self.assertNotEqual(result.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
            self.assertEqual(git_route(cwd)['active_task'], expected)
        denied = handle({'cwd': str(self.root), 'hook_event_name': 'PreToolUse', 'session_id': 'work',
                         'tool_name': 'Edit', 'tool_input': {'file_path': str(workspace / 'source.txt')}})
        self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')
        with self.assertRaisesRegex(WorkflowError, 'GIT_WORKSPACE'):
            self.cmd('check-path', 'work', str(workspace / 'source.txt'))

    def test_fullstack_uses_integration_tree_and_checks_child_commits(self):
        self.setup_git()
        self.prepare(intent='full-stack')
        self.cmd('task-add', 'work', 'T02', '--title', '前端', '--role', 'frontend', '--acceptance', 'UI checks')
        self.cmd('task-add', 'work', 'T03', '--title', '集成', '--role', 'integration',
                 '--acceptance', 'Integration checks', '--depends', 'T01,T02')
        self.cmd('seal-plan', 'work')
        self.cmd('topology-select', 'work', '--mode', 'in-place', '--quote', 'SYNTHETIC control only')
        for task in ('T01', 'T02', 'T03'):
            self.cmd('topology-select', 'work', '--task', task, '--mode', 'worktree', '--quote', 'SYNTHETIC independent child')
        self.cmd('advance', 'work', '--to', 'execute')
        for task in ('T01', 'T02'):
            self.cmd('task-start', 'work', task)
            workspace = self.project.workspace_root(self.state(), task_id=task)
            (workspace / (task + '.txt')).write_text(task, encoding='utf-8')
            self.git('add', task + '.txt', cwd=workspace)
            self.git('commit', '-qm', task, cwd=workspace)
            self.cmd('observe-head', 'work', '--task', task, '--reason', 'SYNTHETIC inspect child commit')
            self.check(task)
            self.cmd('task-done', 'work', task)
        self.cmd('task-start', 'work', 'T03')
        integration = self.project.workspace_root(self.state(), task_id='T03')
        with self.assertRaisesRegex(WorkflowError, 'GIT_INTEGRATION_'):
            self.check('T03')
        children = self.state()['tasks'][:2]
        for child in children:
            self.git('merge', '--no-edit', child['topology']['branch'], cwd=integration)
        self.cmd('observe-head', 'work', '--task', 'T03', '--reason', 'SYNTHETIC inspected merged commits')
        self.check('T03')
        self.cmd('task-done', 'work', 'T03')
        final = self.check('all')
        self.assertEqual(Path(final['workspace_root']), integration)
        self.assertEqual(final['git']['head'], self.git('rev-parse', 'HEAD', cwd=integration))
        for task in ('T01', 'T02'):
            self.assertEqual((integration / (task + '.txt')).read_text(encoding='utf-8'), task)
        self.assertFalse((self.root / 'T01.txt').exists())
        self.cmd('advance', 'work', '--to', 'review')
        doc = self.project.paths('work')[1]
        (doc / 'review.md').write_text('# Review\nIntegrated frontend and backend fixture; no blockers.', encoding='utf-8')
        self.cmd('review', 'work', '--reviewer', 'self', '--blocking', '0')
        self.cmd('advance', 'work', '--to', 'checking')
        self.cmd('advance', 'work', '--to', 'ready')
        self.cmd('ready', 'work')
        self.assertEqual(self.cmd('status', 'work')['status'], 'ready')
        child_workspace = Path(children[0]['topology']['workspace_root'])
        (child_workspace / 'T01.txt').write_text('unverified after integration', encoding='utf-8')
        self.assertEqual(self.cmd('status', 'work')['status'], 'needs-verification')
        with self.assertRaisesRegex(WorkflowError, 'GIT_CHILD|EVIDENCE_STALE'):
            self.check('all')

    def test_check_path_rejects_source_edits_in_control_root(self):
        self.setup_git()
        self.prepare()
        self.cmd('topology-select', 'work', '--mode', 'worktree', '--quote', 'SYNTHETIC isolate')
        with self.assertRaisesRegex(WorkflowError, 'GIT_WORKSPACE_PATH'):
            self.cmd('check-path', 'work', str(self.root / 'source.txt'))


if __name__ == '__main__':
    unittest.main()
