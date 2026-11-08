from pathlib import Path
import tempfile
import unittest

from gsp.runtime import WorkflowError


class LearningTests(unittest.TestCase):
    def test_cli_correction_and_history_use_same_log(self):
        from contextlib import redirect_stdout, redirect_stderr
        import io, json
        from gsp.runtime import main
        def cli(*args):
            output=io.StringIO()
            with redirect_stdout(output): code=main(['--project',str(self.root),*args])
            self.assertEqual(code,0)
            return json.loads(output.getvalue())
        cli('init')
        first=cli('learn','--text','Initial conclusion','--source','task/1')[0]
        fixed=cli('learn','--text','Correct conclusion','--source','task/2','--supersedes',first['id'],'--reason','Observed evidence')[0]
        self.assertEqual(fixed['status'],'active')
        self.assertEqual(len(cli('learn','--history')),2)
        self.assertEqual(cli('learn','--invalidate',fixed['id'],'--source','task/3','--reason','No longer applicable'),[])
        self.assertEqual(len(cli('learn','--history')),2)

    def setUp(self):
        from gsp.learnings import Learnings
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.book = Learnings(self.root)

    def test_duplicate_merges_sources_and_correction_keeps_history(self):
        first = self.book.add('Use bounded retries', 'task/1')
        second = self.book.add('  use   BOUNDED retries ', 'task/2')
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(self.book.query()[0]['sources'], ['task/1', 'task/2'])
        fixed = self.book.add('Retry EACCES only', 'task/3', supersedes=first['id'], reason='Other errors are permanent')
        self.assertEqual([r['id'] for r in self.book.query()], [fixed['id']])
        history = self.book.query(history=True)
        self.assertEqual(history[0]['status'], 'superseded')
        self.assertEqual(history[0]['replaced_by'], fixed['id'])
        self.assertEqual(history[0]['text'], 'Use bounded retries')

    def test_invalidation_and_readding_does_not_revive_obsolete_entry(self):
        entry = self.book.add('Old conclusion', 'task/1')
        self.book.invalidate(entry['id'], 'Incorrect condition', 'task/2')
        self.assertEqual(self.book.query(), [])
        with self.assertRaises(WorkflowError): self.book.add('Old conclusion', 'task/3')
        self.assertEqual(self.book.query(history=True)[0]['status'], 'invalid')

    def test_legacy_and_corrupt_log(self):
        self.book.file.parent.mkdir(parents=True)
        self.book.file.write_text('{"id":"old","at":"date","text":"Legacy","source":"task"}\n', encoding='utf-8')
        self.assertEqual(self.book.query()[0]['sources'], ['task'])
        self.book.add('Corrected', 'task/2', supersedes='old', reason='New evidence')
        self.assertEqual(len(self.book.query(history=True)), 2)
        with self.book.file.open('a', encoding='utf-8') as out: out.write('{bad\n')
        before = self.book.file.read_bytes()
        with self.assertRaises(WorkflowError): self.book.add('No write', 'task/3')
        self.assertEqual(self.book.file.read_bytes(), before)

    def test_invalid_relationship_and_missing_reason_do_not_write(self):
        entry = self.book.add('Valid', 'task/1')
        before = self.book.file.read_bytes()
        with self.assertRaises(WorkflowError): self.book.add('Correction', 'task/2', supersedes=entry['id'])
        with self.assertRaises(WorkflowError): self.book.invalidate('unknown', 'reason', 'task')
        self.assertEqual(self.book.file.read_bytes(), before)
