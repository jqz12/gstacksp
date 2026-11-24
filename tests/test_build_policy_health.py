"""Health reports build-policy state without blocking on advisory findings."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gsp.build_policy import build_policy_report
from gsp.runtime import health


class BuildPolicyHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-policy-health-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.gsp').mkdir()

    def write_policy(self, value):
        path = self.root / '.gsp' / 'build-policy.json'
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def test_cargo_project_without_policy_is_advisory_missing(self):
        (self.root / 'Cargo.toml').write_text('[package]\nname = "fixture"\n', encoding='utf-8')
        report = build_policy_report(self.root)
        self.assertEqual(report['status'], 'missing')
        self.assertEqual(report['findings'][0]['code'], 'BUILD_POLICY_MISSING')
        self.assertEqual(report['findings'][0]['severity'], 'advisory')

    def test_invalid_policy_is_blocking(self):
        (self.root / 'Cargo.toml').write_text('[package]\nname = "fixture"\n', encoding='utf-8')
        path = self.root / '.gsp' / 'build-policy.json'
        path.write_text('{not json', encoding='utf-8')
        report = build_policy_report(self.root)
        self.assertEqual(report['status'], 'invalid')
        self.assertEqual(report['findings'][0]['severity'], 'blocking')

    def test_profile_expectation_is_read_only_advisory(self):
        cargo = self.root / 'Cargo.toml'
        cargo.write_text('[package]\nname = "fixture"\n\n[profile.dev]\ndebug = true\n',
                         encoding='utf-8')
        before = cargo.read_bytes()
        self.write_policy({'profile_expectations': {'debug': False}})
        report = build_policy_report(self.root)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual([row['code'] for row in report['findings']],
                         ['PROFILE_EXPECTATION_MISMATCH'])
        self.assertEqual(report['findings'][0]['severity'], 'advisory')
        self.assertEqual(cargo.read_bytes(), before)

    def test_health_row_is_not_fail_for_missing_but_fail_for_invalid(self):
        (self.root / 'Cargo.toml').write_text('[package]\nname = "fixture"\n', encoding='utf-8')
        benign = {'check': 'codex-user-update', 'status': 'NOT_CHECKED', 'detail': 'fixture'}
        with patch('gsp.runtime._user_update_check', return_value=benign):
            missing = health(self.root, 'codex')
        row = next(row for row in missing['checks'] if row['check'] == 'build-policy')
        self.assertEqual(row['status'], 'NOT_CHECKED')
        self.assertEqual(row['detail']['status'], 'missing')
        self.write_policy({'temp_roots': ['../escape']})
        with patch('gsp.runtime._user_update_check', return_value=benign):
            invalid = health(self.root, 'codex')
        row = next(row for row in invalid['checks'] if row['check'] == 'build-policy')
        self.assertEqual(row['status'], 'FAIL')


if __name__ == '__main__':
    unittest.main()
