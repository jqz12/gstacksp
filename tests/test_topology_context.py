import unittest

from gsp.git_topology import recommend


def inventory(branch='main', dirty=False, status='clean'):
    return {
        'status': status,
        'current': {
            'branch': branch,
            'source_changed_paths': ['src/changed.py'] if dirty else [],
        },
        'risks': [],
    }


class TopologyContextTests(unittest.TestCase):
    def test_small_serial_task_owned_clean_branch_can_stay_in_place(self):
        result = recommend(inventory('feature/GSP-000018-owned'), {
            'number': 'GSP-000018', 'title': '小修复',
            'intent': 'single-task', 'size': 'small',
        })

        self.assertEqual(result['mode'], 'in-place')
        self.assertTrue(any('原地' in reason for reason in result['reasons']))

    def test_large_size_alone_does_not_force_worktree(self):
        result = recommend(inventory('feature/GSP-000018-owned'), {
            'number': 'GSP-000018', 'title': '跨模块重构',
            'intent': 'serial-multi-task', 'size': 'large',
        })

        self.assertEqual(result['mode'], 'in-place')
        self.assertTrue(any('规模' in reason for reason in result['reasons']))

    def test_large_serial_task_on_main_uses_task_branch_not_worktree(self):
        result = recommend(inventory('main'), {
            'number': 'GSP-000018', 'title': '跨模块重构',
            'intent': 'serial-multi-task', 'size': 'large',
        })

        self.assertEqual(result['mode'], 'branch')
        self.assertTrue(any('主分支' in reason for reason in result['reasons']))

    def test_parallel_intent_still_requires_worktree(self):
        result = recommend(inventory('main'), {
            'number': 'GSP-000018', 'title': '并行功能',
            'intent': 'parallel-multi-task', 'size': 'small',
        })

        self.assertEqual(result['mode'], 'worktree')

    def test_independent_verification_requires_worktree(self):
        result = recommend(inventory('feature/GSP-000018-owned'), {
            'number': 'GSP-000018', 'title': '独立验证',
            'intent': 'single-task', 'size': 'small',
            'independent_verification': True,
        })

        self.assertEqual(result['mode'], 'worktree')
        self.assertTrue(any('独立验证' in reason for reason in result['reasons']))

    def test_existing_change_protection_requires_worktree(self):
        result = recommend(inventory('feature/GSP-000018-owned'), {
            'number': 'GSP-000018', 'title': '保护现有改动',
            'intent': 'single-task', 'size': 'small',
            'protect_existing': True,
        })

        self.assertEqual(result['mode'], 'worktree')
        self.assertTrue(any('现有' in reason for reason in result['reasons']))

    def test_dirty_workspace_still_requires_worktree(self):
        result = recommend(inventory('feature/GSP-000018-owned', dirty=True), {
            'number': 'GSP-000018', 'title': '继续开发',
            'intent': 'single-task', 'size': 'small',
        })

        self.assertEqual(result['mode'], 'worktree')

    def test_unowned_clean_feature_branch_keeps_a_task_branch(self):
        result = recommend(inventory('someone-else/topic'), {
            'number': 'GSP-000018', 'title': '新任务',
            'intent': 'single-task', 'size': 'small',
        })

        self.assertEqual(result['mode'], 'branch')

    def test_detached_head_uses_task_branch(self):
        result = recommend(inventory(None), {
            'number': 'GSP-000018', 'title': '恢复开发',
            'intent': 'single-task', 'size': 'small',
        })

        self.assertEqual(result['mode'], 'branch')

    def test_non_repository_keeps_compatibility_path(self):
        result = recommend({'status': 'not-a-repository', 'risks': []}, {
            'number': 'GSP-000018', 'title': '非 Git', 'intent': 'single-task',
        })

        self.assertIsNone(result['mode'])


if __name__ == '__main__':
    unittest.main()
