"""Cross-feature integration for the workflow efficiency repair."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gsp.build_context import BUILD_LEASE_NAME, compatibility_key
from gsp.hosts import terminate_owned
from gsp.runtime import Project, WorkflowError, atomic, dispatch, parser, read
from tests import test_topology_lifecycle as topology_lifecycle
from tests.process_helpers import popen, run as run_process, session_binding


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts' / 'gsp.py'


def clean_environment(**updates):
    env = {key: value for key, value in os.environ.items()
           if key not in {'CARGO_TARGET_DIR', 'BUILD_ROOT'}}
    env.update(updates)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    return env


def wait_until(predicate, timeout=10, interval=.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


class WorkflowEfficiencyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-efficiency-int-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.contexts = {}

    def bind_legacy_fixture(self, project, state):
        # These scenarios retain the published execution/lease contract; the
        # modern convergence declarations have separate normal-entry coverage.
        state.pop('convergence_schema', None)
        project.write(state, 'fixture', 'legacy efficiency scenario',
                      action_uid='fixture:legacy-efficiency')
        binding=session_binding(project.root,state['work_uuid'],'main','codex','efficiency-main')
        self.contexts[str(project.root.resolve())]=str(project.root/binding.handle_path)

    def call(self, *argv, ok=True, env=None, timeout=120, root=None):
        target = Path(root or self.root)
        child_env=dict(env if env is not None else clean_environment())
        child_env.pop('GSP_SESSION_CONTEXT',None)
        if str(target.resolve()) in self.contexts:
            child_env['GSP_SESSION_CONTEXT']=self.contexts[str(target.resolve())]
        child_env.update(HOME=self.tmp.name,USERPROFILE=self.tmp.name,
                         CODEX_HOME=str(Path(self.tmp.name)/'.codex'))
        result = run_process(
            [sys.executable, str(CLI), '--project', str(target), *argv],
            cwd=target, capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=timeout, env=child_env)
        if ok is True:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        elif ok is False:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def git(self, *argv, cwd=None):
        result = run_process(
            ['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
             '-c', 'core.hooksPath=', *argv],
            cwd=cwd or self.root, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def run_id(self):
        return 'work'

    def state(self):
        return read(self.root / '.gsp' / 'runs' / self.run_id() / 'state.json')

    def state_bytes(self):
        return (self.root / '.gsp' / 'runs' / self.run_id() / 'state.json').read_bytes()

    def document(self, kind):
        state = self.state()
        relative = (state.get('documents') or {}).get(kind)
        if relative:
            return self.root / relative
        return self.root / state.get('doc_dir', 'docs/gsp/tasks/work') / f'{kind}.md'

    def initialize_git(self):
        self.call('init')
        (self.root / 'source.txt').write_text('baseline\n', encoding='utf-8')
        self.git('init', '-q', '-b', 'main')
        self.git('add', '.gitignore', 'source.txt')
        self.git('commit', '-qm', 'baseline')

    def public_five_task_lifecycle(self):
        self.initialize_git()
        self.call('new', self.run_id(), '--title', 'Efficiency integration',
                  '--intent', 'single-task')
        self.bind_legacy_fixture(Project(self.root),self.state())
        self.document('design').write_text(
            '# Design\nFive tasks exercise public delivery and evidence selection.\n',
            encoding='utf-8')
        self.call('task-add', self.run_id(), 'T01', '--title', 'First',
                  '--acceptance', 'First behavior passes')
        self.assertEqual(self.state()['status'], 'design')
        self.call('approve', self.run_id(), '--quote', 'SYNTHETIC fixture approval')
        for index in range(2, 6):
            self.call('task-add', self.run_id(), f'T{index:02}', '--title', f'Task {index}',
                      '--acceptance', 'Behavior passes', '--depends', f'T{index - 1:02}')
        self.document('plan').write_text(
            '# Plan\nT01 through T05 run the same real check and final regression.\n',
            encoding='utf-8')
        self.call('seal-plan', self.run_id())
        self.call('scope', self.run_id(), '--off', '--quote', 'SYNTHETIC no freeze')
        self.call('topology', self.run_id())
        self.call('topology-select', self.run_id(), '--mode', 'in-place',
                  '--quote', 'SYNTHETIC fixture writer')
        self.call('advance', self.run_id(), '--to', 'execute')

        program = self.root / 'fixture_test.py'
        program.write_text(
            'import unittest\n'
            'class Check(unittest.TestCase):\n'
            ' def test_fixture(self): self.assertEqual(2 + 2, 4)\n'
            'unittest.main()\n', encoding='utf-8')
        return program

    def run_check(self, task, kind, program):
        payload = json.loads(self.call(
            'run', self.run_id(), '--task', task, '--kind', kind, '--',
            sys.executable, str(program)).stdout)
        self.assertEqual(payload['status'], 'pass')
        return payload

    def test_public_five_task_lifecycle_and_preflight(self):
        program = self.public_five_task_lifecycle()
        tests_receipt = None
        for index in range(1, 6):
            task = f'T{index:02}'
            self.call('task-start', self.run_id(), task)
            tests = self.run_check(task, 'tests', program)
            if index == 1:
                tests_receipt = tests
                self.run_check(task, 'check', program)
            self.call('task-done', self.run_id(), task)

        first = next(row for row in self.state()['tasks'] if row['id'] == 'T01')
        self.assertEqual(first['evidence'], tests_receipt['id'])
        self.assertEqual(self.state()['status'], 'execute')

        final = self.run_check('all', 'tests', program)
        self.assertEqual(final['task'], 'all')
        self.call('advance', self.run_id(), '--to', 'review')
        review = self.document('review')
        review.write_text(
            '# Review\nFive-task public fixture reviewed; no blocking findings.\n',
            encoding='utf-8')
        self.call('review', self.run_id(), '--reviewer', 'self', '--blocking', '0')
        self.call('advance', self.run_id(), '--to', 'checking')
        self.call('advance', self.run_id(), '--to', 'ready')
        self.call('ready', self.run_id())
        self.assertEqual(self.state()['status'], 'ready')

        brief = json.loads(self.call('status', self.run_id(), '--brief').stdout)
        self.assertEqual(brief['recorded_status'], 'ready')
        self.assertEqual(brief['validation'], 'NOT_CHECKED')
        self.assertTrue(brief['not_authoritative'])
        self.assertTrue(all(row['status'] == 'done' for row in brief['tasks']))

        before = self.state()
        raw = self.state_bytes()
        observation = json.loads(self.call(
            'observe-head', self.run_id(), '--reason', 'SYNTHETIC same head').stdout)
        self.assertFalse(observation['changed'])
        self.assertEqual(observation['reason'], 'no-change')
        self.assertEqual(self.state_bytes(), raw)
        self.assertEqual(self.state()['review'], before['review'])

        def forbidden(*args, **kwargs):
            raise AssertionError('close preflight must not launch Git or scan source')

        project = Project(self.root)
        with patch('subprocess.run', forbidden):
            with patch('gsp.runtime.snapshot', forbidden):
                result, success = dispatch(
                    project, parser().parse_args([
                        'close', self.run_id(), '--reason', 'missing closure condition']))
        self.assertFalse(success)
        self.assertEqual(result['blockers'][0]['code'], 'WORK_CLOSURE_CONDITION_REQUIRED')
        self.assertEqual(self.state_bytes(), raw)

    def prepare_internal_cargo_project(self, root, shared):
        project = Project(root)
        project.initialize()
        (root / 'Cargo.toml').write_text(
            '[package]\nname = "fixture"\nversion = "0.0.0"\nedition = "2021"\n',
            encoding='utf-8')
        self.git('init', '-q', '-b', 'main', cwd=root)
        self.git('add', '.gitignore', 'Cargo.toml', cwd=root)
        self.git('commit', '-qm', 'baseline', cwd=root)

        def cmd(*argv):
            result, success = dispatch(project, parser().parse_args(list(argv)))
            self.assertTrue(success, result)
            return result

        cmd('new', 'work', '--title', 'Build integration', '--intent', 'single-task')
        state = read(root / '.gsp' / 'runs' / 'work' / 'state.json')
        self.bind_legacy_fixture(project,state)
        project.document(state, 'design').write_text(
            '# Design\nBuild context and lease integration.\n', encoding='utf-8')
        cmd('approve', 'work', '--quote', 'SYNTHETIC fixture approval')
        cmd('task-add', 'work', 'T01', '--title', 'Run',
            '--acceptance', 'Real child receives build context')
        project.document(state, 'plan').write_text(
            '# Plan\nT01 exercises the native command path.\n', encoding='utf-8')
        cmd('seal-plan', 'work')
        cmd('scope', 'work', '--off', '--quote', 'SYNTHETIC no freeze')
        cmd('topology-select', 'work', '--mode', 'in-place',
            '--quote', 'SYNTHETIC fixture writer')
        cmd('advance', 'work', '--to', 'execute')
        cmd('task-start', 'work', 'T01')
        key = compatibility_key(project, clean_environment())
        atomic(root / '.gsp' / 'build-policy.json', {
            'build_cache': {
                'mode': 'reuse-exclusive',
                'root': str(shared),
                'context': {'compatibility_key': key},
            },
        })
        return project

    def test_conflicting_build_environment_rejected_before_child(self):
        shared = Path(self.tmp.name) / 'shared-target'
        self.prepare_internal_cargo_project(self.root, shared)
        atomic(self.root / '.gsp' / 'build-policy.json', {
            'build_cache': {'mode': 'isolated'},
        })
        marker = Path(self.tmp.name) / 'child-started.txt'
        program = self.root / 'probe.py'
        program.write_text(
            'from pathlib import Path\n'
            f'Path({str(marker)!r}).write_text("started", encoding="utf-8")\n'
            'print("Ran 1 test in 0.001s")\n'
            'print("OK")\n', encoding='utf-8')
        conflict = clean_environment(
            CARGO_TARGET_DIR=str(Path(self.tmp.name) / 'ambient-target'))
        result = self.call(
            'run', self.run_id(), '--task', 'T01', '--kind', 'tests', '--',
            sys.executable, str(program), ok=False, env=conflict)
        self.assertIn('BUILD_CONTEXT_CONFLICT', result.stdout + result.stderr)
        self.assertFalse(marker.exists())
        self.assertNotIn('inflight', self.state())

        success = json.loads(self.call(
            'run', self.run_id(), '--task', 'T01', '--kind', 'tests', '--',
            sys.executable, str(program)).stdout)
        self.assertEqual(success['status'], 'pass')
        self.assertEqual(success['build_context']['mode'], 'isolated')
        self.assertTrue(Path(success['build_context']['root']).is_relative_to(self.root.resolve()))
        self.assertEqual(success['build_context']['root'],
                         success['build_context']['env']['CARGO_TARGET_DIR'])
        self.assertTrue(marker.exists())

    def test_child_same_head_observation_is_noop(self):
        fixture = topology_lifecycle.TopologyLifecycleTests(methodName='runTest')
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.setup_git()
        fixture.prepare()
        fixture.cmd('topology-select', 'work', '--mode', 'in-place',
                    '--quote', 'SYNTHETIC control writer')
        fixture.cmd('topology-select', 'work', '--task', 'T01', '--mode', 'worktree',
                    '--quote', 'SYNTHETIC child isolation')
        fixture.cmd('advance', 'work', '--to', 'execute')
        fixture.cmd('task-start', 'work', 'T01')
        fixture.check('T01')
        fixture.cmd('task-done', 'work', 'T01')
        state_file = fixture.root / '.gsp' / 'runs' / 'work' / 'state.json'
        before_state = fixture.state()
        before_bytes = state_file.read_bytes()
        history = list(before_state['tasks'][0]['topology'].get('head_history') or [])

        result = fixture.cmd('observe-head', 'work', '--task', 'T01',
                             '--reason', 'SYNTHETIC unchanged child head')

        self.assertFalse(result['changed'])
        self.assertFalse(result['identity_changed'])
        self.assertEqual(state_file.read_bytes(), before_bytes)
        self.assertEqual(fixture.state()['tasks'][0]['evidence'],
                         before_state['tasks'][0]['evidence'])
        self.assertEqual(fixture.state()['tasks'][0]['topology'].get('head_history') or [],
                         history)

    def test_cross_control_root_lease_and_live_run_status(self):
        shared = Path(self.tmp.name) / 'shared-target'
        first_root = Path(self.tmp.name) / 'first'
        second_root = Path(self.tmp.name) / 'second'
        first_root.mkdir()
        second_root.mkdir()
        self.prepare_internal_cargo_project(first_root, shared)
        self.prepare_internal_cargo_project(second_root, shared)

        long_program = first_root / 'long_probe.py'
        release = Path(self.tmp.name) / 'release_probe.txt'
        long_program.write_text(
            'import pathlib, time\n'
            'release = pathlib.Path(' + repr(str(release)) + ')\n'
            'deadline = time.monotonic() + 90\n'
            'while not release.exists() and time.monotonic() < deadline:\n'
            '    time.sleep(0.1)\n'
            'print("Ran 1 test in 0.001s")\n'
            'print("OK")\n', encoding='utf-8')
        lease_path = shared / BUILD_LEASE_NAME
        process = popen(
            [sys.executable, str(CLI), '--project', str(first_root),
             'run', 'work', '--task', 'T01', '--kind', 'tests', '--timeout', '60',
             '--', sys.executable, str(long_program)],
            cwd=first_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', env=clean_environment(
                GSP_SESSION_CONTEXT=self.contexts[str(first_root.resolve())],
                HOME=self.tmp.name, USERPROFILE=self.tmp.name,
                CODEX_HOME=str(Path(self.tmp.name)/'.codex')))
        try:
            def current_lease():
                if not lease_path.is_file():
                    return None
                try:
                    document = read(lease_path)
                except WorkflowError:
                    return None
                return document if document.get('phase') == 'running' else None

            lease = wait_until(
                current_lease, timeout=15)
            self.assertIsNotNone(lease, 'first writer did not acquire the build lease')
            self.assertEqual(lease['phase'], 'running')
            first_state = read(first_root / '.gsp' / 'runs' / 'work' / 'state.json')
            evidence_id = first_state['inflight']['id']

            status = json.loads(self.call(
                'run-status', 'work', '--evidence', evidence_id,
                env=clean_environment(), root=first_root).stdout)
            self.assertTrue(status['found'])
            self.assertFalse(status['terminal'])
            self.assertEqual(status['status'], 'running')
            self.assertEqual(status['validation'], 'NOT_CHECKED')
            self.assertTrue(status['not_authoritative'])

            second_program = second_root / 'short_probe.py'
            second_program.write_text('print("Ran 1 test in 0.001s")\nprint("OK")\n',
                                      encoding='utf-8')
            blocked = self.call(
                'run', 'work', '--task', 'T01', '--kind', 'tests', '--',
                sys.executable, str(second_program), ok=False,
                env=clean_environment(), timeout=30, root=second_root)
            self.assertIn('BUILD_LEASE_HELD', blocked.stdout + blocked.stderr)

            release.write_text('go', encoding='utf-8')
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            receipt = read(first_root / '.gsp' / 'runs' / 'work' / 'evidence'
                           / evidence_id / 'receipt.json')
            self.assertEqual(receipt['status'], 'pass')
            self.assertFalse(lease_path.exists())
        finally:
            if process.poll() is None:
                terminate_owned(process)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


if __name__ == '__main__':
    unittest.main()
