import unittest

from gsp.hosts import codex_argv
from gsp.runtime import WorkflowError


class WorkerModelPolicyTests(unittest.TestCase):
    def test_every_worker_role_explicitly_requests_deepseek_max(self):
        for phase in ('plan', 'execute', 'fix-task', 'review', 'fix-review'):
            with self.subTest(phase=phase):
                argv = codex_argv({'phase': phase, 'schema': 'schema.json', 'result': 'result.json'}, 'codex.exe')
                self.assertEqual(argv[argv.index('--model') + 1], 'group/deepseek-2')
                self.assertIn('model_reasoning_effort="max"', argv)
                self.assertNotIn('--last', argv)

    def test_worker_cannot_inherit_or_override_to_a_main_model(self):
        for model in ('gpt-6.1-sol', '', None):
            with self.subTest(model=model), self.assertRaisesRegex(WorkflowError, 'WORKER_MODEL_REQUIRED'):
                codex_argv({'phase': 'execute', 'schema': 's', 'result': 'r', 'model_alias': model}, 'codex.exe')
