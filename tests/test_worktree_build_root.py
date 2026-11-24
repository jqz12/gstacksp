"""Managed worktrees get one owned build root outside their source tree."""
import json
import os
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from gsp.dispatch import render_task_prompt
from gsp.runtime import Project, atomic
from gsp.sessions import register_session, write_brief
from gsp.worktree_lifecycle import cleanup_worktree, prepare_worktree


class WorktreeBuildRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-build-root-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.project = Project(self.root)
        self.project.initialize()
        self.git('init', '-q', '-b', 'main')
        (self.root / 'README.md').write_text('# fixture\n', encoding='utf-8')
        self.git('add', '.gitignore', 'README.md')
        self.git('commit', '-qm', 'baseline')
        head = self.git('rev-parse', 'HEAD')
        self.state = {
            'id': 'cross-session-build-root',
            'number': 'GSP-000021',
            'title': 'managed build root',
            'tasks': [
                {'id': 'T01', 'title': 'build root one', 'role': 'implementation'},
                {'id': 'T02', 'title': 'build root two', 'role': 'implementation'},
            ],
            'git_topology': {
                'workspace_root': str(self.root),
                'branch': 'main',
                'head': head,
                'base_head': head,
            },
        }

    def git(self, *argv):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith('GIT_CONFIG_')}
        result = subprocess.run(
            ['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
             '-c', 'core.hooksPath=', *argv],
            cwd=self.root, capture_output=True, text=True, encoding='utf-8', env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def task(self, task_id):
        return next(row for row in self.state['tasks'] if row['id'] == task_id)

    def dispatch(self, dispatch_id, task_id):
        return {'id': dispatch_id, 'task': task_id, 'worktree': None,
                'branch': None, 'base': None, 'claimed_paths': ['src']}

    def prepare(self, task_id):
        return prepare_worktree(self.project, self.state, self.task(task_id),
                                self.dispatch('D' + task_id, task_id))

    def build_path(self, row):
        return self.root / row['build_root']

    def test_two_worktrees_get_independent_build_roots(self):
        first = self.prepare('T01')
        second = self.prepare('T02')
        self.assertTrue(first['build_root'].startswith('.gsp/build/GSP-000021/'))
        self.assertNotEqual(first['build_root'], second['build_root'])
        for row in (first, second):
            self.assertEqual(row['build_mode'], 'managed')
            target = row['build_env']['CARGO_TARGET_DIR']
            self.assertEqual(Path(target), (self.root / row['build_root']).resolve())
            self.assertEqual(row['build_env']['BUILD_ROOT'], target)
            prompt = render_task_prompt(self.state, self.task(row['task']), row)
            self.assertIn('CARGO_TARGET_DIR', prompt)
            self.assertIn(row['build_root'], prompt)

    def test_cleanup_removes_the_build_root(self):
        row = self.prepare('T01')
        build = self.build_path(row)
        build.mkdir(parents=True, exist_ok=True)
        (build / 'artifact.bin').write_bytes(b'x' * 32)
        cleaned = cleanup_worktree(self.project, row, 'accepted')
        self.assertEqual(cleaned['result'], 'removed')
        self.assertFalse(build.exists())
        self.assertFalse(Path(row['worktree']).exists())

    def test_active_dispatch_can_keep_the_same_build_root(self):
        row = self.prepare('T01')
        build = self.build_path(row)
        build.mkdir(parents=True, exist_ok=True)
        (build / 'keep.bin').write_bytes(b'x')
        machine = self.root / '.gsp' / 'runs' / self.state['id'] / 'sessions'
        machine.mkdir(parents=True, exist_ok=True)
        atomic(machine / 'dispatch.json', {
            'task': self.state['id'],
            'dispatches': [
                {'id': 'other', 'task': 'T02', 'status': 'running',
                 'build_root': row['build_root'], 'worktree': row['worktree']},
            ],
        })
        cleaned = cleanup_worktree(self.project, row, 'accepted')
        self.assertEqual(cleaned['result'], 'removed')
        self.assertTrue(build.exists())

    def test_dirty_worktree_records_pending_build_bytes(self):
        row = self.prepare('T01')
        build = self.build_path(row)
        build.mkdir(parents=True, exist_ok=True)
        (build / 'artifact.bin').write_bytes(b'x' * 256)
        (Path(row['worktree']) / 'dirty.txt').write_text('keep me', encoding='utf-8')
        cleaned = cleanup_worktree(self.project, row, 'accepted')
        self.assertEqual(cleaned['result'], 'cleanup-pending')
        self.assertEqual(cleaned['cleanup_error']['code'], 'WORKTREE_DIRTY')
        self.assertGreaterEqual(cleaned['pending_build_bytes'], 256)

    def test_policy_can_disable_managed_build_roots(self):
        policy = self.root / '.gsp' / 'build-policy.json'
        policy.write_text(json.dumps({'worktree_build': 'off'}), encoding='utf-8')
        row = self.prepare('T01')
        self.assertIsNone(row['build_root'])
        self.assertEqual(row['build_env'], {})
        self.assertEqual(row['build_mode'], 'worktree')

    def test_session_brief_includes_the_build_environment(self):
        row = self.prepare('T01')
        register_session(self.project, self.state, Namespace(
            session='S01', role='controller', host='manual', thread=None, task=None,
            branch=None, worktree=None, write_mode='read-only', path=[], takeover=False,
            reason=''))
        register_session(self.project, self.state, Namespace(
            session='S02', role='worker', host='manual', thread=None, task='T01',
            branch=row['branch'], worktree=row['worktree'], write_mode='write',
            path=['src'], takeover=False, reason=''))
        result = write_brief(self.project, self.state, Namespace(
            session='S01', owner='S02', task='T01', title='Build root brief',
            objective='Use the managed build root.', path=[], interface=[], test=[],
            depends='', forbid='', replace=False))
        text = (self.root / result['brief']).read_text(encoding='utf-8')
        self.assertIn('CARGO_TARGET_DIR', text)
        self.assertIn(row['build_root'], text)


if __name__ == '__main__':
    unittest.main()
