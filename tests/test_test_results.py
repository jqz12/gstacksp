import unittest
from gsp.runtime import test_count


class TestResultsTests(unittest.TestCase):
    def test_cargo_sums_nonempty_and_empty_targets(self):
        text='test result: ok. 23 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out;\n'
        text+='test result: ok. 0 passed; 0 failed; 7 ignored; 0 measured; 0 filtered out;\n'
        text+='test result: ok. 4 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out;\n'
        self.assertEqual(test_count(text),27)

    def test_cargo_all_empty_or_ignored_is_zero(self):
        self.assertEqual(test_count('test result: ok. 0 passed; 0 failed; 7 ignored; 0 measured; 0 filtered out;'),0)

    def test_cargo_summary_does_not_override_conflicting_generic_history(self):
        text='Previously 500 passed\ntest result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out;'
        self.assertEqual(test_count(text),2)

    def test_generic_zero_history_remains_conservative(self):
        self.assertEqual(test_count('Previously 50 passed\nRan 0 tests'),0)

    def test_structured_failure_counts_retained(self):
        from gsp.test_results import cargo_summary
        result=cargo_summary('test result: FAILED. 2 passed; 1 failed; 3 ignored; 0 measured; 4 filtered out;')
        self.assertEqual(result,{'framework':'cargo','targets':1,'passed':2,'failed':1,'ignored':3,'filtered':4})
