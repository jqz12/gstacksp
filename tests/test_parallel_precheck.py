import json
import os
import tempfile
import unittest
from pathlib import Path

try:
    from process_helpers import run, session_binding
except ModuleNotFoundError:
    from tests.process_helpers import run, session_binding


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts' / 'gsp.py'


class ParallelPrecheckCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-parallel-precheck-')
        self.env=os.environ.copy()
        self.env.pop('GSP_SESSION_CONTEXT',None)
        self.env.update(HOME=self.tmp.name,USERPROFILE=self.tmp.name,CODEX_HOME=str(Path(self.tmp.name)/'.codex'))
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Parallel Precheck Test')
        self.git('config', 'user.email', 'parallel-precheck@example.invalid')
        (self.project / 'README.md').write_text('# precheck\n', encoding='utf-8')
        self.git('add', 'README.md')
        self.git('commit', '-qm', 'chore: baseline')
        self.cli('init')
        self.cli('new', 'parent', '--title', '并行预检', '--intent', 'full-stack', '--size', 'large')
        state = self.read_state()
        binding=session_binding(self.project,state['work_uuid'],'main','codex','precheck-main')
        self.env['GSP_SESSION_CONTEXT']=str(self.project/binding.handle_path)
        (self.project / state['documents']['design']).write_text(
            '# full stack\n\n前后端独立实现、唯一集成归属与验收。\n', encoding='utf-8')
        self.cli('approve', 'parent', '--quote', '批准并行预检设计')
        self.cli('task-add', 'parent', 'T01', '--title', '后端', '--acceptance', '后端测试', '--role', 'backend')
        self.cli('task-add', 'parent', 'T02', '--title', '前端', '--acceptance', '前端测试', '--role', 'frontend')
        (self.project / self.read_state()['documents']['plan']).write_text(
            '# plan\n\nT01 后端；T02 前端。\n', encoding='utf-8')
        self.cli('seal-plan', 'parent')
        self.cli('scope', 'parent', '--off', '--quote', '独立 worktree 由后续绑定')
        self.cli('topology-select', 'parent', '--mode', 'in-place', '--quote', '父任务在 main 控制账本')

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        result = run(['git', *args], cwd=self.project, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def cli(self, *args, ok=True):
        result = run([__import__('sys').executable, str(CLI), '--project', str(self.project), *args],
                     capture_output=True, text=True, encoding='utf-8', env=self.env)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def read_state(self):
        return json.loads((self.project / '.gsp/runs/parent/state.json').read_text(encoding='utf-8'))

    def test_execute_requires_integration_owner(self):
        result = self.cli('advance', 'parent', '--to', 'execute', ok=False)
        self.assertIn('GIT_INTEGRATION_REQUIRED', result.stdout + result.stderr)

    def test_execute_allows_covered_integration(self):
        self.cli('task-add', 'parent', 'T03', '--title', '集成', '--acceptance', '集成测试',
                 '--role', 'integration', '--depends', 'T01,T02')
        (self.project / self.read_state()['documents']['plan']).write_text(
            '# plan\n\nT01 后端；T02 前端；T03 集成。\n', encoding='utf-8')
        self.cli('seal-plan', 'parent')
        self.cli('advance', 'parent', '--to', 'execute')
        self.assertEqual(self.read_state()['status'], 'execute')


if __name__ == '__main__':
    unittest.main()
