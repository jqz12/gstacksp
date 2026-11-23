import json
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from process_helpers import run
except ModuleNotFoundError:
    from tests.process_helpers import run

from gsp.documents import Governance


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts' / 'gsp.py'


class BlockedLifecycleCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-blocked-')
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Blocked Lifecycle Test')
        self.git('config', 'user.email', 'blocked@example.invalid')
        (self.project / 'README.md').write_text('# blocked\n', encoding='utf-8')
        self.git('add', 'README.md')
        self.git('commit', '-qm', 'chore: baseline')
        self.cli('init')
        self.cli('new', 'work', '--title', '阻塞终态')

    def git(self, *args):
        result = run(['git', *args], cwd=self.project, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def cli(self, *args, ok=True):
        result = run([sys.executable, str(CLI), '--project', str(self.project), *args],
                     capture_output=True, text=True, encoding='utf-8')
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def read_state(self):
        return json.loads((self.project / '.gsp/runs/work/state.json').read_text(encoding='utf-8'))

    def archive_stage(self):
        state = self.read_state()
        return Governance(self.project).get(state['work_uuid'])['stage']

    def test_block_records_and_unblock_restores(self):
        self.cli('block', 'work', '--reason', 'waiting for upstream')
        blocked = self.read_state()
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(blocked['blocked_from'], 'design')
        self.cli('unblock', 'work', '--reason', 'upstream restored')
        state = self.read_state()
        self.assertEqual(state['status'], 'design')
        self.assertEqual(state['unblock_reason'], 'upstream restored')
        events = (self.project / '.gsp/runs/work/events.jsonl').read_text(encoding='utf-8')
        self.assertIn('"event": "unblock"', events)

    def test_unblock_requires_blocked(self):
        result = self.cli('unblock', 'work', '--reason', 'not blocked', ok=False)
        self.assertIn('UNBLOCK_REQUIRES_BLOCKED', result.stdout + result.stderr)

    def test_abandoned_close_requires_blocked(self):
        result = self.cli('close', 'work', '--abandoned', '--reason', 'not blocked', ok=False)
        self.assertIn('CLOSE_ABANDON_REQUIRES_BLOCKED', result.stdout + result.stderr)

    def test_abandoned_close_cancels_archive(self):
        self.cli('block', 'work', '--reason', 'cannot continue')
        self.cli('close', 'work', '--abandoned', '--reason', 'explicitly abandoned')
        state = self.read_state()
        self.assertEqual(state['status'], 'closed')
        self.assertEqual(state['close_kind'], 'abandoned')
        self.assertFalse((self.project / '.gsp/active.json').exists())
        self.assertEqual(self.archive_stage(), 'cancelled')

    def test_plain_close_still_requires_ready(self):
        self.cli('block', 'work', '--reason', 'cannot continue')
        result = self.cli('close', 'work', '--reason', 'close without abandon', ok=False)
        self.assertIn('CLOSE_REQUIRES_READY', result.stdout + result.stderr)

    def test_resume_blocked_offers_unblock_and_abandoned(self):
        self.cli('block', 'work', '--reason', 'waiting for upstream')
        result = self.cli('resume', 'work')
        pending = json.loads(result.stdout)['pending_decisions']
        blocked = [item for item in pending if item.get('kind') == 'blocked']
        self.assertEqual(len(blocked), 1)
        self.assertIn('unblock', blocked[0]['command'])
        self.assertIn('--abandoned', blocked[0]['command'])

    def test_abandoned_close_tolerates_missing_worktree(self):
        state = self.read_state()
        (self.project / state['documents']['design']).write_text(
            '# Design\nFixture abandoned close.', encoding='utf-8')
        self.cli('approve', 'work', '--quote', 'SYNTHETIC approved')
        self.cli('task-add', 'work', 'T01', '--title', '实现', '--acceptance', 'checks pass')
        (self.project / state['documents']['plan']).write_text(
            '# Plan\nT01 implementation and checks.', encoding='utf-8')
        self.cli('seal-plan', 'work')
        self.cli('scope', 'work', '--off', '--quote', 'SYNTHETIC no freeze')
        self.cli('topology-select', 'work', '--mode', 'worktree', '--quote', 'SYNTHETIC isolated child')
        workspace = Path(self.read_state()['git_topology']['workspace_root'])
        self.cli('block', 'work', '--reason', 'abandon after rework')
        self.git('worktree', 'remove', '--force', str(workspace))
        self.cli('close', 'work', '--abandoned', '--reason', 'worktree retired manually')
        closed = self.read_state()
        self.assertEqual(closed['status'], 'closed')
        self.assertEqual(closed['close_kind'], 'abandoned')
        self.assertEqual(self.archive_stage(), 'cancelled')


if __name__ == '__main__':
    unittest.main()
