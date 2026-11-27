from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gsp.documents import Governance
from gsp.runtime import Project, dispatch, parser, read


class MonthLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        Project(self.root).initialize()
        self.book = Governance(self.root)

    def at(self, month, title):
        with patch('gsp.documents.datetime') as clock:
            clock.now.return_value = datetime(2026, month, 9)
            return self.book.create(title, 'Goal', 'user')

    def test_new_month_fixed_and_global_number_survives_rebuild(self):
        a = self.at(1, 'First')
        self.assertEqual(a['folder'], 'docs/work/2026/01/GSP-000001-first')
        self.book.index.unlink()
        (self.root/'.gsp/work-counter.json').unlink()
        b = self.at(11, 'Second')
        self.assertEqual(b['number'], 'GSP-000002')
        self.assertIn('/2026/11/', b['folder'])
        with patch('gsp.documents.datetime') as clock:
            clock.now.return_value = datetime(2027, 2, 1)
            a = self.book.stage(a['uuid'], 'design', 'Resume later')
        self.assertIn('/2026/01/', a['folder'])
        self.assertTrue((self.root/a['folder']/'spec.md').exists())

    def test_rebuild_keeps_old_year_and_new_month_records(self):
        old = self.book.create('Legacy', 'Goal', 'user', folder='docs/work/2025/GSP-000001-legacy')
        new = self.at(11, 'Current')
        self.book.index.unlink()
        self.book.rebuild()
        rows = self.book.search()['results']
        self.assertEqual({r['uuid'] for r in rows}, {old['uuid'], new['uuid']})
        self.assertEqual(self.book.get(old['uuid'])['folder'], old['folder'])

    def test_dashboard_does_not_link_future_artifacts(self):
        project = Project(self.root)
        project.initialize()
        dispatch(project, parser().parse_args(['new', 'fresh', '--title', 'Fresh task']))
        project.dashboard(project.ledger_state('fresh'))
        text = (self.root/'docs/gsp/README.md').read_text(encoding='utf-8')
        for name in ('plan.md', 'decisions.md', 'delivery.md'):
            self.assertNotIn(name, text)
        state = read(self.root/'.gsp/runs/fresh/state.json')
        spec = project.document(state, 'design')
        self.assertIn('spec.md', text)
        spec.write_text('# Real specification with acceptance criteria', encoding='utf-8')
        dispatch(project, parser().parse_args(['approve', 'fresh', '--quote', 'approved']))
        project.dashboard(project.ledger_state('fresh'))
        self.assertIn('plan.md', (self.root/'docs/gsp/README.md').read_text(encoding='utf-8'))

    def test_saved_monthly_design_is_protected_without_local_run(self):
        from gsp.document_migration import move
        from gsp.runtime import WorkflowError
        record = self.at(11, 'Saved design')
        record = self.book.stage(record['uuid'], 'design', 'Discuss saved idea')
        with self.assertRaisesRegex(WorkflowError, 'DOCUMENT_BOUND'):
            move(self.root, record['folder']+'/spec.md', 'docs/archive/spec.md', True)
