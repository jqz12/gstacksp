"""Git probe budget and identity compatibility, measured on real repositories."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gsp.git_topology import probe
from gsp.runtime import Project, dispatch, git_identity, parser
from tests.process_helpers import run


class GitProbeCostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-probe-cost-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = Project(self.root)
        self.project.initialize()
        self.git('init', '-q', '-b', 'main')
        (self.root / 'source.txt').write_text('baseline', encoding='utf-8')
        self.git('add', 'source.txt')
        self.git('commit', '-qm', 'baseline')

    def git(self, *argv, cwd=None):
        result = run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                      '-c', 'core.hooksPath=', *argv], cwd=cwd or self.root,
                     capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def cmd(self, *argv):
        result, ok = dispatch(self.project, parser().parse_args(list(argv)))
        self.assertTrue(ok, result)
        return result

    def state(self):
        import json
        return json.loads((self.root / '.gsp/runs/work/state.json').read_text(encoding='utf-8'))

    def prepare_worktree_task(self):
        self.cmd('new', 'work', '--title', '探针成本', '--intent', 'serial-multi-task', '--size', 'large')
        state = self.state()
        self.project.document(state, 'design').write_text('# Design\nFixture probe budget.', encoding='utf-8')
        self.cmd('approve', 'work', '--quote', 'SYNTHETIC approved')
        self.cmd('task-add', 'work', 'T01', '--title', '探针', '--acceptance', 'checks pass')
        self.project.document(state, 'plan').write_text('# Plan\nT01 probe budget.', encoding='utf-8')
        self.cmd('seal-plan', 'work')
        self.cmd('scope', 'work', '--off', '--quote', 'SYNTHETIC no freeze')
        self.cmd('topology-select', 'work', '--mode', 'worktree', '--quote', 'SYNTHETIC isolated child')
        self.cmd('advance', 'work', '--to', 'execute')
        self.cmd('task-start', 'work', 'T01')
        return Path(self.state()['git_topology']['workspace_root'])

    def test_state_git_call_budget(self):
        workspace = self.prepare_worktree_task()
        calls = []
        real = subprocess.run

        def counting(*args, **kwargs):
            command = args[0] if args else kwargs.get('args')
            if isinstance(command, (list, tuple)) and command and command[0] == 'git':
                calls.append(list(command))
            return real(*args, **kwargs)

        with patch('subprocess.run', counting):
            state, _, _ = self.project.state('work')
        self.assertLessEqual(len(calls), 4, calls)
        self.assertEqual(state['git']['root'], str(workspace.resolve()))
        self.assertEqual(state['git']['head'], self.git('rev-parse', 'HEAD', cwd=workspace))
        self.assertEqual(state['git']['branch'], self.state()['git_topology']['branch'])

    def test_git_identity_handles_unborn_detached_and_non_git(self):
        with tempfile.TemporaryDirectory(prefix='gsp-probe-unborn-') as folder:
            root = Path(folder)
            run(['git', 'init', '-q', '-b', 'main'], cwd=root, capture_output=True, text=True)
            self.assertEqual(git_identity(root),
                             {'root': str(root.resolve()), 'branch': 'main', 'head': None})
        with tempfile.TemporaryDirectory(prefix='gsp-probe-detached-') as folder:
            root = Path(folder)
            self.git('init', '-q', '-b', 'main', cwd=root)
            (root / 'a.txt').write_text('x', encoding='utf-8')
            self.git('add', 'a.txt', cwd=root)
            self.git('commit', '-qm', 'init', cwd=root)
            head = self.git('rev-parse', 'HEAD', cwd=root)
            self.git('checkout', '-q', '--detach', cwd=root)
            self.assertEqual(git_identity(root),
                             {'root': str(root.resolve()), 'branch': None, 'head': head})
        with tempfile.TemporaryDirectory(prefix='gsp-probe-plain-') as folder:
            self.assertEqual(git_identity(Path(folder)),
                             {'root': None, 'branch': None, 'head': None})

    def test_probe_current_matches_identity_and_can_skip_branches(self):
        (self.root / 'source.txt').write_text('changed', encoding='utf-8')
        identity = git_identity(self.root)
        inventory = probe(self.root, include_branches=False)
        self.assertEqual(inventory['branches'], [])
        self.assertEqual(inventory['current']['root'], identity['root'])
        self.assertEqual(inventory['current']['branch'], identity['branch'])
        self.assertEqual(inventory['current']['head'], identity['head'])
        self.assertEqual(inventory['current']['source_changed_paths'], ['source.txt'])


if __name__ == '__main__':
    unittest.main()
