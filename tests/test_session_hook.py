import tempfile
import unittest
from pathlib import Path

from gsp.codex_hooks import handle
from gsp.runtime import Project, parser, dispatch


class SessionHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-session-hook-')
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.project = Project(self.root)
        self.project.initialize()
        self.cmd('new', 'work', '--title', '跨会话 hook', '--intent', 'cross-session')
        self.cmd('session-register', 'work', '--role', 'controller', '--host', 'codex',
                 '--thread', 'thread-a', '--write-mode', 'write', '--path', 'services')

    def tearDown(self):
        self.tmp.cleanup()

    def cmd(self, *argv):
        return dispatch(self.project, parser().parse_args(list(argv)))[0]

    def payload(self, session, path):
        return {'hook_event_name': 'PreToolUse', 'tool_name': 'Write', 'cwd': str(self.root),
                'session_id': session, 'tool_input': {'file_path': path}}

    def test_registered_session_can_edit_claimed_path(self):
        self.assertEqual(handle(self.payload('thread-a', 'services/unit.service')), {})

    def test_apply_patch_checks_each_declared_target(self):
        payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'apply_patch', 'cwd': str(self.root),
                   'session_id': 'thread-a', 'tool_input': {'command':
                       '*** Begin Patch\n*** Add File: services/another.service\n+placeholder\n*** End Patch'}}
        self.assertEqual(handle(payload), {})

    def test_unknown_or_unclaimed_session_is_denied_before_edit(self):
        denied = handle(self.payload('thread-b', 'services/unit.service'))
        self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('SESSION_', denied['hookSpecificOutput']['permissionDecisionReason'])
        denied = handle(self.payload('thread-a', 'tests/unit.py'))
        self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('SESSION_PATH_', denied['hookSpecificOutput']['permissionDecisionReason'])


if __name__ == '__main__':
    unittest.main()
