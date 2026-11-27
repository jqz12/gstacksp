import tempfile
import unittest
from pathlib import Path

from gsp.architecture import analyze_project, make_baseline


class ArchitectureBaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gsp-architecture-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')

    def baseline(self, rules=None):
        return make_baseline(analyze_project(self.root, rules=rules), reason='existing project intake')

    def test_local_fix_in_legacy_large_file_preserves_debt_identity(self):
        self.write('old.py', 'x = 1\n' * 1001)
        baseline = self.baseline()
        self.write('old.py', 'x = 2\n' * 1002)
        result = analyze_project(self.root, baseline=baseline, changed_paths=['old.py'])
        self.assertTrue(result['change_ok'])
        self.assertEqual(result['introduced'], [])
        self.assertEqual(len(result['historical']), 1)
        self.assertFalse(baseline['required_quality_checks_waived'])

    def test_new_cycle_blocks_while_old_cycle_remains_visible(self):
        self.write('a.py', 'import b\n')
        self.write('b.py', 'import a\n')
        baseline = self.baseline()
        self.write('c.py', 'import d\n')
        self.write('d.py', 'import c\n')
        result = analyze_project(self.root, baseline=baseline, changed_paths=['c.py', 'd.py'])
        self.assertFalse(result['change_ok'])
        self.assertEqual(len(result['historical']), 1)
        self.assertEqual(result['introduced'][0]['subjects'], ['c.py', 'd.py'])

    def test_incremental_update_reads_only_changes_and_propagates_dependents(self):
        self.write('app.py', 'from core import value\n')
        self.write('core.py', 'value = 1\n')
        baseline = self.baseline()
        self.write('core.py', 'value = 2\n')
        result = analyze_project(self.root, baseline=baseline, changed_paths=['core.py'])
        self.assertTrue(result['change_ok'])
        self.assertEqual(result['coverage']['files_read'], 1)
        self.assertEqual(result['coverage']['affected'], ['app.py', 'core.py'])

    def test_reverse_dependency_and_coverage_errors_fail_change(self):
        self.write('ui/view.py', 'value = 1\n')
        self.write('core/data.py', 'value = 1\n')
        rules = {'max_lines': 1000, 'forbidden_dependencies': [['core/', 'ui/']]}
        baseline = self.baseline(rules)
        self.write('core/data.py', 'import ui.view\n')
        result = analyze_project(self.root, baseline=baseline, changed_paths=['core/data.py'], rules=rules)
        self.assertEqual(result['introduced'][0]['kind'], 'forbidden-dependency')
        self.write('core/data.py', 'invalid (\n')
        self.assertFalse(analyze_project(self.root, baseline=baseline, changed_paths=['core/data.py'], rules=rules)['change_ok'])

    def test_rule_change_requires_rebaseline_and_keeps_history(self):
        self.write('a.py', 'value = 1\n')
        baseline = self.baseline()
        report = analyze_project(self.root, baseline=baseline, changed_paths=[], rules={'max_lines': 50})
        self.assertFalse(report['baseline_compatible'])
        self.assertFalse(report['change_ok'])
        updated = make_baseline(report, previous=baseline, reason='reviewed rule change')
        self.assertEqual(len(updated['history']), 2)
        self.assertEqual(updated['history'][1]['previous_input_digest'], baseline['input_digest'])

    def test_unknown_language_is_explicit_and_scope_escape_rejected(self):
        self.write('main.rs', 'fn main() {}\n')
        result = analyze_project(self.root)
        self.assertEqual(result['coverage']['unknown'], ['main.rs'])
        baseline = self.baseline()
        result = analyze_project(self.root, baseline=baseline, changed_paths=['main.rs'])
        self.assertFalse(result['change_ok'])
        with self.assertRaisesRegex(ValueError, 'PATH_ESCAPE'):
            analyze_project(self.root, baseline=self.baseline(), changed_paths=['../other.py'])
