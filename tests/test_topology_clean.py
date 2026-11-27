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
from gsp.runtime import (
    Project,
    _git_run,
    atomic,
    dispatch,
    git_identity,
    parser,
    read,
    topology_clean,
)
from gsp.topology_cleanup import clean_topology


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
        self.assertFalse((self.root / '.gsp/runs/work/topology-clean.json').exists())

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
        dispatch(self.project, parser().parse_args(['new', 'work', '--title', 'closed archive']))
        work_uid = self.project.ledger_state('work')['work_uuid']
        from gsp.documents import Governance
        book = Governance(self.root)
        archive = book.get(work_uid)
        archive['stage'] = 'closed'
        book._save(archive)
        self.write_state(work_uuid=work_uid, status='ready')

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


class RecoverableTopologyCleanTests(unittest.TestCase):
    setUp = TopologyCleanTests.setUp
    git = TopologyCleanTests.git
    branches = TopologyCleanTests.branches
    add_worktree = TopologyCleanTests.add_worktree
    populate = TopologyCleanTests.populate

    def clean(self, *, apply, git_run=None, active=None):
        return clean_topology(
            self.project,
            self.state,
            None,
            apply,
            git_run=git_run or _git_run,
            git_identity=git_identity,
            active_worktrees=active or set(),
            clock=lambda: '2026-11-27T00:00:00+00:00',
        )

    def test_branch_delete_failure_is_persisted_and_replayed_without_repeating_remove(self):
        self.populate()
        path = self.worktrees['T01']
        branch = 'feature/GSP-000099-T01-clean-merged'
        failed_once = False

        def branch_fails(cwd, *argv):
            nonlocal failed_once
            if argv[:2] == ('branch', '-d') and not failed_once:
                failed_once = True
                return subprocess.CompletedProcess(
                    ['git', *argv], 1, '', 'injected branch failure'
                )
            return _git_run(cwd, *argv)

        first = self.clean(apply=True, git_run=branch_fails)
        self.assertTrue(first['pending'])
        self.assertFalse(path.exists())
        self.assertIn(branch, self.branches())
        self.assertEqual(first['rows'][0]['pending_step'], 'branch_delete')
        saved = read(self.root / '.gsp' / 'runs' / 'work' / 'topology-clean.json')
        self.assertTrue(saved['pending'])
        self.assertEqual(saved['rows'][0]['pending_step'], 'branch_delete')

        def no_repeat_remove(cwd, *argv):
            if argv[:2] == ('worktree', 'remove'):
                raise AssertionError('verified worktree removal was repeated')
            return _git_run(cwd, *argv)

        replay = self.clean(apply=True, git_run=no_repeat_remove)
        self.assertFalse(replay['pending'])
        self.assertNotIn(branch, self.branches())
        self.assertEqual(replay['prune_error'], None)
        self.assertEqual(
            [row['path'] for row in replay['applied']], [str(path.resolve())]
        )
        self.assertFalse(
            any(row['path'] == str(path.resolve()) for row in replay['refused'])
        )

    def test_missing_worktree_requires_persisted_verified_removal_proof(self):
        missing = (self.root / '.gsp' / 'worktrees' / 'gone').resolve()
        self.tasks.append({
            'id': 'T01',
            'status': 'done',
            'topology': {
                'workspace_root': str(missing),
                'branch': 'feature/gone',
                'head': self.head,
                'base_branch': 'main',
                'effective_mode': 'worktree',
            },
        })

        unknown = self.clean(apply=False)
        self.assertTrue(unknown['pending'])
        self.assertEqual(unknown['rows'][0]['reason'], 'WORKSPACE_MISSING_NO_PROOF')
        self.assertFalse((self.root / '.gsp' / 'runs' / 'work').exists())

        proof = self.root / '.gsp' / 'runs' / 'work' / 'topology-clean.json'
        atomic(proof, {
            'schema': 1,
            'run': 'work',
            'apply': True,
            'applied': [{'path': str(missing), 'verified_absent': True}],
            'rows': [],
            'refused': [],
            'prune_error': None,
        })
        disposed = self.clean(apply=False)
        self.assertFalse(disposed['pending'])
        self.assertEqual(disposed['rows'][0]['reason'], 'ALREADY_DISPOSED')

    def test_prune_failure_keeps_pending_until_a_successful_retry(self):
        self.populate()
        path = self.worktrees['T01']
        branch = 'feature/GSP-000099-T01-clean-merged'

        def prune_fails(cwd, *argv):
            if argv == ('worktree', 'prune'):
                return subprocess.CompletedProcess(
                    ['git', *argv], 1, '', 'injected prune failure'
                )
            return _git_run(cwd, *argv)

        first = self.clean(apply=True, git_run=prune_fails)
        self.assertTrue(first['pending'])
        self.assertFalse(path.exists())
        self.assertNotIn(branch, self.branches())
        self.assertIn('injected prune failure', first['prune_error'])
        self.assertEqual(first['rows'][0]['pending_step'], 'prune')

        def no_repeat(cwd, *argv):
            if argv[:2] in {('worktree', 'remove'), ('branch', '-d')}:
                raise AssertionError('completed cleanup step was repeated')
            return _git_run(cwd, *argv)

        replay = self.clean(apply=True, git_run=no_repeat)
        self.assertFalse(replay['pending'])
        self.assertIsNone(replay['prune_error'])

    def test_missing_unknown_workspace_cannot_create_its_own_removal_proof(self):
        missing = self.root / '.gsp/worktrees/never-owned'
        self.tasks.append({'id': 'T01', 'status': 'done', 'topology': {
            'workspace_root': str(missing), 'branch': 'feature/never-owned',
            'head': self.head, 'effective_mode': 'worktree'}})
        first = self.clean(apply=True)
        self.assertTrue(first['pending'])
        self.assertFalse(first['rows'][0]['verified_absent'])
        second = self.clean(apply=True)
        self.assertTrue(second['pending'])
        self.assertEqual(second['applied'], [])

    def test_completed_prune_is_not_repeated(self):
        self.populate()
        self.clean(apply=True)
        def no_repeat(cwd, *argv):
            if argv[:2] in {('worktree', 'remove'), ('worktree', 'prune'), ('branch', '-d')}:
                raise AssertionError('completed disposal step was repeated')
            return _git_run(cwd, *argv)
        self.assertFalse(self.clean(apply=True, git_run=no_repeat)['pending'])

    def test_merged_commits_since_binding_are_a_valid_removal_candidate(self):
        self.populate()
        path = self.worktrees['T01']
        (path / 'new.py').write_text('value = 1\n', encoding='utf-8')
        self.git('add', 'new.py', cwd=path)
        self.git('commit', '-qm', 'task implementation', cwd=path)
        self.git('merge', '--ff-only', 'feature/GSP-000099-T01-clean-merged')
        result = self.clean(apply=True)
        self.assertFalse(result['pending'])
        self.assertFalse(path.exists())

    def test_preview_is_read_only_and_does_not_create_run_state(self):
        self.populate()
        run = self.root / '.gsp' / 'runs' / 'work'
        self.assertFalse(run.exists())

        result = self.clean(apply=False)

        self.assertFalse(result['pending'])
        self.assertFalse(run.exists())
        self.assertFalse((run / 'topology-clean.json').exists())
        self.assertTrue(self.worktrees['T01'].exists())


if __name__ == '__main__':
    unittest.main()
