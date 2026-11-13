import tempfile
import unittest
from pathlib import Path
from gsp.release_manifest import build_manifest, validate_manifest

class ReleaseManifestTests(unittest.TestCase):
    def test_manifest_is_deterministic_and_excludes_local_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'README.md').write_text('x'); (root/'.gsp').mkdir(); (root/'.gsp/state.json').write_text('secret')
            (root/'.superpowers').mkdir(); (root/'.superpowers/progress.md').write_text('local')
            result=build_manifest(root, source='https://github.com/example/gstacksp.git', version='v0.9.0', commit='abc')
            self.assertEqual(result['files'], ['README.md'])
            self.assertEqual(result, build_manifest(root, source=result['source'], version=result['version'], commit=result['commit']))
            validate_manifest(result)
    def test_invalid_source_and_version_rejected(self):
        with self.assertRaises(ValueError):
            build_manifest(Path('.'), source='local', version='main', commit='')
if __name__=='__main__': unittest.main()
