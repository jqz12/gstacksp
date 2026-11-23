import json
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from process_helpers import run
except ModuleNotFoundError:
    from tests.process_helpers import run


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts' / 'gsp.py'


class IntegrationOwnershipCliTests(unittest.TestCase):
    def make_project(self, initial=()):
        tmp = tempfile.TemporaryDirectory(prefix='gsp-integration-owner-')
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name) / 'project'
        self.project.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Integration Owner Test')
        self.git('config', 'user.email', 'integration-owner@example.invalid')
        (self.project / 'README.md').write_text('# owner\n', encoding='utf-8')
        self.git('add', 'README.md')
        self.git('commit', '-qm', 'chore: baseline')
        self.cli('init')
        self.cli('new', 'parent', '--title', '集成归属', '--intent', 'full-stack', '--size', 'large')
        state = self.read_state()
        (self.project / state['documents']['design']).write_text(
            '# 集成归属\n\n每个前后端任务必须有唯一 integration owner。\n', encoding='utf-8')
        self.cli('approve', 'parent', '--quote', '批准集成归属设计')
        for entry in initial:
            self.cli('task-add', 'parent', *entry)
        (self.project / self.read_state()['documents']['plan']).write_text(
            '# plan\n\nT01/T02/T03 独立实现与集成。\n', encoding='utf-8')
        self.cli('seal-plan', 'parent')

    def default_tasks(self, include_integration=True):
        rows = [
            ('T01', '--title', '后端', '--acceptance', '后端测试', '--role', 'backend'),
            ('T02', '--title', '前端', '--acceptance', '前端测试', '--role', 'frontend'),
        ]
        if include_integration:
            rows.append(('T03', '--title', '集成', '--acceptance', '集成测试',
                         '--role', 'integration', '--depends', 'T01,T02'))
        return rows

    def git(self, *args):
        result = run(['git', *args], cwd=self.project, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def cli(self, *args, ok=True):
        result = run([sys.executable, str(CLI), '--project', str(self.project), *args],
                     capture_output=True, text=True, encoding='utf-8')
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def read_state(self):
        return json.loads((self.project / '.gsp/runs/parent/state.json').read_text(encoding='utf-8'))

    def test_late_frontend_requires_owned_by(self):
        self.make_project(self.default_tasks())
        before = self.read_state()
        result = self.cli('task-add', 'parent', 'T04', '--title', '迟加前端',
                          '--acceptance', '前端测试', '--role', 'frontend', ok=False)
        self.assertIn('INTEGRATION_OWNER_REQUIRED', result.stdout + result.stderr)
        self.assertEqual(len(self.read_state()['tasks']), len(before['tasks']))

    def test_owned_by_updates_dependency_and_seal(self):
        self.make_project(self.default_tasks())
        self.cli('task-add', 'parent', 'T04', '--title', '迟加前端',
                 '--acceptance', '前端测试', '--role', 'frontend', '--owned-by', 'T03')
        state = self.read_state()
        task = next(item for item in state['tasks'] if item['id'] == 'T04')
        integration = next(item for item in state['tasks'] if item['id'] == 'T03')
        self.assertEqual(task['owned_by'], 'T03')
        self.assertIn('T04', integration['depends'])
        self.assertIsNone(state['plan_seal'])
        self.assertEqual(state['status'], 'plan')

    def test_done_integration_requires_reopen(self):
        self.make_project(self.default_tasks())
        path = self.project / '.gsp/runs/parent/state.json'
        state = self.read_state()
        integration = next(item for item in state['tasks'] if item['id'] == 'T03')
        integration['status'] = 'done'
        path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        result = self.cli('task-add', 'parent', 'T04', '--title', '迟加前端',
                          '--acceptance', '前端测试', '--role', 'frontend',
                          '--owned-by', 'T03', ok=False)
        self.assertIn('INTEGRATION_REOPEN_REQUIRED', result.stdout + result.stderr)
        after = self.read_state()
        integration = next(item for item in after['tasks'] if item['id'] == 'T03')
        self.assertEqual(integration['status'], 'done')
        self.assertNotIn('T04', integration['depends'])

    def test_integration_dependency_coverage_required(self):
        self.make_project([
            ('T01', '--title', '后端一', '--acceptance', '测试', '--role', 'backend'),
            ('T02', '--title', '后端二', '--acceptance', '测试', '--role', 'backend'),
        ])
        result = self.cli('task-add', 'parent', 'T03', '--title', '集成',
                          '--acceptance', '集成测试', '--role', 'integration',
                          '--depends', 'T01', ok=False)
        self.assertIn('INTEGRATION_COVERAGE_REQUIRED', result.stdout + result.stderr)
        self.assertIn('T02', result.stdout + result.stderr)

    def test_second_integration_rejected(self):
        self.make_project(self.default_tasks())
        result = self.cli('task-add', 'parent', 'T04', '--title', '第二个集成',
                          '--acceptance', '集成测试', '--role', 'integration',
                          '--depends', 'T01,T02', ok=False)
        self.assertIn('GIT_INTEGRATION_OWNER_CONFLICT', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
