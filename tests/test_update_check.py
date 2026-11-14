import json
import tempfile
import unittest
from pathlib import Path

from gsp.update_check import check
from gsp.update_policy import update_guard


class UpdateCheckTests(unittest.TestCase):
    def runner(self, output, code=0):
        def run(*args, **kwargs):
            class Result:
                returncode = code
                stdout = output
                stderr = 'offline' if code else ''
            return Result()
        return run

    def test_selects_latest_stable_tag_and_caches_for_a_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / 'check.json'
            output = 'a1\trefs/tags/v0.9.3\na2\trefs/tags/v0.10.0\na3\trefs/tags/v0.10.0^{}\n'
            first = check(cache, 'https://github.com/jqz12/gstacksp.git', 'v0.9.3', now=100, runner=self.runner(output))
            second = check(cache, 'https://github.com/jqz12/gstacksp.git', 'v0.9.3', now=100 + 10, runner=self.runner(''))
            self.assertEqual(first['latest'], 'v0.10.0')
            self.assertFalse(first['cached'])
            self.assertTrue(second['cached'])

    def test_network_failure_is_not_checked_and_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = check(Path(tmp) / 'check.json', 'https://github.com/jqz12/gstacksp.git', now=100, runner=self.runner('', 1))
            self.assertEqual(result['status'], 'NOT_CHECKED')
            self.assertTrue(json.loads((Path(tmp) / 'check.json').read_text())['status'] == 'NOT_CHECKED')

    def test_auto_update_is_blocked_by_active_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / '.gsp').mkdir(); (root / '.gsp' / 'active.json').write_text('{"task":"demo"}')
            result = update_guard(root)
            self.assertFalse(result['allowed']); self.assertEqual(result['reason'], 'active-task')
