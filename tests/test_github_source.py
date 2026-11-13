import tempfile
import subprocess
import unittest
from pathlib import Path
from gsp.github_source import resolve_source

class GithubSourceTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.run(['git','-c','user.name=fixture','-c','user.email=fixture@example.invalid','-C',str(root),*args],capture_output=True,text=True)
    def fixture(self):
        root=Path(tempfile.mkdtemp()); self.git(root,'init','-b','main'); (root/'README.md').write_text('v1'); self.git(root,'add','.'); self.git(root,'commit','-m','v1'); self.git(root,'tag','v0.9.0'); return root
    def test_resolves_tag_to_isolated_directory(self):
        src=self.fixture()
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'source'; result=resolve_source(str(src), 'v0.9.0', out)
            self.assertEqual(result['version'],'v0.9.0'); self.assertTrue((out/'README.md').is_file())
    def test_missing_tag_fails_without_creating_target(self):
        src=self.fixture(); out=Path(tempfile.mkdtemp())/'source'
        with self.assertRaises(RuntimeError): resolve_source(str(src),'v9.9.9',out)
        self.assertFalse(out.exists())
if __name__=='__main__': unittest.main()
