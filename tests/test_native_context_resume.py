import json
import unittest

from gsp.codex_worker_adapter import correction_context


class NativeContextResumeTests(unittest.TestCase):
    def setUp(self):
        self.thread = '01a21b31-2815-72f2-9964-88ee78efc61b'
        self.contexts = {'task-uid-1': {'native_session_id': self.thread, 'model_alias': 'group/deepseek-2'}}
        self.job = {'phase': 'execute', 'task': {'id': 'T01'}, 'task_contract': {'task_uid': 'task-uid-1'}}

    def test_same_task_correction_resumes_exact_native_context_after_restore(self):
        restored = json.loads(json.dumps(self.contexts))
        self.assertEqual(correction_context(self.job, restored, correcting=True), self.thread)

    def test_independent_task_and_reviewer_use_new_context(self):
        self.assertIsNone(correction_context(self.job, self.contexts, correcting=False))
        self.assertIsNone(correction_context({**self.job, 'phase': 'review'}, self.contexts, correcting=True))
        other = {**self.job, 'task_contract': {'task_uid': 'different-task'}}
        with self.assertRaisesRegex(ValueError, 'NATIVE_CONTEXT_REQUIRED'):
            correction_context(other, self.contexts, correcting=True)

    def test_missing_or_unverified_original_context_never_uses_last(self):
        for value in ({}, {'native_session_id': '--last', 'model_alias': 'group/deepseek-2'},
                      {'native_session_id': self.thread, 'model_alias': 'gpt-6.1-sol'}):
            with self.assertRaises(ValueError):
                correction_context(self.job, {'task-uid-1': value}, correcting=True)
