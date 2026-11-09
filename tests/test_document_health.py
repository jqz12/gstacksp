import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


class DocumentHealthTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('gsp.document_health'),'document health is not implemented')
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'project';self.root.mkdir()
        self.reports=Path(self.tmp.name)/'reports'
    def audit(self):
        from gsp.document_health import audit
        return audit(self.root,self.reports)
    def test_reports_duplicates_broken_links_and_preserves_target(self):
        (self.root/'docs').mkdir()
        for name in ('a.md','b.md'): (self.root/'docs'/name).write_text('# Same\n\n[missing](missing.md)\n',encoding='utf-8')
        before={p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        r=self.audit(); rules={f['rule'] for f in r['findings']}
        self.assertIn('duplicate-content',rules);self.assertIn('broken-link',rules)
        after={p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before,after)
        self.assertTrue(Path(r['report_markdown']).is_file());self.assertTrue(Path(r['report_json']).is_file())
        self.assertEqual(r['checks']['semantic-correctness'],'NOT_CHECKED')
    def test_report_history_and_finding_ids_are_stable(self):
        (self.root/'README.md').write_text('[bad](missing.md)',encoding='utf-8')
        a=self.audit();b=self.audit()
        self.assertNotEqual(a['report_json'],b['report_json'])
        self.assertEqual(a['findings'][0]['id'],b['findings'][0]['id'])
        self.assertEqual(b['delta']['new'],[])
        (self.root/'README.md').write_text('# Fixed',encoding='utf-8')
        c=self.audit();self.assertIn(a['findings'][0]['id'],c['delta']['resolved'])
    def test_fences_are_not_document_links_and_unknown_is_not_redundant(self):
        (self.root/'docs').mkdir()
        (self.root/'docs/note.md').write_text('# Unlinked\n```md\n[example](imaginary.md)\n```\n',encoding='utf-8')
        r=self.audit()
        self.assertFalse(any(f['rule']=='broken-link' for f in r['findings']))
        self.assertTrue(all(f.get('action')!='delete' for f in r['findings']))
    def test_malformed_records_are_findings_not_fake_pass(self):
        folder=self.root/'docs/work/2026/task';folder.mkdir(parents=True)
        (folder/'record.json').write_text('{bad',encoding='utf-8')
        r=self.audit()
        self.assertFalse(r['integrity_ok'])
        self.assertTrue(any(f['rule']=='invalid-record' for f in r['findings']))
    def test_report_directory_cannot_be_a_scanned_input(self):
        from gsp.document_health import audit
        (self.root/'docs').mkdir()
        first=audit(self.root,self.root/'docs/gsp/reports/document-governance')
        second=audit(self.root,self.root/'docs/gsp/reports/document-governance')
        self.assertEqual(first['inventory']['markdown_files'],second['inventory']['markdown_files'])
