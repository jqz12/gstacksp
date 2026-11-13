import tempfile,unittest
from pathlib import Path
from gsp.version_store import VersionStore
class GithubUpdateIntegrationTests(unittest.TestCase):
 def test_two_host_targets_share_version_and_rollback(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t); a=root/'v1'; b=root/'v2'; a.mkdir(); b.mkdir(); (a/'x').write_text('1'); (b/'x').write_text('2')
   for host in ('codex','claude'):
    s=VersionStore(root/host); s.switch('v1',a); s.switch('v2',b); s.rollback('v1'); self.assertEqual(s.current()['version'],'v1')
if __name__=='__main__': unittest.main()
