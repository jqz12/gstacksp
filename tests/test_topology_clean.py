"""Topology cleanup is dry-run by default and refuses unsafe worktrees."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gsp.git_topology import residue
from gsp.runtime import Project, atomic, dispatch, parser, read, topology_clean


class TopologyCleanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-topology-clean-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = Project(self.root)
        self.project.initialize()
        self.git('init', '-q', '-b', 'main')
        (self.root / 'README.md').write_text('# fixture\n', encoding='utf-8')
        self.git('add', '.gitignore', 'README.md')
        self.git('commit', '-qm', 'baseline')
        self.head = self.git('rev-parse', 'HEAD')
        self.worktrees = {}
        self.tasks = []
        self.state = {
            'id': 'work', 'number': 'GSP-000099', 'title': 'topology clean',
            'status': 'closed', 'git_topology': {
                'workspace_root': str(self.root), 'enforce': True,
                'effective_mode': 'in-place', 'base_branch': 'main',
                'branch': 'main', 'head': self.head, 'status': 'selected',
            },
            'tasks': self.tasks, 'evidence': [], 'scope': {'mode': 'off'},
        }

    def git(self, *argv, cwd=None):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith('GIT_CONFIG_')}
        result = subprocess.run(
            ['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
             '-c', 'core.hooksPath=', *argv],
            cwd=cwd or self.root, capture_output=True, text=True, encoding='utf-8', env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def branches(self):
        return self.git('branch', '--format=%(refname:short)').splitlines()

    def add_worktree(self, task, name, status, mutate=None):
        path = self.root / '.gsp' / 'worktrees' / name
        branch = f'feature/GSP-000099-{task}-{name}'
        self.git('worktree', 'add', '-b', branch, str(path), 'main')
        if mutate:
            mutate(path)
        topology = {'workspace_root': str(path.resolve()), 'branch': branch,
                    'head': self.git('rev-parse', 'HEAD', cwd=path),
                    'base_branch': 'main', 'enforce': True,
                    'effective_mode': 'worktree', 'status': 'selected'}
        self.tasks.append({'id': task, 'status': status, 'topology': topology})
        self.worktrees[task] = path
        return path, branch

    def populate(self):
        self.add_worktree('T01', 'clean-merged', 'done')
        def open_commit(path):
            (path / 'feature.txt').write_text('open\n', encoding='utf-8')
            self.git('add', 'feature.txt', cwd=path)
            self.git('commit', '-qm', 'unmerged feature', cwd=path)
        self.add_worktree('T02', 'open-unmerged', 'done', open_commit)
        def dirt(path):
            (path / 'dirty.txt').write_text('uncommitted\n', encoding='utf-8')
        self.add_worktree('T03', 'dirty-tree', 'done', dirt)
        self.add_worktree('T04', 'active-tree', 'running')

    def test_dry_run_reports_actions_without_mutating(self):
        self.populate()
        before = {task: path.exists() for task, path in self.worktrees.items()}
        result = topology_clean(self.project, self.state, None, apply=False)
        actions = {row['task']: (row['action'], row['reason']) for row in result['rows']}
        self.assertEqual(actions['T01'], ('remove', 'MERGED_CLEAN'))
        self.assertEqual(actions['T02'], ('keep', 'UNMERGED'))
        self.assertEqual(actions['T03'], ('keep', 'DIRTY'))
        self.assertEqual(actions['T04'], ('keep', 'ACTIVE'))
        self.assertTrue(all(path.exists() for path in self.worktrees.values()))
        self.assertEqual(before, {task: path.exists() for task, path in self.worktrees.items()})
        self.assertEqual(result['applied'], [])
        saved = read(self.root / '.gsp' / 'runs' / 'work' / 'topology-clean.json')
        self.assertEqual(len(saved['rows']), 4)

    def test_apply_removes_only_merged_clean_inactive_worktrees(self):
        self.populate()
        result = topology_clean(self.project, self.state, None, apply=True)
        self.assertFalse(self.worktrees['T01'].exists())
        self.assertTrue(self.worktrees['T02'].exists())
        self.assertTrue(self.worktrees['T03'].exists())
        self.assertTrue(self.worktrees['T04'].exists())
        self.assertNotIn('feature/GSP-000099-T01-clean-merged', self.branches())
        self.assertEqual(len(result['applied']), 1)
        self.assertEqual({row['task'] for row in result['refused']}, {'T02', 'T03', 'T04'})

    def test_task_filter_limits_the_scan(self):
        self.populate()
        result = topology_clean(self.project, self.state, 'T02', apply=False)
        self.assertEqual([row['task'] for row in result['rows']], ['T02'])
        self.assertTrue(self.worktrees['T01'].exists())

    def write_state(self, **overrides):
        identity = self.project.identity()
        state = {
            'schema': 1, 'id': 'work', 'number': 'GSP-000099', 'title': 'missing workspace',
            'root': str(self.root), 'project_id': identity['id'], 'status': 'ready',
            'documents': {'design': 'docs/work/x/spec.md', 'plan': 'docs/work/x/plan.md'},
            'git': {'root': str(self.root), 'branch': 'main', 'head': self.head},
            'git_topology': {
                'status': 'selected', 'enforce': True, 'effective_mode': 'worktree',
                'workspace_root': str((self.root / '.gsp' / 'worktrees' / 'gone').resolve()),
                'branch': 'feature/gone', 'head': self.head, 'base_branch': 'main',
            },
            'tasks': [], 'evidence': [], 'review': None, 'scope': {'mode': 'off'},
            'design_approval': None, 'plan_seal': None, 'authorizations': [],
        }
        state.update(overrides)
        run = self.root / '.gsp' / 'runs' / 'work'
        run.mkdir(parents=True, exist_ok=True)
        atomic(run / 'state.json', state)
        return state

    def test_status_and_resume_report_missing_workspace(self):
        self.write_state()
        for command in ('status', 'resume'):
            with self.subTest(command=command):
                args = parser().parse_args([command, 'work'])
                result, ok = dispatch(self.project, args)
                self.assertTrue(ok, result)
                state = result['state'] if command == 'resume' else result
                self.assertTrue(state['workspace_missing'])
                self.assertTrue(state['workspace_recovery'])

    def test_close_reconcile_normalizes_a_closed_archive(self):
        self.write_state(work_uuid='archive-1', status='ready')

        class StubGovernance:
            def __init__(self, root):
                self.root = root
            def get(self, identity):
                return {'uuid': identity, 'stage': 'closed', 'run_id': 'work', 'documents': {}}
            def stage(self, *args, **kwargs):
                raise AssertionError('reconcile must not restage the archive')
            def _save(self, record, extra=None):
                return record
            def refresh(self, paths):
                return None

        args = parser().parse_args(['close', 'work', '--reconcile', '--reason', 'workspace gone'])
        with patch('gsp.documents.Governance', StubGovernance):
            result, ok = dispatch(self.project, args)
        self.assertTrue(ok, result)
        self.assertTrue(result['workspace_missing'])
        self.assertEqual(result['status'], 'closed')
        self.assertEqual(read(self.root / '.gsp' / 'runs' / 'work' / 'state.json')['status'], 'closed')

    def test_residue_counts_closed_merged_clean_worktrees(self):
        self.populate()
        run = self.root / '.gsp' / 'runs' / 'done'
        run.mkdir(parents=True, exist_ok=True)
        atomic(run / 'state.json', {
            'id': 'done', 'status': 'closed',
            'git_topology': {'workspace_root': str(self.worktrees['T01'].resolve()),
                             'branch': 'feature/GSP-000099-T01-clean-merged'},
            'tasks': [],
        })
        report = residue(self.root)
        self.assertGreaterEqual(report['count'], 1)
        self.assertIn(str(self.worktrees['T01'].resolve()),
                      {row['path'] for row in report['worktrees']})
        self.assertIn('topology-clean', report['command'])


if __name__ == '__main__':
    unittest.main()
