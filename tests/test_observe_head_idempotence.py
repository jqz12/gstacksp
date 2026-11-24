"""observe-head is a no-op for unchanged identity and still invalidates real changes."""
import sys
import tempfile
import unittest
from pathlib import Path

from gsp.runtime import Project, WorkflowError, dispatch, head_observation_change, parser, read
from tests.process_helpers import run as run_process


class HeadObservationUnitTests(unittest.TestCase):
    def test_unchanged_identity_is_no_change(self):
        identity = {'root': 'root', 'branch': 'branch', 'head': 'head'}
        result = head_observation_change(identity, dict(identity), {'checks': {'evidence': True}})
        self.assertFalse(result['identity_changed'])
        self.assertFalse(result['verification_changed'], result)
        self.assertTrue(result['preserve_content_evidence'])
        self.assertEqual(result['reason'], 'no-change')

    def test_head_only_change_preserves_reverified_content(self):
        previous = {'root': 'root', 'branch': 'branch', 'head': 'old'}
        current = {'root': 'root', 'branch': 'branch', 'head': 'new'}
        result = head_observation_change(previous, current, {'checks': {'evidence': True}})
        self.assertTrue(result['identity_changed'])
        self.assertFalse(result['verification_changed'], result)
        self.assertTrue(result['preserve_content_evidence'])
        self.assertEqual(result['reason'], 'identity-changed-content-preserved')

    def test_binding_or_verification_change_invalidates(self):
        previous = {'root': 'root', 'branch': 'branch', 'head': 'same'}
        branch = head_observation_change(
            previous, {'root': 'root', 'branch': 'other', 'head': 'same'},
            {'checks': {'evidence': True}})
        self.assertTrue(branch['identity_changed'])
        self.assertTrue(branch['verification_changed'])
        self.assertFalse(branch['preserve_content_evidence'])

        stale = head_observation_change(previous, dict(previous),
                                        {'checks': {'contract': False, 'entry': False}})
        self.assertFalse(stale['identity_changed'])
        self.assertTrue(stale['verification_changed'])
        self.assertIn('contract', stale['reason'])
        self.assertIn('entry', stale['reason'])


class ObserveHeadIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-observe-head-')
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
        result = run_process(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                              '-c', 'core.hooksPath=', *argv], cwd=cwd or self.root,
                             capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def state(self):
        return read(self.root / '.gsp/runs/work/state.json')

    def state_bytes(self):
        return (self.root / '.gsp/runs/work/state.json').read_bytes()

    def setup_git(self):
        self.git('init', '-q', '-b', 'main')
        (self.root / 'source.txt').write_text('baseline', encoding='utf-8')
        self.git('add', 'source.txt', '.gitignore')
        self.git('commit', '-qm', 'baseline')

    def prepare(self):
        self.cmd('new', 'work', '--title', '观察幂等', '--intent', 'single-task')
        state = self.state()
        self.project.document(state, 'design').write_text('# Design\nFixture observe-head.', encoding='utf-8')
        self.cmd('approve', 'work', '--quote', 'SYNTHETIC fixture approved')
        self.cmd('task-add', 'work', 'T01', '--title', 'Implementation', '--acceptance', 'Checks pass')
        self.project.document(state, 'plan').write_text('# Plan\nT01 implementation and checks.', encoding='utf-8')
        self.cmd('seal-plan', 'work')
        self.cmd('scope', 'work', '--off', '--quote', 'SYNTHETIC no freeze')
        self.cmd('topology-select', 'work', '--mode', 'in-place', '--quote', 'SYNTHETIC control writer')

    def check(self, task='T01'):
        workspace = (self.project.verification_root(self.state()) if task == 'all'
                     else self.project.workspace_root(self.state(), task_id=task))
        program = workspace / '.gsp/fixture_test.py'
        program.parent.mkdir(exist_ok=True)
        program.write_text('import unittest\nclass Check(unittest.TestCase):\n'
                           ' def test_arithmetic(self): self.assertEqual(2 + 2, 4)\n'
                           'unittest.main()\n', encoding='utf-8')
        return self.cmd('run', 'work', '--task', task, '--kind', 'tests', '--', sys.executable, str(program))

    def finish_ready(self):
        self.cmd('advance', 'work', '--to', 'execute')
        self.cmd('task-start', 'work', 'T01')
        self.check('T01')
        self.cmd('task-done', 'work', 'T01')
        self.check('all')
        self.cmd('advance', 'work', '--to', 'review')
        doc = self.project.paths('work')[1]
        (doc / 'review.md').write_text('# Review\nFixture observe-head reviewed; no blockers.', encoding='utf-8')
        self.cmd('review', 'work', '--reviewer', 'self', '--blocking', '0')
        self.cmd('advance', 'work', '--to', 'checking')
        self.cmd('advance', 'work', '--to', 'ready')
        self.cmd('ready', 'work')
        self.assertEqual(self.state()['status'], 'ready')

    def test_parent_same_head_is_noop_and_contract_change_invalidates(self):
        self.setup_git()
        self.prepare()
        self.finish_ready()
        before = self.state()
        raw = self.state_bytes()

        result = self.cmd('observe-head', 'work', '--reason', 'SYNTHETIC unchanged observation')

        self.assertFalse(result['changed'])
        self.assertEqual(result['reason'], 'no-change')
        self.assertEqual(self.state_bytes(), raw)
        self.assertEqual(self.state()['review'], before['review'])
        self.assertEqual(self.state()['tasks'][0]['evidence'], before['tasks'][0]['evidence'])
        self.assertEqual(self.state().get('verification_epoch', 0),
                         before.get('verification_epoch', 0))

        plan = self.project.document(self.state(), 'plan')
        plan.write_text(plan.read_text(encoding='utf-8') + '\nContract change.\n', encoding='utf-8')
        result = self.cmd('observe-head', 'work', '--reason', 'SYNTHETIC contract changed')
        state = self.state()
        self.assertFalse(result['changed'])
        self.assertEqual(state['status'], 'needs-verification')
        self.assertEqual(state['tasks'][0]['evidence'], before['tasks'][0]['evidence'])
        self.assertEqual(state.get('verification_epoch', 0), before.get('verification_epoch', 0))

    def child_ready(self):
        self.setup_git()
        self.prepare()
        self.cmd('topology-select', 'work', '--task', 'T01', '--mode', 'worktree',
                 '--quote', 'SYNTHETIC child isolation')
        binding = self.state()['tasks'][0]['topology']
        workspace = Path(binding['workspace_root'])
        self.git('switch', '-c', 'other-branch', cwd=workspace)
        with self.assertRaisesRegex(WorkflowError, 'GIT_WORKSPACE_MISMATCH'):
            self.cmd('observe-head', 'work', '--task', 'T01', '--reason', 'SYNTHETIC wrong branch')
        self.git('switch', binding['branch'], cwd=workspace)
        self.finish_ready()
        return workspace

    def test_child_same_head_preserves_review_and_ready(self):
        workspace = self.child_ready()
        before = self.state()
        raw = self.state_bytes()
        history = list(before['tasks'][0]['topology'].get('head_history') or [])

        result = self.cmd('observe-head', 'work', '--task', 'T01', '--reason', 'SYNTHETIC same child head')

        self.assertFalse(result['changed'])
        self.assertFalse(result['identity_changed'])
        self.assertEqual(self.state_bytes(), raw)
        self.assertEqual(self.state()['status'], 'ready')
        self.assertEqual(self.state()['review'], before['review'])
        self.assertEqual(self.state()['tasks'][0]['evidence'], before['tasks'][0]['evidence'])
        self.assertEqual(self.state()['tasks'][0]['topology'].get('head_history') or [], history)

        previous_head = self.state()['tasks'][0]['topology']['head']
        self.git('commit', '--allow-empty', '-qm', 'empty identity change', cwd=workspace)
        new_head = self.git('rev-parse', 'HEAD', cwd=workspace)
        result = self.cmd('observe-head', 'work', '--task', 'T01',
                          '--reason', 'SYNTHETIC empty commit')
        state = self.state()
        self.assertTrue(result['changed'])
        self.assertTrue(result['identity_changed'])
        self.assertFalse(result['verification_changed'], result)
        self.assertEqual(state['status'], 'ready')
        self.assertEqual(state['review'], before['review'])
        self.assertEqual(state['tasks'][0]['evidence'], before['tasks'][0]['evidence'])
        self.assertEqual(state['tasks'][0]['topology']['head'], new_head)
        self.assertNotEqual(state['tasks'][0]['topology']['head'], previous_head)
        self.assertEqual(len(state['tasks'][0]['topology']['head_history']), len(history) + 1)

        (workspace / 'changed.txt').write_text('real content change', encoding='utf-8')
        self.git('add', 'changed.txt', cwd=workspace)
        self.git('commit', '-qm', 'content change', cwd=workspace)
        result = self.cmd('observe-head', 'work', '--task', 'T01',
                          '--reason', 'SYNTHETIC content change')
        state = self.state()
        self.assertTrue(result['verification_changed'], result)
        self.assertFalse(result['preserve_content_evidence'])
        self.assertEqual(state['status'], 'needs-verification')
        self.assertIsNone(state['review'])
        self.assertEqual(state['tasks'][0]['evidence'], before['tasks'][0]['evidence'])

    def test_child_entry_change_invalidates_ready(self):
        workspace = self.child_ready()
        before = self.state()

        program = workspace / '.gsp/fixture_test.py'
        program.write_text(program.read_text(encoding='utf-8') + '\n# changed entry\n', encoding='utf-8')
        result = self.cmd('observe-head', 'work', '--task', 'T01',
                          '--reason', 'SYNTHETIC entry script changed')

        state = self.state()
        self.assertTrue(result['verification_changed'], result)
        self.assertEqual(state['status'], 'needs-verification')
        self.assertIsNone(state['review'])
        self.assertEqual(state['tasks'][0]['evidence'], before['tasks'][0]['evidence'])


if __name__ == '__main__':
    unittest.main()
