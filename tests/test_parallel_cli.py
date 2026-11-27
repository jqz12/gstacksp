import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from process_helpers import run, session_binding
except ModuleNotFoundError:
    from tests.process_helpers import run, session_binding


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts' / 'gsp.py'


class ParallelTopologyCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-parallel-cli-')
        self.env=os.environ.copy()
        self.env.pop('GSP_SESSION_CONTEXT',None)
        self.env.update(HOME=self.tmp.name,USERPROFILE=self.tmp.name,CODEX_HOME=str(Path(self.tmp.name)/'.codex'))
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Parallel CLI Test')
        self.git('config', 'user.email', 'parallel-cli@example.invalid')
        (self.project / 'README.md').write_text('# parallel\n', encoding='utf-8')
        self.git('add', 'README.md')
        self.git('commit', '-qm', 'chore: baseline')
        self.cli('init')
        self.cli('new', 'parent', '--title', '全栈并行任务', '--intent', 'full-stack')
        self.state = self.read_state()
        binding=session_binding(self.project,self.state['work_uuid'],'main','codex','parallel-main')
        self.env['GSP_SESSION_CONTEXT']=str(self.project/binding.handle_path)
        (self.project / self.state['documents']['design']).write_text(
            '# full stack\n\n前后端契约、独立验证和集成验收。\n', encoding='utf-8')
        self.cli('approve', 'parent', '--quote', '批准全栈并行设计')
        self.cli('task-add', 'parent', 'T01', '--title', '后端', '--acceptance', '后端测试', '--role', 'backend')
        self.cli('task-add', 'parent', 'T02', '--title', '前端', '--acceptance', '前端测试', '--role', 'frontend')
        self.cli('task-add', 'parent', 'T03', '--title', '集成', '--acceptance', '集成测试', '--role', 'integration', '--depends', 'T01,T02')
        state = self.read_state()
        (self.project / state['documents']['plan']).write_text('# Plan\n'+ '\n'.join(
            f'## Task {tid}: {tid}\nConvergence: '+json.dumps({'requires_slices':False,'single_outcome':True,'small':True})+'\n**Writes:** '+json.dumps({'paths':[path]})+'\n'
            for tid,path in (('T01','src/backend'),('T02','src/frontend'),('T03','src/integration'))),encoding='utf-8')
        self.cli('seal-plan', 'parent')
        self.cli('scope', 'parent', '--off', '--quote', '全栈任务由独立 worktree 隔离')

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        result = run(['git', *args], cwd=self.project, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def cli(self, *args, ok=True):
        result = run([sys.executable, str(CLI), '--project', str(self.project), *args],
                     capture_output=True, text=True, encoding='utf-8', env=self.env)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def read_state(self):
        return json.loads((self.project / '.gsp/runs/parent/state.json').read_text(encoding='utf-8'))

    def test_each_writable_child_gets_its_own_worktree_binding(self):
        self.cli('topology-select', 'parent', '--mode', 'in-place', '--quote', '父任务使用 main 原地控制账本')
        self.cli('advance', 'parent', '--to', 'execute')
        for task_id, suffix in [('T01', '后端'), ('T02', '前端'), ('T03', '集成')]:
            self.cli('topology-select', 'parent', '--task', task_id, '--mode', 'worktree', '--legacy-worktree',
                     '--branch', f'feature/GSP-000001-{task_id}-{suffix}', '--quote', f'{task_id} 使用独立 worktree')
        state = self.read_state()
        bindings = [task['topology'] for task in state['tasks']]
        self.assertEqual(len({item['workspace_root'] for item in bindings}), 3)
        self.assertEqual({item['branch'] for item in bindings}, {
            'feature/GSP-000001-T01-后端', 'feature/GSP-000001-T02-前端', 'feature/GSP-000001-T03-集成'})
        self.cli('task-start', 'parent', 'T01')


if __name__ == '__main__':
    unittest.main()
