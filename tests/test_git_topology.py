import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gsp.git_topology import branch_name, probe, recommend, validate_branch_name
from gsp.runtime import WorkflowError


class GitTopologyTests(unittest.TestCase):
    def repo(self):
        temp = tempfile.TemporaryDirectory(prefix='gsp-topology-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=root, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Topology Test'], cwd=root, check=True)
        subprocess.run(['git', 'config', 'user.email', 'topology@example.invalid'], cwd=root, check=True)
        (root / 'README.md').write_text('# topology\n', encoding='utf-8')
        subprocess.run(['git', 'add', 'README.md'], cwd=root, check=True)
        subprocess.run(['git', 'commit', '-qm', 'chore: baseline'], cwd=root, check=True)
        return root

    def git(self, root, *args):
        return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True).stdout

    def test_probe_reports_current_branch_worktree_and_clean_status(self):
        root = self.repo()

        result = probe(root)

        self.assertEqual(result['status'], 'clean')
        self.assertEqual(result['current']['branch'], 'main')
        self.assertEqual(result['current']['root'], str(root.resolve()))
        self.assertEqual(len(result['worktrees']), 1)
        self.assertEqual(result['worktrees'][0]['path'], str(root.resolve()))
        self.assertEqual(result['active_tasks'], [])
        self.assertEqual(result['risks'], [])

    def test_probe_reports_dirty_and_untracked_paths(self):
        root = self.repo()
        (root / 'README.md').write_text('changed\n', encoding='utf-8')
        (root / 'new.txt').write_text('new\n', encoding='utf-8')

        result = probe(root)

        self.assertEqual(result['status'], 'needs-decision')
        self.assertIn('GIT_DIRTY', {item['code'] for item in result['risks']})
        self.assertEqual(result['current']['changed_paths'], ['README.md', 'new.txt'])

    def test_probe_preserves_unicode_spaces_and_rename_paths(self):
        root = self.repo()
        self.git(root, 'mv', 'README.md', '中文 文档.md')
        (root / '箭头 → 文件.txt').write_text('new', encoding='utf-8')
        self.assertEqual(probe(root)['current']['changed_paths'],
                         ['README.md', '中文 文档.md', '箭头 → 文件.txt'])

    def test_git_missing_and_worktree_probe_failure_are_explicit(self):
        root = self.repo()
        with patch('gsp.git_topology.subprocess.run', side_effect=FileNotFoundError('git')):
            self.assertIn('GIT_UNAVAILABLE', {x['code'] for x in probe(root)['risks']})
        from gsp import git_topology
        original = git_topology._run
        def fail_worktrees(path, *args):
            if args[:2] == ('worktree', 'list'):
                return subprocess.CompletedProcess(args, 128, '', 'fixture worktree failure')
            return original(path, *args)
        with patch.object(git_topology, '_run', side_effect=fail_worktrees):
            result = probe(root)
        self.assertIn('GIT_PROBE_FAILED', {x['code'] for x in result['risks']})
        self.assertEqual(result['status'], 'blocked')

    def test_historical_states_are_visible_without_claiming_live_workspaces(self):
        root = self.repo()
        result = probe(root, active_tasks=[
            {'id': 'old-a', 'status': 'ready', 'git': {'root': str(root), 'branch': 'main'}},
            {'id': 'old-b', 'status': 'needs-verification', 'git': {'root': str(root), 'branch': 'main'}},
        ])
        self.assertEqual(len(result['active_tasks']), 2)
        self.assertNotIn('GIT_WORKTREE_CONFLICT', {x['code'] for x in result['risks']})

    def test_cross_session_single_in_place_retains_writer_claim(self):
        root = self.repo()
        state = {'id': 'continued', 'tasks': [{'id': 'T01', 'role': 'implementation'}],
                 'git_topology': {'intent': 'cross-session', 'effective_mode': 'in-place',
                                  'branch': 'main', 'workspace_root': str(root)}}
        result = probe(root, active_tasks=[state])
        self.assertTrue(result['active_tasks'][0]['claiming'])

    def test_probe_discovers_child_bindings_and_all_local_branches(self):
        root = self.repo()
        self.git(root, 'branch', 'feature/GSP-000018-中文分支')
        binding = {'effective_mode': 'worktree', 'workspace_root': str(root), 'branch': 'main'}
        result = probe(root, active_tasks=[{'id': 'parent', 'tasks': [
            {'id': 'T01', 'role': 'backend', 'topology': binding},
            {'id': 'T02', 'role': 'frontend', 'topology': binding},
        ]}])
        self.assertIn('GIT_WORKTREE_CONFLICT', {x['code'] for x in result['risks']})
        self.assertIn('feature/GSP-000018-中文分支', [x['name'] for x in result['branches']])

    def test_probe_reports_detached_head_as_blocking(self):
        root = self.repo()
        self.git(root, 'checkout', '--detach', 'HEAD')

        result = probe(root)

        self.assertIsNone(result['current']['branch'])
        finding = next(item for item in result['risks'] if item['code'] == 'GIT_DETACHED_HEAD')
        self.assertEqual(finding['severity'], 'blocking')

    def test_probe_reports_multiple_worktrees_and_bound_conflicts(self):
        root = self.repo()
        other = root.parent / (root.name + '-other')
        self.git(root, 'worktree', 'add', '-b', 'feature/other', str(other))
        self.addCleanup(lambda: subprocess.run(['git', 'worktree', 'remove', '--force', str(other)], cwd=root))

        result = probe(root, active_tasks=[
            {'id': 'run-a', 'git_topology': {'effective_mode': 'worktree', 'branch': 'feature/other', 'workspace_root': str(other)}},
            {'id': 'run-b', 'git_topology': {'effective_mode': 'worktree', 'branch': 'feature/other', 'workspace_root': str(other)}},
        ])

        self.assertEqual(len(result['worktrees']), 2)
        codes = {item['code'] for item in result['risks']}
        self.assertIn('GIT_WORKTREE_CONFLICT', codes)

    def test_pending_inventory_is_not_a_writer_claim(self):
        root = self.repo()
        state = {'id': 'pending', 'git_topology': {'status': 'pending', 'branch': 'main', 'workspace_root': str(root)}}
        self.assertFalse(probe(root, active_tasks=[state])['active_tasks'][0]['claiming'])

    def test_recommendation_chooses_worktree_for_parallel_or_dirty_large_work(self):
        clean = self.repo()
        clean_probe = probe(clean)
        recommendation = recommend(clean_probe, {'number': 'GSP-000018', 'title': '大功能', 'intent': 'parallel-multi-task'})
        self.assertEqual(recommendation['mode'], 'worktree')

        self.assertTrue(any('并行' in reason for reason in recommendation['reasons']))

        (clean / 'dirty.txt').write_text('dirty\n', encoding='utf-8')
        dirty_probe = probe(clean)
        dirty_recommendation = recommend(dirty_probe, {'number': 'GSP-000018', 'title': '小修复', 'intent': 'single-task'})
        self.assertEqual(dirty_recommendation['mode'], 'worktree')
        self.assertTrue(any('脏' in reason for reason in dirty_recommendation['reasons']))

    def test_large_single_task_recommends_worktree(self):
        result = recommend(probe(self.repo()), {'number': 'GSP-000018', 'title': '跨模块重构',
                                               'intent': 'single-task', 'size': 'large'})
        self.assertEqual(result['mode'], 'worktree')

    def test_branch_name_keeps_gsp_and_task_number_and_chinese_topic(self):
        name = branch_name('GSP-000018', 'T02', '后端接口')
        self.assertEqual(name, 'feature/GSP-000018-T02-后端接口')
        validate_branch_name(name)

    def test_branch_name_rejects_unsafe_names(self):
        for invalid in ('feature/GSP 000018', 'feature//GSP-000018', 'feature/GSP-000018\\x', 'feature/GSP-000018\n',
                        'feature/GSP-000018-other/child', 'feature/GSP-000018-bad..name',
                        'feature/GSP-000018-bad?name', 'feature/GSP-0000187-wrong', 'feature/GSP-xyz-title'):
            with self.subTest(invalid=invalid):
                with self.assertRaises(WorkflowError):
                    validate_branch_name(invalid)

    def test_probe_non_repository_returns_explicit_blocking_error(self):
        with tempfile.TemporaryDirectory(prefix='gsp-no-git-') as folder:
            result = probe(Path(folder))
        self.assertEqual(result['status'], 'not-a-repository')
        self.assertEqual(result['risks'][0]['code'], 'GIT_NOT_REPOSITORY')
        self.assertEqual(result['risks'][0]['severity'], 'blocking')


if __name__ == '__main__':
    unittest.main()
