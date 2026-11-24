import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from process_helpers import run
except ModuleNotFoundError:  # direct unittest module invocation from repository root
    from tests.process_helpers import run


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts' / 'gsp.py'


class GitTopologyCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-topology-cli-')
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Topology CLI Test')
        self.git('config', 'user.email', 'topology-cli@example.invalid')
        (self.project / 'README.md').write_text('# topology cli\n', encoding='utf-8')
        self.git('add', 'README.md')
        self.git('commit', '-qm', 'chore: baseline')
        self.prepare_task()

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        result = run(['git', *args], cwd=self.project, capture_output=True, text=True,
                      encoding='utf-8')
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

    def state(self):
        return json.loads((self.project / '.gsp/runs/task/state.json').read_text(encoding='utf-8'))

    def prepare_task(self):
        self.cli('init')
        self.cli('new', 'task', '--title', '拓扑 CLI 验收')
        state = self.state()
        (self.project / state['documents']['design']).write_text(
            '# 拓扑 CLI 验收\n\n确认任务拓扑和工作区绑定。\n', encoding='utf-8')
        self.cli('approve', 'task', '--quote', '批准拓扑 CLI 验收设计')
        self.cli('task-add', 'task', 'T1', '--title', '实现拓扑 CLI', '--acceptance', '测试通过')
        (self.project / state['documents']['plan']).write_text(
            '# 计划\n\n- T1: 实现拓扑 CLI 并测试。\n', encoding='utf-8')
        self.cli('seal-plan', 'task')
        self.cli('scope', 'task', '--off', '--quote', '本任务按 GSP 规则记录源码范围')

    def test_topology_is_read_only_and_task_start_requires_selection(self):
        result = self.cli('topology', 'task')
        report = json.loads(result.stdout)
        self.assertEqual(report['current']['branch'], 'main')
        self.assertEqual(report['worktrees'][0]['path'], str(self.project.resolve()))
        self.assertEqual(report['recommendation']['mode'], 'branch')
        self.assertIsNone(self.state()['git_topology']['requested_mode'])

        self.cli('task-start', 'task', 'T1', ok=False)
        self.assertIn('GIT_TOPOLOGY_REQUIRED', self.cli('task-start', 'task', 'T1', ok=False).stderr)

    def test_in_place_choice_records_quote_and_workspace(self):
        self.cli('topology-select', 'task', '--mode', 'in-place', '--quote', '我明确选择 main 原地实现本任务')
        state = self.state()
        topology = state['git_topology']
        self.assertEqual(topology['requested_mode'], 'in-place')
        self.assertEqual(topology['effective_mode'], 'in-place')
        self.assertEqual(topology['control_root'], str(self.project.resolve()))
        self.assertEqual(topology['workspace_root'], str(self.project.resolve()))
        self.assertEqual(topology['decision_source'], 'caller-attributed-user')
        self.cli('advance', 'task', '--to', 'execute')
        self.cli('task-start', 'task', 'T1')

    def test_branch_choice_creates_gsp_bound_branch(self):
        self.cli('topology-select', 'task', '--mode', 'branch',
                 '--branch', 'feature/GSP-000001-T01-拓扑-cli', '--quote', '选择任务分支隔离')
        state = self.state()
        topology = state['git_topology']
        self.assertEqual(topology['effective_mode'], 'branch')
        self.assertEqual(topology['branch'], 'feature/GSP-000001-T01-拓扑-cli')
        self.assertEqual(self.git('branch', '--show-current'), 'feature/GSP-000001-T01-拓扑-cli')

    def test_worktree_choice_keeps_control_root_and_creates_source_workspace(self):
        self.cli('topology-select', 'task', '--mode', 'worktree',
                 '--branch', 'feature/GSP-000001-T01-拓扑-worktree', '--quote', '选择独立 worktree 隔离')
        state = self.state()
        topology = state['git_topology']
        workspace = Path(topology['workspace_root'])
        self.assertEqual(topology['effective_mode'], 'worktree')
        self.assertEqual(topology['control_root'], str(self.project.resolve()))
        self.assertNotEqual(workspace, self.project.resolve())
        self.assertTrue(workspace.is_dir())
        self.assertEqual(self.git('branch', '--show-current'), 'main')
        self.assertIn('feature/GSP-000001-T01-拓扑-worktree', self.git('worktree', 'list'))

    def test_external_link_is_optional_and_typed(self):
        self.cli('external-link', 'task', '--kind', 'issue', '--value', '#123')
        self.cli('external-link', 'task', '--kind', 'pr', '--value', '#456')
        links = self.state()['external_links']
        self.assertEqual(links, [{'kind': 'issue', 'value': '#123'}, {'kind': 'pr', 'value': '#456'}])

    def test_new_records_explicit_isolation_context(self):
        self.cli('new', 'isolated', '--title', '独立验证任务', '--independent-verification',
                 '--protect-existing')
        state = json.loads((self.project / '.gsp/runs/isolated/state.json').read_text(encoding='utf-8'))
        topology = state['git_topology']
        self.assertTrue(topology['independent_verification'])
        self.assertTrue(topology['protect_existing'])
        self.assertEqual(topology['recommended_mode'], 'worktree')

    def test_new_large_serial_task_does_not_record_worktree_recommendation(self):
        self.cli('new', 'large-serial', '--title', '大型串行任务', '--size', 'large',
                 '--intent', 'serial-multi-task')
        state = json.loads((self.project / '.gsp/runs/large-serial/state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['git_topology']['recommended_mode'], 'branch')


if __name__ == '__main__':
    unittest.main()
