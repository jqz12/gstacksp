from process_helpers import docfolder
from process_helpers import document
"""Real unittest subprocesses in isolated fixture projects, no paid/live services."""
from pathlib import Path
import json
import subprocess
from process_helpers import run, popen
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts/gsp.py'


class EndToEndTests(unittest.TestCase):
    def test_two_tasks_installed_hosts_and_real_regression(self):
        with tempfile.TemporaryDirectory(prefix='gsp-e2e-') as temp:
            project = Path(temp) / '项目 project space'
            project.mkdir()

            def call(*args, script=CLI, ok=True):
                r = run([sys.executable, str(script), '--project', str(project), *args],
                                   capture_output=True, encoding='utf-8', errors='replace')
                if ok: self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                else: self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                return r

            call('install', '--host', 'both', '--optional', 'design-review')
            call('install', '--host', 'both', '--optional', 'design-review')  # ownership-aware idempotence
            for base in ('.claude', '.agents'):
                skills = list((project / base / 'skills').glob('gsp-*/SKILL.md'))
                self.assertEqual(len(skills), 23)
                self.assertFalse((project / base / 'skills/gsp-retro').exists())
                installed_cli = project / base / 'skills/gsp-workflow/scripts/gsp.py'
                report = json.loads(call('health', '--host', 'both', script=installed_cli).stdout)
                self.assertTrue(report['static_ok'])
                self.assertFalse(report['live_verified'])
                self.assertTrue((installed_cli.parent.parent / 'THIRD_PARTY_NOTICES.md').is_file())
            call('init')
            self.assertIn('.gsp/', (project / '.gitignore').read_text(encoding='utf-8'))
            call('new', 'hello', '--title', 'Fixture greeting', '--mode', 'unattended', '--quote', 'SYNTHETIC TEST ONLY: use recommendations')
            docs = docfolder(project,'hello')
            document(project, 'hello', 'design').write_text('# Fixture design\nNormalize whitespace and greet a name.\n', encoding='utf-8')
            call('approve', 'hello', '--quote', 'SYNTHETIC TEST ONLY: approve this fixture design')
            call('task-add', 'hello', 'T1', '--title', 'Normalize', '--acceptance', 'Whitespace is trimmed')
            call('task-add', 'hello', 'T2', '--title', 'Greet', '--acceptance', 'Greeting uses normalized name', '--depends', 'T1')
            document(project, 'hello', 'plan').write_text('# Fixture plan\nT1 normalize and test. T2 greet and integration test.\n', encoding='utf-8')
            call('seal-plan', 'hello')
            call('scope', 'hello', '--off', '--quote', 'SYNTHETIC TEST ONLY: no freeze')
            call('task-start', 'hello', 'T2', ok=False)
            call('task-start', 'hello', 'T1')
            (project / 'app.py').write_text('def normalize(name):\n    return name.strip()\n', encoding='utf-8')
            test = project / 'test_app.py'
            test.write_text("import unittest\nfrom app import normalize\nclass AppTests(unittest.TestCase):\n def test_normalize(self): self.assertEqual(normalize(' Ada '), 'Ada')\n", encoding='utf-8')
            call('run', 'hello', '--task', 'T1', '--kind', 'tests', '--', sys.executable, '-m', 'unittest', '-v')
            call('task-done', 'hello', 'T1')
            call('task-start', 'hello', 'T2')
            with (project / 'app.py').open('a', encoding='utf-8') as stream:
                stream.write("\ndef greet(name):\n    return 'Hello ' + normalize(name)\n")
            with test.open('a', encoding='utf-8') as stream:
                stream.write(" def test_greet(self):\n  from app import greet\n  self.assertEqual(greet(' Ada '), 'Hello Ada')\n")
            call('run', 'hello', '--task', 'T2', '--kind', 'tests', '--', sys.executable, '-m', 'unittest', '-v')
            call('task-done', 'hello', 'T2')
            (docs / 'review.md').write_text('# Fixture review\nSelf reviewed normalize, greet and both real assertions; browser not applicable.\n', encoding='utf-8')
            call('review', 'hello', '--reviewer', 'self', '--blocking', '0')
            call('ready', 'hello', ok=False)
            call('run', 'hello', '--task', 'all', '--kind', 'tests', '--', sys.executable, '-m', 'unittest', '-v')
            call('ready', 'hello')
            self.assertIn('READY', (docs / 'delivery.md').read_text(encoding='utf-8'))
            state = json.loads(call('status', 'hello').stdout)
            receipts = [json.loads((project / p).read_text(encoding='utf-8')) for p in state['evidence']]
            self.assertEqual([r['tests_ran'] for r in receipts], [1, 2, 2])
            call('save', 'hello')
            call('resume', 'hello')
            call('learn', '--text', 'Fixture normalization trims surrounding whitespace', '--source', 'hello/T1 test_app.py')
            rows = json.loads(call('learn', '--query', 'normalization').stdout)
            self.assertEqual(len(rows), 1)

    def test_summary_parser_unknown_and_zero(self):
        sys.path.insert(0, str(ROOT))
        from gsp.runtime import test_count
        self.assertEqual(test_count('Ran 0 tests\nprevious run: 3 passed'), 0)
        self.assertIsNone(test_count('Looks good'))


if __name__ == '__main__':
    unittest.main()
