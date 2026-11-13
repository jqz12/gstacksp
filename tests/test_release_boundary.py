import tempfile,unittest
from pathlib import Path
from scripts.release_check import publishable_files
class ReleaseBoundaryTests(unittest.TestCase):
 def test_local_state_is_excluded(self):
  with tempfile.TemporaryDirectory() as t:
   r=Path(t); (r/'README.md').write_text('x'); (r/'.gsp').mkdir(); (r/'.gsp/x').write_text('x'); (r/'.superpowers').mkdir(); (r/'.superpowers/x').write_text('x'); self.assertEqual(publishable_files(r),['README.md'])
if __name__=='__main__': unittest.main()
