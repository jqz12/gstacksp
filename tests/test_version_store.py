import tempfile
import unittest
from pathlib import Path
from gsp.version_store import VersionStore

class VersionStoreTests(unittest.TestCase):
    def test_switch_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=VersionStore(Path(tmp)); a=Path(tmp)/'v1'; b=Path(tmp)/'v2'; a.mkdir(); b.mkdir(); (a/'x').write_text('1'); (b/'x').write_text('2')
            s.switch('v1',a); s.switch('v2',b); self.assertEqual(s.current()['version'],'v2'); s.rollback('v1'); self.assertEqual(s.current()['version'],'v1')
    def test_local_edit_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=VersionStore(Path(tmp)); a=Path(tmp)/'v1'; a.mkdir(); (a/'x').write_text('1'); s.switch('v1',a); (a/'x').write_text('changed'); self.assertTrue(s.modified())
if __name__=='__main__': unittest.main()
