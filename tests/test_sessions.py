import subprocess
import tempfile
import unittest
from pathlib import Path

from gsp.runtime import Project, WorkflowError, parser, dispatch, read
from gsp.documents import Governance
from gsp.sessions import (
    authorize_edit,
    claim_session,
    handoff_controller,
    register_session,
    session_status,
)


class SessionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-session-registry-')
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Session Test')
        self.git('config', 'user.email', 'session@example.invalid')
        (self.root / 'README.md').write_text('# sessions\n', encoding='utf-8')
        self.git('add', 'README.md')
        self.git('commit', '-qm', 'chore: baseline')
        self.project = Project(self.root)
        self.project.initialize()
        self.cmd('new', 'work', '--title', '中文跨会话任务', '--intent', 'cross-session')
        self.state = read(self.root / '.gsp/runs/work/state.json')
        self.cmd('task-add', 'work', 'T01', '--title', '服务实现', '--acceptance', '接口测试通过')
        self.state = read(self.root / '.gsp/runs/work/state.json')

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        result = subprocess.run(['git', *args], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def cmd(self, *argv):
        return dispatch(self.project, parser().parse_args(list(argv)))[0]

    def test_registers_sessions_and_rejects_second_writer_in_same_workspace(self):
        first = register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-a', '--write-mode', 'write', '--path', 'services']))
        self.assertEqual(first['session']['id'], 'S01')
        second_args = parser().parse_args([
            'session-register', 'work', '--role', 'worker', '--host', 'claude',
            '--thread', 'thread-b', '--task', 'T01', '--write-mode', 'write', '--path',
            'services/quant-control.service'])
        with self.assertRaisesRegex(WorkflowError, 'WORKSPACE_WRITE_LEASE_HELD'):
            register_session(self.project, self.state, second_args)

    def test_second_controller_is_rejected_until_handoff(self):
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-a', '--write-mode', 'write', '--path', '.']))
        with self.assertRaisesRegex(WorkflowError, 'CONTROLLER_LEASE_HELD S01'):
            register_session(self.project, self.state, parser().parse_args([
                'session-register', 'work', '--role', 'controller', '--host', 'claude',
                '--thread', 'thread-b', '--write-mode', 'read-only']))

    def test_non_overlapping_worktrees_can_claim_paths_and_overlap_is_blocked(self):
        first = register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-a', '--worktree', str(self.root / 'one'),
            '--write-mode', 'write', '--path', 'services']))
        second = register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'worker', '--host', 'claude',
            '--thread', 'thread-b', '--task', 'T01', '--worktree', str(self.root / 'two'),
            '--write-mode', 'write', '--path', 'tests']))
        self.assertEqual((first['session']['id'], second['session']['id']), ('S01', 'S02'))
        result = claim_session(self.project, self.state, 'S02', ['tests'])
        self.assertEqual(result['claimed_paths'], ['tests'])

    def test_chinese_title_uses_stable_run_id_folder_suffix(self):
        record = Governance(self.root).create('中文标题', '目标', 'test', run_id='cross-session-development')
        self.assertTrue(record['folder'].endswith('GSP-000002-cross-session-development'))

    def test_handoff_transfers_controller_and_old_session_cannot_edit(self):
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'controller', '--host', 'codex',
            '--thread', 'thread-a', '--write-mode', 'write', '--path', '.']))
        register_session(self.project, self.state, parser().parse_args([
            'session-register', 'work', '--role', 'observer', '--host', 'claude',
            '--thread', 'thread-b', '--write-mode', 'read-only']))
        result = handoff_controller(self.project, self.state, parser().parse_args([
            'session-handoff', 'work', '--from', 'S01', '--to', 'S02',
            '--reason', '主会话切换']))
        self.assertEqual(result['controller_session'], 'S02')
        with self.assertRaisesRegex(WorkflowError, 'SESSION_WRITE_LEASE_REQUIRED'):
            authorize_edit(self.project, self.state, 'thread-a', [self.root / 'README.md'])
        status = session_status(self.project, self.state)
        self.assertEqual(status['controller_session'], 'S02')
        self.assertEqual(status['sessions'][0]['status'], 'handed-off')


if __name__ == '__main__':
    unittest.main()
