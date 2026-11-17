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


class SessionCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-session-cli-')
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Session CLI Test')
        self.git('config', 'user.email', 'session-cli@example.invalid')
        (self.project / 'README.md').write_text('# cli\n', encoding='utf-8')
        self.git('add', 'README.md')
        self.git('commit', '-qm', 'chore: baseline')
        self.cli('init')
        self.cli('new', 'work', '--title', '跨会话 CLI', '--intent', 'cross-session')
        state = self.state()
        (self.project / state['documents']['design']).write_text('# design\n\nvalid\n', encoding='utf-8')
        self.cli('approve', 'work', '--quote', '批准')
        self.cli('task-add', 'work', 'T01', '--title', '服务', '--acceptance', '测试')

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        result = run(['git', *args], cwd=self.project, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def cli(self, *args, ok=True):
        result = run([sys.executable, str(CLI), '--project', str(self.project), *args],
                     capture_output=True, text=True, encoding='utf-8')
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def state(self):
        return json.loads((self.project / '.gsp/runs/work/state.json').read_text(encoding='utf-8'))

    def test_cli_writes_brief_report_and_history(self):
        self.cli('session-register', 'work', '--role', 'controller', '--host', 'codex',
                 '--thread', 'main-thread', '--write-mode', 'write', '--path', 'services')
        self.cli('session-register', 'work', '--role', 'worker', '--host', 'claude',
                 '--thread', 'worker-thread', '--task', 'T01', '--worktree', str(self.project / 'child'),
                 '--write-mode', 'write', '--path', 'services/quant-control.service')
        self.cli('session-brief', 'work', 'T01', '--session', 'S01', '--owner', 'S02',
                 '--title', '服务实现', '--objective', '实现服务契约', '--path', 'services/quant-control.service',
                 '--test', 'python -m unittest tests.test_service -v')
        self.cli('session-report', 'work', 'T01', '--session', 'S02', '--status', 'DONE',
                 '--summary', '实现并通过测试', '--files', 'services/quant-control.service',
                 '--tests', '1 passed', '--evidence', '.gsp/runs/work/evidence/x.json')
        self.cli('session-report', 'work', 'T01', '--session', 'S02', '--status', 'DONE',
                 '--summary', '补充接口注释', '--files', 'services/quant-control.service')
        sessions = self.project / self.state()['doc_dir'] / 'sessions'
        self.assertTrue((sessions / 'T01-brief.md').is_file())
        self.assertTrue((sessions / 'T01-report.md').is_file())
        self.assertTrue(list((sessions / 'history').glob('T01-report-*.md')))
        status = json.loads(self.cli('session-status', 'work').stdout)
        self.assertEqual(status['tasks']['T01']['report_status'], 'DONE')


if __name__ == '__main__':
    unittest.main()
