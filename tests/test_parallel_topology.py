import unittest

from gsp.runtime import parser, validate_parallel_bindings


def task(task_id, role, workspace, status='done', branch=None, depends=None):
    return {
        'id': task_id,
        'title': task_id,
        'role': role,
        'status': status,
        'depends': depends or [],
        'topology': {
            'effective_mode': 'worktree',
            'workspace_root': workspace,
            'branch': branch or f'feature/GSP-000018-{task_id}',
        },
    }


class ParallelTopologyTests(unittest.TestCase):
    def state(self, tasks, intent='full-stack'):
        return {
            'id': 'gsp-parent',
            'number': 'GSP-000018',
            'git_topology': {'intent': intent, 'effective_mode': 'worktree'},
            'tasks': tasks,
        }

    def test_frontend_backend_integration_have_distinct_writable_workspaces(self):
        result = validate_parallel_bindings(self.state([
            task('T01', 'backend', 'E:/wt/backend'),
            task('T02', 'frontend', 'E:/wt/frontend'),
            task('T03', 'integration', 'E:/wt/integration', depends=['T01', 'T02']),
        ]))
        self.assertTrue(result['valid'])
        self.assertEqual(result['integration_task'], 'T03')
        self.assertEqual(result['blocking'], [])

    def test_shared_writable_workspace_is_blocked(self):
        result = validate_parallel_bindings(self.state([
            task('T01', 'backend', 'E:/wt/shared', status='running'),
            task('T02', 'frontend', 'E:/wt/shared', status='running'),
            task('T03', 'integration', 'E:/wt/integration'),
        ]))
        self.assertFalse(result['valid'])
        self.assertIn('GIT_WORKTREE_CONFLICT', {row['code'] for row in result['blocking']})

    def test_full_stack_requires_integration_task_and_completed_children(self):
        missing = validate_parallel_bindings(self.state([
            task('T01', 'backend', 'E:/wt/backend'),
            task('T02', 'frontend', 'E:/wt/frontend'),
        ]))
        self.assertIn('GIT_INTEGRATION_REQUIRED', {row['code'] for row in missing['blocking']})

        pending = validate_parallel_bindings(self.state([
            task('T01', 'backend', 'E:/wt/backend', status='running'),
            task('T02', 'frontend', 'E:/wt/frontend'),
            task('T03', 'integration', 'E:/wt/integration', depends=['T01', 'T02']),
        ]))
        self.assertIn('GIT_CHILD_TASKS_PENDING', {row['code'] for row in pending['blocking']})

    def test_serial_single_task_does_not_require_parallel_integration(self):
        result = validate_parallel_bindings(self.state([
            task('T01', 'implementation', 'E:/wt/one'),
        ], intent='single-task'))
        self.assertTrue(result['valid'])
        self.assertIsNone(result['integration_task'])

    def test_cross_session_single_task_uses_its_parent_workspace(self):
        result = validate_parallel_bindings(self.state([
            {'id': 'T01', 'role': 'implementation', 'status': 'done', 'topology': {}},
        ], intent='cross-session'))
        self.assertTrue(result['valid'], result)
        self.assertIsNone(result['integration_task'])

    def test_task_add_parser_keeps_role_and_parent_metadata_options(self):
        args = parser().parse_args([
            'task-add', 'parent', 'T02', '--title', '前端', '--acceptance', '前端测试',
            '--role', 'frontend', '--agent', 'agent-a', '--session', 'session-a', '--parent-task', 'T01',
        ])
        self.assertEqual(args.role, 'frontend')
        self.assertEqual(args.parent_task, 'T01')


if __name__ == '__main__':
    unittest.main()
