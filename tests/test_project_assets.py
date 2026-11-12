import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gsp.document_health import audit


class ProjectAssetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'project'
        self.root.mkdir()
        self.reports = Path(self.temp.name)/'reports'

    def test_root_outputs_and_nonmarkdown_evidence_are_inventoried(self):
        (self.root/'Cargo.toml').write_text('[workspace]\n')
        (self.root/'Cargo.lock').write_text('# lock\n')
        (self.root/'b612_dump.txt').write_text('private raw data')
        evidence = self.root/'docs/research/evidence/data.csv'
        evidence.parent.mkdir(parents=True)
        evidence.write_text('a,b\n')
        result = audit(self.root, self.reports)
        self.assertIn('assets', result)
        assets = {a['path']: a for a in result['assets']}
        self.assertEqual(assets['Cargo.toml']['role'], 'engineering')
        self.assertEqual(assets['Cargo.lock']['role'], 'engineering')
        self.assertIn('docs/research/evidence/data.csv', assets)
        candidates = [f['path'] for f in result['findings'] if f['rule'] == 'root-artifact-candidate']
        self.assertEqual(candidates, ['b612_dump.txt'])
        self.assertNotIn('private raw data', json.dumps(result))

    def test_assets_do_not_read_raw_files_or_traverse_build_cache(self):
        raw = self.root/'huge.txt'
        raw.write_bytes(b'x'*2_000_001)
        target = self.root/'target'
        target.mkdir()
        (target/'cache.txt').write_text('cache')
        original = Path.read_bytes
        def guarded(path):
            if path == raw:
                raise AssertionError('Raw asset body must not be read')
            return original(path)
        with patch.object(Path, 'read_bytes', guarded):
            result = audit(self.root, self.reports)
        self.assertIn('assets', result)
        self.assertEqual([a['path'] for a in result['assets']], ['huge.txt'])
        self.assertEqual(result['asset_scan']['raw_content'], 'NOT_READ')

    def test_literal_generator_is_a_candidate_and_reports_are_external(self):
        raw = self.root/'dump.txt'
        raw.write_text('raw')
        tools = self.root/'prototype/tools'
        tools.mkdir(parents=True)
        (tools/'dump.py').write_text('OUT = '+repr(str(raw))+'\n', encoding='utf-8')
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = audit(self.root, self.reports)
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertIn('root-output-literal', {f['rule'] for f in result['findings']})
        self.assertEqual(result['asset_scan']['generator_execution'], 'NOT_CHECKED')

    def test_asset_limits_and_script_limits_are_visible(self):
        from gsp.project_assets import scan_assets
        for i in range(5):
            (self.root/f'{i}.txt').write_text('raw')
        assets, findings, coverage = scan_assets(self.root, self.reports, max_entries=2)
        self.assertLessEqual(len(assets), 2)
        self.assertTrue(coverage['truncated'])
        self.assertIn('asset-scan-truncated', {f['rule'] for f in findings})
        tools = self.root/'tools'
        tools.mkdir()
        for i in range(2):
            (tools/f'{i}.py').write_text('OUT = "something"\n')
        _, findings, coverage = scan_assets(self.root, self.reports, max_scripts=1)
        self.assertTrue(coverage['generators_truncated'])
        self.assertIn('generator-scan-truncated', {f['rule'] for f in findings})

    def test_ignored_root_file_is_visible_and_git_status_is_preserved(self):
        from process_helpers import run
        result = run(['git', 'init', '-q', str(self.root)], capture_output=True)
        self.assertEqual(result.returncode, 0)
        (self.root/'.gitignore').write_text('/dump.txt\n')
        (self.root/'dump.txt').write_text('ignored')
        result = audit(self.root, self.reports)
        self.assertIn('assets', result)
        item = next(a for a in result['assets'] if a['path'] == 'dump.txt')
        self.assertEqual(item['git_state'], 'ignored')

    def test_local_materials_are_inventoried(self):
        local = self.root/'.local/research/run'
        local.mkdir(parents=True)
        (local/'capture.bin').write_bytes(b'raw')
        result = audit(self.root, self.reports)
        self.assertIn('.local/research/run/capture.bin', [a['path'] for a in result['assets']])

    def test_symlink_is_not_followed(self):
        link = self.root/'outside.txt'
        try:
            link.symlink_to(Path(self.temp.name)/'missing.txt')
        except OSError:
            self.skipTest('symlink unavailable')
        result = audit(self.root, self.reports)
        self.assertIn('asset-link-skipped', {f['rule'] for f in result['findings']})

    def test_script_oversize_or_invalid_syntax_does_not_silently_pass(self):
        from gsp.project_assets import scan_assets
        tools = self.root/'scripts'
        tools.mkdir()
        (tools/'bad.py').write_text('broken (')
        (tools/'large.py').write_bytes(b'x'*100)
        (tools/'invalid_path.py').write_text('OUT = "bad\\0.txt"\n')
        _, findings, _ = scan_assets(self.root, self.reports, max_script_bytes=50)
        self.assertIn('generator-unchecked', {f['rule'] for f in findings})

    def test_internal_reports_are_excluded_from_asset_inventory(self):
        reports = self.root/'docs/gsp/reports/document-governance'
        first = audit(self.root, reports)
        second = audit(self.root, reports)
        self.assertIn('assets', first)
        self.assertEqual(first['assets'], second['assets'])

    def test_external_report_does_not_reinventory_default_report_history(self):
        old = self.root/'docs/gsp/reports/document-governance/old'
        old.mkdir(parents=True)
        (old/'report.md').write_text('[missing](absent.txt)')
        (old/'report.json').write_text('{}')
        result = audit(self.root, self.reports)
        self.assertEqual(result['inventory']['markdown_files'], 0)
        self.assertEqual(result['assets'], [])

    @unittest.skipUnless(os.name == 'nt', 'Windows junction test')
    def test_junctions_are_skipped_without_path_is_junction_api(self):
        from process_helpers import run
        docs = self.root/'docs'
        tools = self.root/'tools'
        docs.mkdir()
        tools.mkdir()
        targets = [self.root/'private', self.root/'target', Path(self.temp.name)/'outside']
        for number, target in enumerate(targets):
            target.mkdir()
            (target/'raw.txt').write_text('raw')
            (target/'note.md').write_text('external evidence')
            (target/'generator.py').write_text('OUT = '+repr(str(self.root/'new.txt')))
            result = run(['cmd', '/c', 'mklink', '/J', str(docs/f'linked-{number}'), str(target)],
                         capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        result = run(['cmd', '/c', 'mklink', '/J', str(tools/'linked'), str(targets[1])],
                     capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        with patch.object(Path, 'is_junction', return_value=False, create=True):
            report = audit(self.root, self.reports)
        self.assertEqual(report['assets'], [])
        self.assertEqual(report['inventory']['markdown_files'], 0)
        self.assertEqual(report['asset_scan']['scripts_read'], 0)
        self.assertIn('asset-link-skipped', {f['rule'] for f in report['findings']})
