import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


class GovernanceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('gsp.documents'), 'document governance is not implemented')
        from gsp.documents import Governance
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.book = Governance(self.root)

    def test_idea_is_minimal_and_identity_is_versioned(self):
        r = self.book.create('Login idea', 'Save for later', 'user: save this')
        folder = self.root / r['folder']
        self.assertEqual(r['stage'], 'idea')
        self.assertTrue((folder/'brief.md').is_file())
        self.assertFalse((folder/'spec.md').exists())
        self.assertFalse((folder/'plan.md').exists())
        self.assertEqual(json.loads((folder/'record.json').read_text())['uuid'], r['uuid'])
        self.assertEqual(self.book.get(r['number'])['uuid'], r['uuid'])

    def test_module_rename_split_retrieves_old_work_without_full_text(self):
        m = self.book.module('Authentication', aliases=['login'], paths=['src/auth/'])
        old = self.book.create('Initial account system', 'Summary. '*50+'FULL_BODY_SENTINEL', 'user', modules=[m['uuid']])
        self.book.module('Identity', module_id=m['uuid'], aliases=['auth'], paths=['src/identity/'])
        child = self.book.module('Sessions', parents=[m['uuid']])
        rows = self.book.search(module=child['uuid'])['results']
        self.assertEqual(rows[0]['uuid'], old['uuid'])
        self.assertIn('ancestor', rows[0]['reason'])
        self.assertNotIn('FULL_BODY_SENTINEL', json.dumps(rows))
        self.assertEqual(self.book.search(query='login')['results'][0]['uuid'], old['uuid'])

    def test_lifecycle_never_manufactures_approval_or_ready(self):
        from gsp.runtime import WorkflowError
        r = self.book.create('Idea', 'Goal', 'user')
        r = self.book.stage(r['uuid'], 'design', 'discuss')
        self.assertTrue((self.root/r['folder']/'spec.md').exists())
        with self.assertRaises(WorkflowError): self.book.stage(r['uuid'], 'ready', 'pretend')
        r = self.book.stage(r['uuid'], 'paused', 'later')
        self.assertEqual(r['previous_stage'], 'design')
        self.assertEqual(self.book.stage(r['uuid'], 'design', 'resume')['uuid'], r['uuid'])

    def test_design_transition_preserves_existing_user_spec(self):
        r=self.book.create('Existing draft','goal','user')
        p=self.root/r['folder']/'spec.md';p.write_text('# User written real design\n',encoding='utf-8')
        self.book.stage(r['uuid'],'design','continue existing discussion')
        self.assertEqual(p.read_text(encoding='utf-8'),'# User written real design\n')

    def test_rebuild_works_and_flags_external_edits_until_refresh(self):
        r = self.book.create('Original', 'Goal', 'user')
        p = self.root/r['folder']/'record.json'
        data = json.loads(p.read_text()); data['title'] = 'Externally changed'
        p.write_text(json.dumps(data), encoding='utf-8')
        self.assertTrue(self.book.search(query='Original')['results'][0]['stale'])
        self.book.rebuild()
        self.assertEqual(self.book.search(query='Externally')['results'][0]['uuid'], r['uuid'])
        self.book.index.unlink()
        self.assertEqual(self.book.get(r['uuid'])['title'], 'Externally changed')

    def test_task_relationship_cycles_and_unknown_modules_rejected(self):
        from gsp.runtime import WorkflowError
        a = self.book.create('A', 'a', 'user'); b = self.book.create('B', 'b', 'user')
        self.book.link(a['uuid'], b['uuid'], 'supersedes', 'new design')
        with self.assertRaises(WorkflowError): self.book.link(b['uuid'], a['uuid'], 'supersedes', 'cycle')
        with self.assertRaises(WorkflowError): self.book.create('C', 'c', 'user', modules=['unknown'])

    def test_keyword_search_can_find_existing_document_body(self):
        (self.root/'docs').mkdir(exist_ok=True)
        (self.root/'docs/legacy.md').write_text('# Existing guide\n\nUnique historic constraint about retries.',encoding='utf-8')
        self.book.rebuild()
        result=self.book.search(query='historic constraint')
        self.assertEqual(result['documents'][0]['path'],'docs/legacy.md')
        self.assertLessEqual(len(result['documents'][0]['excerpt']),240)

    def test_explicit_relationship_is_searchable(self):
        a=self.book.create('Original', 'a', 'user'); b=self.book.create('Replacement','b','user')
        self.book.link(b['uuid'],a['uuid'],'supersedes','new requirement')
        self.assertEqual(self.book.search(related=a['uuid'])['results'][0]['uuid'],b['uuid'])

    def test_history_not_default_input_but_explicit_policy_can_include_it(self):
        from gsp.runtime import snapshot,atomic
        p=self.root/'docs/work/2026/history/brief.md';p.parent.mkdir(parents=True);p.write_text('history')
        self.assertNotIn('docs/work/2026/history/brief.md',snapshot(self.root))
        atomic(self.root/'.gsp/verification-inputs.json',{'include':['docs/work/**/*.md']})
        self.assertIn('docs/work/2026/history/brief.md',snapshot(self.root))

    def test_bounded_search_and_updates_do_not_rebuild_history(self):
        from unittest.mock import patch
        for i in range(13): self.book.create('Task '+str(i), 'brief', 'user')
        with patch.object(self.book, 'rebuild', side_effect=AssertionError('unexpected full scan')):
            rows = self.book.search(limit=4, offset=2)
            self.assertEqual(len(rows['results']), 4)
            self.assertTrue(rows['truncated'])
            self.book.stage(rows['results'][0]['uuid'], 'paused', 'later')

    def test_stale_counter_cannot_reissue_an_existing_number(self):
        a=self.book.create('First','brief','user')
        (self.root/'.gsp/work-counter.json').write_text('{"next":1}',encoding='utf-8')
        b=self.book.create('Different title','brief','user')
        self.assertNotEqual(a['number'],b['number'])

    def test_pending_transaction_is_recoverable_without_overwrite(self):
        from unittest.mock import patch
        from gsp.runtime import WorkflowError
        with patch.object(self.book, '_finish', side_effect=OSError('power loss')):
            with self.assertRaises(OSError): self.book.create('Recover me', 'brief', 'user')
        with self.assertRaises(WorkflowError): self.book.create('Blocked', 'b', 'user')
        self.book.recover()
        self.assertEqual(self.book.search(query='Recover')['results'][0]['title'], 'Recover me')

    def test_symlink_storage_escape_rejected(self):
        from gsp.runtime import WorkflowError
        with tempfile.TemporaryDirectory() as outside:
            (self.root/'docs').mkdir(exist_ok=True)
            try: (self.root/'docs/work').symlink_to(outside, target_is_directory=True)
            except OSError: self.skipTest('symlink unavailable')
            with self.assertRaises(WorkflowError): self.book.create('Escape', 'x', 'user')
