import importlib.util
from pathlib import Path
import tempfile
import unittest


class PackagePrivacyTests(unittest.TestCase):
    def test_source_bundle_excludes_task_records_and_cross_project_reports(self):
        spec=importlib.util.spec_from_file_location('gsp_package',Path(__file__).resolve().parents[1]/'scripts/package.py')
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        self.assertTrue(hasattr(m,'collect_files'),'package needs an auditable privacy boundary')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for rel in ['docs/gsp/tasks/private/review.md','docs/work/2026/private/brief.md','research/reports/other/report.json','docs/guide.md','gsp/runtime.py','README.md']:
                p=root/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('fixture')
            names={p.relative_to(root).as_posix() for p in m.collect_files(root)}
            self.assertIn('docs/guide.md',names)
            self.assertFalse(any(n.startswith(('docs/gsp/','docs/work/','research/')) for n in names))
