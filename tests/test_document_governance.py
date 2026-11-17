import tempfile
import unittest
from pathlib import Path

from gsp.documents import Governance
from gsp.runtime import Project, WorkflowError, dispatch, parser, read


class CrossSessionDocumentGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-cross-docs-')
        self.root = Path(self.tmp.name)
        self.project = Project(self.root)
        self.project.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_chinese_tasks_get_stable_non_colliding_topics(self):
        book = Governance(self.root)
        first = book.create('第一次跨会话开发', '目标一', 'test', run_id='first-session')
        second = book.create('第二次跨会话开发', '目标二', 'test', run_id='second-session')
        self.assertTrue(first['folder'].endswith('GSP-000001-first-session'))
        self.assertTrue(second['folder'].endswith('GSP-000002-second-session'))
        self.assertNotEqual(first['folder'], second['folder'])

    def test_single_session_does_not_create_session_registry(self):
        dispatch(self.project, parser().parse_args(['new', 'single', '--title', '单会话任务']))
        with self.assertRaisesRegex(WorkflowError, 'SESSION_MODE_REQUIRED'):
            dispatch(self.project, parser().parse_args(['session-status', 'single']))
        state = read(self.root / '.gsp/runs/single/state.json')
        self.assertFalse((self.root / '.gsp/runs/single/sessions').exists())
        self.assertFalse((self.root / state['doc_dir'] / 'sessions').exists())

    def test_cross_session_records_live_under_task_document_folder(self):
        dispatch(self.project, parser().parse_args(['new', 'cross', '--title', '跨会话任务', '--intent', 'cross-session']))
        state = read(self.root / '.gsp/runs/cross/state.json')
        dispatch(self.project, parser().parse_args([
            'session-register', 'cross', '--role', 'controller', '--host', 'codex',
            '--thread', 'main', '--write-mode', 'write', '--path', 'services']))
        machine = self.root / '.gsp/runs/cross/sessions/index.json'
        human = self.root / state['doc_dir'] / 'sessions/index.md'
        self.assertTrue(machine.is_file())
        self.assertTrue(human.is_file())
        self.assertIn('S01', human.read_text(encoding='utf-8'))
        self.assertNotIn('plan-v2', '\n'.join(p.name for p in human.parent.iterdir()))


if __name__ == '__main__':
    unittest.main()
