from process_helpers import docfolder
from process_helpers import document
from process_helpers import public_main_binding
from process_helpers import session_binding
from process_helpers import temporary_env
"""Backlog lifecycle behavior; isolated projects, real verification subprocesses."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gsp.runtime import Project, contract_hash, fingerprint, main


class TodoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gsp-todos-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = tempfile.TemporaryDirectory(prefix='gsp-todos-home-')
        self.addCleanup(self.home.cleanup)
        codex_home = Path(self.home.name) / '.codex'
        codex_home.mkdir()
        self.fixture_env = temporary_env({
            'HOME': self.home.name,
            'USERPROFILE': self.home.name,
            'CODEX_HOME': str(codex_home),
            'GSP_SESSION_CONTEXT': '',
        }).start()
        self.addCleanup(self.fixture_env.stop)

    def cli(self, *args, ok=True):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(['--project', str(self.root), *args])
            except SystemExit as exc:
                code = exc.code
        if ok:
            self.assertEqual(code, 0, out.getvalue() + err.getvalue())
            return json.loads(out.getvalue())
        self.assertNotEqual(code, 0, out.getvalue() + err.getvalue())
        return err.getvalue()

    def add(self, **overrides):
        values = dict(title='Keep follow-up', what='Handle absent configuration', why='Avoid a confusing error',
                      context='Observed in fixture', source='fixture/review#1', priority='P2', effort='S')
        values.update(overrides)
        args = []
        for key, value in values.items():
            args += ['--' + key.replace('_', '-'), value]
        return self.cli('todo-add', *args)

    def bind_work(self, run='work'):
        state = json.loads(
            (self.root / '.gsp/runs' / run / 'state.json').read_text(encoding='utf-8')
        )
        binding = public_main_binding(
            self.root, state['work_uuid'], thread='todos-' + run,
        )
        self.context = self.root / binding.handle_path
        self.context_env = temporary_env({
            'GSP_SESSION_CONTEXT': str(self.context),
        }).start()
        self.addCleanup(self.context_env.stop)

    def work(self):
        self.cli('new', 'work', '--title', 'Fixture implementation')
        self.bind_work()
        doc = docfolder(self.root,'work')
        document(self.root, 'work', 'design').write_text('# Design\nHandle the configuration path with a meaningful result.\n', encoding='utf-8')
        self.cli('approve', 'work', '--quote', 'SYNTHETIC TEST fixture approval')
        self.cli('task-add', 'work', 'T1', '--title', 'Configuration', '--acceptance', 'Missing configuration handled')
        document(self.root, 'work', 'plan').write_text(
            '# Plan\n\n## T1: Configuration\n'
            'Outcome: Missing configuration is handled and tested.\n'
            'Convergence: {"requires_slices":false,"single_outcome":'
            '"Missing configuration is handled and tested."}\n',
            encoding='utf-8',
        )
        self.cli('seal-plan', 'work')
        self.cli('scope', 'work', '--off', '--quote', 'SYNTHETIC TEST fixture choice')
        self.cli('advance', 'work', '--to', 'execute')
        return doc

    def finish_task(self):
        self.cli('task-start', 'work', 'T1')
        (self.root / 'test_app.py').write_text('import unittest\nclass AppTest(unittest.TestCase):\n def test_result(self): self.assertEqual(2+3,5)\n', encoding='utf-8')
        self.cli('run', 'work', '--task', 'T1', '--', sys.executable, '-m', 'unittest', '-v')
        self.cli('task-done', 'work', 'T1')

    def prepare_delivery(self, doc):
        self.finish_task()
        self.cli('run', 'work', '--task', 'all', '--', sys.executable, '-m', 'unittest', '-v')
        project = Project(self.root)
        state = project.ledger_state('work')
        reviewer = session_binding(
            self.root, state['work_uuid'], kind='reviewer',
            thread='todos-independent-reviewer', capabilities={'report'},
        )
        metadata = {
            'candidate_digest': fingerprint(project.verification_root(state)),
            'contract_digest': contract_hash(project, state, doc),
            'reviewer_context': reviewer.handle_path,
            'findings': [],
        }
        (doc / 'review.md').write_text(
            '# Review\nIndependent specification and quality review checked the task.\n'
            '<!-- gsp-review-evidence\n' + json.dumps(metadata) + '\n-->\n',
            encoding='utf-8',
        )
        self.cli('advance', 'work', '--to', 'review')
        self.cli('review', 'work', '--reviewer', 'fresh-context', '--blocking', '0')
        self.cli('advance', 'work', '--to', 'checking')

    def test_initialize_creates_project_backlog(self):
        self.cli('init')
        self.assertTrue((self.root / 'TODOS.md').is_file())

    def test_existing_singular_file_and_user_text_are_preserved(self):
        file = self.root / 'TODO.md'
        original = '# Personal notes\n\n- [ ] Keep my original issue\n'
        file.write_text(original, encoding='utf-8')
        self.cli('init')
        self.add()
        self.assertFalse((self.root / 'TODOS.md').exists())
        self.assertTrue(file.read_text(encoding='utf-8').startswith(original))
        self.assertTrue(self.cli('todo-list')['unmanaged_content'])

    def test_duplicate_problem_and_source_reuses_identity(self):
        self.cli('init')
        first, second = self.add(), self.add()
        self.assertEqual(first['item']['id'], second['item']['id'])
        self.assertEqual(len(self.cli('todo-list')['items']), 1)
        self.assertFalse(self.cli('todo-list')['unmanaged_content'])

    def test_ambiguous_backlogs_are_not_silently_merged(self):
        self.cli('init')
        (self.root / 'TODO.md').write_text('# Another backlog\n', encoding='utf-8')
        self.assertIn('BACKLOG_AMBIGUOUS', self.cli('todo-list', ok=False))

    def test_duplicate_cannot_silently_downgrade_blocker(self):
        self.cli('init'); self.work()
        self.add(origin='work')
        self.assertIn('BACKLOG_CONFLICT', self.cli('todo-add', '--title', 'Keep follow-up',
            '--what', 'Handle absent configuration', '--why', 'Now required', '--context', 'Fixture',
            '--source', 'fixture/review#1', '--origin', 'work', '--blocking', ok=False))

    def test_removed_backlog_cannot_be_treated_as_empty(self):
        self.cli('init'); doc = self.work(); self.add()
        self.prepare_delivery(doc)
        (self.root / 'TODOS.md').unlink()
        self.assertIn('BACKLOG_MISSING', self.cli('advance', 'work', '--to', 'ready', ok=False))

    def test_no_done_without_completed_task_and_evidence(self):
        self.cli('init'); self.work()
        item = self.add()['item']
        self.assertIn('TASK_NOT_DONE', self.cli('todo-done', item['id'], '--run', 'work', '--task', 'T1', '--reason', 'Fixture rationale', ok=False))
        self.assertEqual(self.cli('todo-list')['items'][0]['status'], 'open')

    def test_done_records_evidence_and_reopen_keeps_history(self):
        self.cli('init'); self.work()
        item = self.add()['item']
        self.finish_task()
        result = self.cli('todo-done', item['id'], '--run', 'work', '--task', 'T1', '--reason', 'Fixture assertion verified')
        self.assertEqual(result['item']['status'], 'done')
        self.assertTrue((self.root / result['item']['evidence']).is_file())
        result = self.cli('todo-reopen', item['id'], '--reason', 'Another input reproduced it')
        self.assertEqual(result['item']['status'], 'open')
        self.assertIn('Fixture assertion verified', result['item']['history'])
        self.assertNotIn('\n\n\n\n', (self.root / 'TODOS.md').read_text(encoding='utf-8'))

    def test_open_blocker_prevents_ready_and_appears_on_resume(self):
        self.cli('init'); doc = self.work()
        self.cli('todo-add', '--title', 'Blocked configuration', '--what', 'Required behavior missing',
                 '--why', 'Cannot meet design', '--context', 'Fixture failure', '--source', 'work/review',
                 '--priority', 'P0', '--effort', 'S', '--origin', 'work', '--blocking')
        result = self.cli('resume', 'work')
        self.assertEqual(len(result['backlog']['items']), 1)
        self.prepare_delivery(doc)
        self.assertIn('BACKLOG_BLOCKING', self.cli('advance', 'work', '--to', 'ready', ok=False))

    def test_nonblocking_followups_disclosed_without_blocking_ready(self):
        self.cli('init'); doc = self.work()
        item = self.add(origin='work')['item']
        self.prepare_delivery(doc)
        self.cli('advance', 'work', '--to', 'ready')
        self.cli('ready', 'work')
        self.assertIn(item['id'], (doc / 'delivery.md').read_text(encoding='utf-8'))
        self.assertIn('TODOS.md', (doc / 'delivery.md').read_text(encoding='utf-8'))

    def test_new_backlog_input_invalidates_ready(self):
        self.test_nonblocking_followups_disclosed_without_blocking_ready()
        self.add(what='A newly discovered follow-up', source='fixture/review#2')
        self.assertNotEqual(self.cli('status', 'work')['status'], 'ready')

    def test_tampered_marked_item_is_not_silently_overwritten(self):
        self.cli('init'); self.add()
        file = self.root / 'TODOS.md'
        file.write_text(file.read_text(encoding='utf-8').replace('**Priority:** P2', '**Priority:** impossible'), encoding='utf-8')
        self.assertIn('BACKLOG_INVALID', self.cli('todo-list', ok=False))

    def test_backlog_symlink_cannot_escape_project(self):
        self.cli('init')
        # Test path validation deterministically without Windows symlink privileges.
        from unittest.mock import patch
        with patch('gsp.todos.inside', return_value=False):
            self.assertIn('ESCAPE', self.cli('todo-list', ok=False))


if __name__ == '__main__':
    unittest.main()
