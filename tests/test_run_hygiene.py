"""`run` isolates TMP and reclaims only entries created by this execution."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gsp.runtime import Project, WorkflowError, dispatch, parser, read


PROBE = '''import os, sys
from pathlib import Path
root = Path.cwd()
for key in ('TMP', 'TEMP'):
    folder = Path(os.environ[key])
    (folder / (key.lower() + '-probe')).mkdir(parents=True, exist_ok=True)
target = root / 'target' / 'tmp'
target.mkdir(parents=True, exist_ok=True)
(target / 'new-entry').mkdir(exist_ok=True)
print('Ran 1 test in 0.001s\\n\\nOK')
sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
'''


class RunHygieneTests(unittest.TestCase):
    def setUp(self):
        self.saved_build_env = {key: os.environ.pop(key) for key in
                                ('CARGO_TARGET_DIR', 'BUILD_ROOT') if key in os.environ}
        self.addCleanup(lambda: os.environ.update(self.saved_build_env))
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-run-hygiene-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = Project(self.root)
        self.project.initialize()
        self.git('init', '-q', '-b', 'main')
        (self.root / 'README.md').write_text('# fixture\n', encoding='utf-8')
        self.git('add', '.gitignore', 'README.md')
        self.git('commit', '-qm', 'baseline')
        self.cmd('new', 'work', '--title', 'Hygiene', '--size', 'small')
        state = self.state()
        # This published run-hygiene fixture predates the convergence schema.
        # Keep the product gate intact; only this synthetic work is legacy.
        state.pop('convergence_schema', None)
        self.project.write(
            state,
            'fixture-legacy-convergence',
            'legacy run-hygiene fixture created before convergence schema',
            action_uid='fixture:legacy-convergence:run-hygiene',
        )
        self.project.document(state, 'design').write_text(
            '# Design\nFixture design long enough for approval.\n', encoding='utf-8')
        self.cmd('approve', 'work', '--quote', 'SYNTHETIC fixture design')
        self.cmd('task-add', 'work', 'T01', '--title', 'Hygiene run',
                 '--acceptance', 'Receipt records hygiene')
        self.project.document(self.state(), 'plan').write_text(
            '# Plan\nT01 fixture check.\n', encoding='utf-8')
        self.cmd('seal-plan', 'work')
        self.cmd('scope', 'work', '--off', '--quote', 'SYNTHETIC no freeze')
        self.cmd('topology-select', 'work', '--mode', 'in-place',
                 '--quote', 'SYNTHETIC fixture in-place')
        self.cmd('advance', 'work', '--to', 'execute')
        self.cmd('task-start', 'work', 'T01')

    def git(self, *argv):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith('GIT_CONFIG_')}
        result = subprocess.run(
            ['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
             '-c', 'core.hooksPath=', *argv],
            cwd=self.root, capture_output=True, text=True, encoding='utf-8', env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def state(self):
        return read(self.root / '.gsp' / 'runs' / 'work' / 'state.json')

    def cmd(self, *argv, require_ok=True):
        values = list(argv)
        extra = []
        if '--' in values:
            split = values.index('--')
            extra = values[split + 1:]
            values = values[:split]
        args = parser().parse_args(values)
        args.argv = extra
        result, ok = dispatch(self.project, args)
        if require_ok:
            self.assertTrue(ok, result)
        return result

    def run_probe(self, exit_code=0):
        program = self.root / 'probe.py'
        program.write_text(PROBE, encoding='utf-8')
        return self.cmd('run', 'work', '--task', 'T01', '--kind', 'tests', '--',
                        sys.executable, str(program), str(exit_code), require_ok=False)

    def receipt(self):
        return read(self.root / self.state()['evidence'][-1])

    def policy(self, value):
        path = self.root / '.gsp' / 'build-policy.json'
        path.write_text(json.dumps(value), encoding='utf-8')

    def test_success_removes_isolation_directory_and_reclaims_new_entries(self):
        (self.root / 'Cargo.toml').write_text('[package]\nname = "fixture"\n', encoding='utf-8')
        preexisting = self.root / 'target' / 'tmp' / 'preexisting'
        preexisting.mkdir(parents=True)
        receipt = self.run_probe()
        hygiene = receipt['hygiene']
        self.assertFalse(hygiene['disabled'])
        self.assertEqual(hygiene['created'], 1)
        self.assertGreaterEqual(hygiene['removed'], 1)
        self.assertEqual(hygiene['retained'], 0)
        self.assertTrue(preexisting.is_dir())
        self.assertFalse((self.root / 'target' / 'tmp' / 'new-entry').exists())
        isolation = Path(hygiene['temp_isolation_dir'])
        self.assertFalse(isolation.exists())
        self.assertEqual(isolation.parent.name, 'runs')

    def test_failure_retains_newest_entry_up_to_the_limit(self):
        (self.root / 'Cargo.toml').write_text('[package]\nname = "fixture"\n', encoding='utf-8')
        self.policy({'retain_on_failure': {'max_entries': 1, 'max_bytes': 52428800},
                     'temp_roots': ['target/tmp']})
        program = self.root / 'probe_fail.py'
        program.write_text(
            "import sys\nfrom pathlib import Path\n"
            "base = Path.cwd() / 'target' / 'tmp'\n"
            "for name in ('a', 'b'):\n"
            "    (base / name).mkdir(parents=True, exist_ok=True)\n"
            "    (base / name / 'payload.bin').write_bytes(b'x' * 2048)\n"
            "sys.exit(3)\n", encoding='utf-8')
        self.cmd('run', 'work', '--task', 'T01', '--kind', 'tests', '--',
                 sys.executable, str(program), require_ok=False)
        hygiene = self.receipt()['hygiene']
        self.assertEqual(hygiene['created'], 2)
        self.assertEqual(hygiene['retained'], 1)
        self.assertGreaterEqual(hygiene['removed'], 1)
        self.assertGreater(hygiene['retained_bytes'], 0)
        self.assertEqual(sorted(path.name for path in (self.root / 'target' / 'tmp').iterdir()),
                         ['b'])

    def test_disabled_isolation_is_reported_and_creates_no_run_directory(self):
        self.policy({'temp_isolation': False})
        program = self.root / 'probe_noop.py'
        program.write_text('print("noop")\n', encoding='utf-8')
        self.cmd('run', 'work', '--task', 'T01', '--kind', 'tests', '--',
                 sys.executable, str(program), require_ok=False)
        receipt = self.receipt()
        self.assertTrue(receipt['hygiene']['disabled'])
        self.assertIsNone(receipt['hygiene']['temp_isolation_dir'])
        self.assertFalse((self.root / '.gsp' / 'tmp' / 'runs' / receipt['id']).exists())

    def test_truncated_logs_cannot_complete_a_task(self):
        program = self.root / 'probe_loud.py'
        program.write_text(
            "print('Ran 1 test in 0.001s')\n"
            "print('OK')\n"
            "print('x' * 4096)\n", encoding='utf-8')
        with patch('gsp.command_execution.MAX_STREAM_BYTES', 64):
            self.cmd('run', 'work', '--task', 'T01', '--kind', 'tests', '--',
                     sys.executable, str(program), require_ok=False)
        receipt = self.receipt()
        self.assertEqual(receipt['status'], 'log-truncated')
        self.assertTrue(receipt['log_integrity']['stdout']['truncated'])
        with self.assertRaisesRegex(WorkflowError, 'EVIDENCE_APPLICABLE_FAILURE'):
            dispatch(self.project, parser().parse_args(['task-done', 'work', 'T01']))


if __name__ == '__main__':
    unittest.main()
