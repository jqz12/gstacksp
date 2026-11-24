import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from gsp.build_context import (BUILD_LEASE_NAME, build_lease, command_environment,
                               compatibility_key, resolve_context)
from gsp.hosts import identity
from gsp.runtime import Project, WorkflowError, atomic, run_check


PROBE = '''import json, os
print(json.dumps({
    "cwd": str(__import__("pathlib").Path.cwd()),
    "CARGO_TARGET_DIR": os.environ.get("CARGO_TARGET_DIR"),
    "BUILD_ROOT": os.environ.get("BUILD_ROOT"),
    "TMP": os.environ.get("TMP"),
}, sort_keys=True))
print("Ran 1 test in 0.001s")
print("OK")
'''


class BuildContextTests(unittest.TestCase):
    def setUp(self):
        self.saved_build_env = {key: os.environ.pop(key) for key in
                                ('CARGO_TARGET_DIR', 'BUILD_ROOT') if key in os.environ}
        self.addCleanup(lambda: os.environ.update(self.saved_build_env))
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-build-context-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.project = Project(self.root)
        self.project.initialize()
        self.git('init', '-q', '-b', 'main')
        (self.root / 'README.md').write_text('# fixture\n', encoding='utf-8')
        (self.root / 'Cargo.toml').write_text(
            '[package]\nname = "fixture"\nversion = "0.0.0"\nedition = "2021"\n',
            encoding='utf-8')
        self.git('add', '.gitignore', 'README.md', 'Cargo.toml')
        self.git('commit', '-qm', 'baseline')
        self.head = self.git('rev-parse', 'HEAD')
        self.documents = {
            'design': 'docs/work/2026/11/GSP-000034-build/spec.md',
            'plan': 'docs/work/2026/11/GSP-000034-build/plan.md',
        }
        for rel in self.documents.values():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# fixture\n', encoding='utf-8')
        self.state = {
            'schema': 1, 'id': 'build', 'number': 'GSP-000034',
            'title': 'Build context', 'root': str(self.root),
            'project_id': self.project.identity()['id'],
            'documents': self.documents, 'status': 'execute',
            'scope': {'mode': 'off'}, 'tasks': [
                {'id': 'T01', 'title': 'one', 'acceptance': 'probe', 'depends': [],
                 'status': 'running', 'role': 'implementation'},
                {'id': 'T02', 'title': 'two', 'acceptance': 'probe', 'depends': ['T01'],
                 'status': 'pending', 'role': 'implementation'},
            ],
            'evidence': [], 'verification_epoch': 0,
            'git_topology': {
                'status': 'selected', 'enforce': True, 'effective_mode': 'in-place',
                'workspace_root': str(self.root), 'branch': 'main',
                'head': self.head, 'intent': 'single-task',
            },
        }

    def git(self, *argv):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith('GIT_CONFIG_')}
        result = subprocess.run(
            ['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
             '-c', 'core.hooksPath=', *argv],
            cwd=self.root, capture_output=True, text=True, encoding='utf-8', env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def policy(self, value):
        atomic(self.root / '.gsp' / 'build-policy.json', value)

    def context(self, task='T01', environ=None):
        return resolve_context(self.project, self.state, task, [sys.executable],
                               environ or {}, source_root=self.root)

    def test_isolated_context_is_stable_across_tasks(self):
        first = self.context('T01')
        second = self.context('T02')

        self.assertEqual(first['mode'], 'isolated')
        self.assertEqual(first['root'], second['root'])
        self.assertTrue(Path(first['root']).is_relative_to(self.root.resolve()))
        self.assertIn('.gsp', Path(first['root']).parts)
        self.assertEqual(first['env']['CARGO_TARGET_DIR'], first['root'])
        self.assertEqual(first['env']['BUILD_ROOT'], first['root'])

    def test_command_environment_reaches_a_real_child(self):
        context = self.context()
        temp = self.root / '.gsp' / 'tmp' / 'probe'
        temp.mkdir(parents=True)
        env = command_environment(os.environ, context, {
            'TMP': str(temp), 'TEMP': str(temp), 'TMPDIR': str(temp),
        })
        result = subprocess.run([sys.executable, '-c', PROBE], cwd=self.root,
                                env=env, capture_output=True, text=True, encoding='utf-8')

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(payload['cwd'], str(self.root.resolve()))
        self.assertEqual(payload['CARGO_TARGET_DIR'], context['root'])
        self.assertEqual(payload['BUILD_ROOT'], context['root'])
        self.assertEqual(payload['TMP'], str(temp))

    def test_conflicting_ambient_target_is_rejected(self):
        with self.assertRaisesRegex(WorkflowError, 'BUILD_CONTEXT_CONFLICT'):
            self.context(environ={'CARGO_TARGET_DIR': str(self.root / 'other-target')})

    def test_reuse_exclusive_requires_root_and_compatibility_context(self):
        self.policy({'build_cache': {'mode': 'reuse-exclusive'}})
        with self.assertRaisesRegex(WorkflowError, 'BUILD_CONTEXT_INCOMPLETE'):
            self.context()

        shared = Path(self.tmp.name) / 'shared-target'
        key = compatibility_key(self.project)
        self.policy({'build_cache': {
            'mode': 'reuse-exclusive', 'root': str(shared),
            'context': {'compatibility_key': key},
        }})
        result = self.context()
        self.assertEqual(result['mode'], 'reuse-exclusive')
        self.assertEqual(result['root'], str(shared.resolve()))
        self.assertTrue(result['override_ambient'])

    def test_two_writers_cannot_share_one_target(self):
        shared = Path(self.tmp.name) / 'shared-target'
        key = compatibility_key(self.project)
        self.policy({'build_cache': {
            'mode': 'reuse-exclusive', 'root': str(shared),
            'context': {'compatibility_key': key},
        }})
        context = self.context()
        with build_lease(self.project, context, 'first'):
            with self.assertRaisesRegex(WorkflowError, 'BUILD_LEASE_HELD'):
                with build_lease(self.project, context, 'second'):
                    pass

    def test_dead_controller_with_live_child_keeps_the_lease(self):
        root = Path(self.tmp.name) / 'shared-target'
        self.policy({'build_cache': {
            'mode': 'reuse-exclusive', 'root': str(root),
            'context': {'compatibility_key': compatibility_key(self.project)},
        }})
        context = self.context()
        root = Path(context['root'])
        root.mkdir(parents=True, exist_ok=True)
        atomic(root / BUILD_LEASE_NAME, {
            'schema': 1, 'evidence_id': 'other', 'phase': 'running',
            'controller': {'pid': 999999, 'token': 'dead'},
            'child': identity(os.getpid()),
        })
        with self.assertRaisesRegex(WorkflowError, 'BUILD_LEASE_UNKNOWN'):
            with build_lease(self.project, context, 'mine'):
                pass

    def test_launch_window_unknown_is_not_reclaimed(self):
        shared = Path(self.tmp.name) / 'shared-target'
        self.policy({'build_cache': {
            'mode': 'reuse-exclusive', 'root': str(shared),
            'context': {'compatibility_key': compatibility_key(self.project)},
        }})
        context = self.context()
        shared.mkdir(parents=True, exist_ok=True)
        atomic(shared / BUILD_LEASE_NAME, {
            'schema': 1, 'evidence_id': 'other', 'phase': 'launching',
            'controller': {'pid': 999999, 'token': 'dead'}, 'child': None,
        })
        with self.assertRaisesRegex(WorkflowError, 'BUILD_LEASE_UNKNOWN'):
            with build_lease(self.project, context, 'mine'):
                pass

    def test_off_policy_injects_nothing(self):
        self.policy({'worktree_build': 'off'})
        context = self.context()
        self.assertEqual(context['mode'], 'off')
        self.assertIsNone(context['root'])
        self.assertEqual(context['env'], {})

    def test_non_cargo_project_injects_nothing(self):
        (self.root / 'Cargo.toml').unlink()
        context = self.context(environ={
            'CARGO_TARGET_DIR': str(self.root / 'ambient-target'),
            'BUILD_ROOT': str(self.root / 'ambient-target'),
        })
        self.assertEqual(context['mode'], 'off')
        self.assertIsNone(context['root'])
        self.assertEqual(context['env'], {})
        self.assertEqual(context['provenance'], 'non-cargo')

    def test_receipt_records_the_actual_build_context(self):
        program = self.root / 'probe_build.py'
        program.write_text(PROBE, encoding='utf-8')
        args = Namespace(task='T01', kind='tests', argv=[sys.executable, str(program)],
                         timeout=30, scan_timeout=60, runner=None)
        run = self.root / '.gsp' / 'runs' / 'build'

        receipt, ok = run_check(self.project, self.state, run, args)

        self.assertTrue(ok, receipt)
        self.assertEqual(receipt['build_context']['mode'], 'isolated')
        self.assertEqual(receipt['build_context']['root'], receipt['build_context']['env']['CARGO_TARGET_DIR'])
        output = (run / 'evidence' / receipt['id'] / 'stdout.txt').read_text(encoding='utf-8')
        payload = json.loads(output.splitlines()[0])
        self.assertEqual(payload['CARGO_TARGET_DIR'], receipt['build_context']['root'])
        self.assertFalse((Path(receipt['build_context']['root']) / BUILD_LEASE_NAME).exists())

    def test_failed_run_reuses_the_same_bound_root(self):
        first = self.context('T01')
        program = self.root / 'probe_fail.py'
        program.write_text('import sys\nsys.exit(7)\n', encoding='utf-8')
        args = Namespace(task='T01', kind='tests', argv=[sys.executable, str(program)],
                         timeout=30, scan_timeout=60, runner=None)
        run = self.root / '.gsp' / 'runs' / 'build'
        receipt, ok = run_check(self.project, self.state, run, args)
        self.assertFalse(ok)
        second = self.context('T01')
        self.assertEqual(first['root'], second['root'])
        self.assertEqual(receipt['build_context']['root'], first['root'])


if __name__ == '__main__':
    unittest.main()
