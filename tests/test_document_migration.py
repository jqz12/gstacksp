import importlib.util
from pathlib import Path
import tempfile
import unittest


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('gsp.document_migration'),'controlled migration is not implemented')
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        (self.root/'docs').mkdir();(self.root/'README.md').write_text('[guide](docs/old.md)',encoding='utf-8')
        (self.root/'docs/old.md').write_text('# Guide\n\n[home](../README.md)\n',encoding='utf-8')
    def migrate(self,apply=False):
        from gsp.document_migration import move
        return move(self.root,'docs/old.md','docs/howto/guide.md',apply)
    def test_preview_is_read_only_and_apply_updates_both_link_directions(self):
        preview=self.migrate();self.assertFalse(preview['applied']);self.assertFalse((self.root/'.gsp').exists())
        self.migrate(True)
        self.assertIn('docs/howto/guide.md',(self.root/'README.md').read_text())
        self.assertIn('../../README.md',(self.root/'docs/howto/guide.md').read_text())
        self.assertIn('howto/guide.md',(self.root/'docs/old.md').read_text(encoding='utf-8'))
    def test_never_overwrites_existing_destination(self):
        from gsp.runtime import WorkflowError
        (self.root/'docs/howto').mkdir();(self.root/'docs/howto/guide.md').write_text('user content')
        with self.assertRaises(WorkflowError): self.migrate(True)
        self.assertEqual((self.root/'docs/howto/guide.md').read_text(),'user content')
    def test_bound_spec_is_refused_until_formal_rebinding(self):
        import json
        from gsp.runtime import WorkflowError
        p=self.root/'.gsp/runs/old';p.mkdir(parents=True)
        (p/'state.json').write_text(json.dumps({'id':'old','documents':{'design':'docs/old.md','plan':'docs/plan.md'}}))
        with self.assertRaises(WorkflowError): self.migrate(True)
    def test_interrupted_write_can_recover(self):
        from unittest.mock import patch
        import gsp.document_migration as migration
        with patch.object(migration,'finish',side_effect=OSError('power loss')):
            with self.assertRaises(OSError): self.migrate(True)
        migration.recover(self.root)
        self.assertTrue((self.root/'docs/howto/guide.md').is_file())
    def test_parent_escape_refused(self):
        from gsp.document_migration import move
        from gsp.runtime import WorkflowError
        with self.assertRaises(WorkflowError):move(self.root,'docs/old.md','../outside.md',True)
