"""Driver launch must reuse the bounded shared process executor."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from gsp.driver import Driver
from gsp.core import WorkflowError as CoreWorkflowError
from gsp.runtime import (
    WorkflowError,
    _assert_execution_control,
    _execution_control_guard,
    _invoke_check_runner,
    digest,
    inspect_lock,
    locked,
    suspend_writer_lock,
)
from gsp import command_execution

ROOT = Path(__file__).resolve().parents[1]


class _Project:
    def __init__(self, root):
        self.root = root
        self.meta = root / '.gsp'

    def paths(self, task):
        return self.root / '.gsp/runs/work', self.root / 'docs/work/work'


class DriverExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gsp-driver-execution-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = _Project(self.root)
        self.driver = Driver(self.project, 'work')
        self.driver.folder.mkdir(parents=True)
        self.driver.file.write_text('{}', encoding='utf-8')
        self.driver.data = {
            'config': {'adapter': 'command', 'host_command': ['host.exe'],
                       'codex_bin': 'codex.exe', 'timeout': 30},
            'deadline': time.time() + 30,
            'pending': {},
        }
        self.driver.load = lambda: self.driver.data
        self.driver.save = Mock()
        self.driver.submit = Mock()
        self.attempt = self.driver.folder / 'attempts/abc'
        self.attempt.mkdir(parents=True)
        self.job = {
            'id': 'abc', 'phase': 'execute', 'task': None,
            'project': str(self.root),
            'job_file': str(self.attempt / 'job.json'),
            'schema': str(self.attempt / 'schema.json'),
            'result': str(self.attempt / 'response.json'),
            'native_resume_id': '01a21b31-2815-72f2-9964-88ee78efc61b',
            'model_alias': 'group/deepseek-2', 'effort': 'max',
        }
        self.driver.data['pending'] = self.job

    @staticmethod
    def evidence(folder):
        stdout = folder / 'stdout.txt'
        stderr = folder / 'stderr.txt'
        stdout.write_text('', encoding='utf-8')
        stderr.write_text('', encoding='utf-8')
        return {
            'status': 'completed', 'returncode': 0,
            'stdout_path': str(stdout), 'stderr_path': str(stderr),
            'log_integrity': {
                'stdout': {'truncated': False},
                'stderr': {'truncated': False},
            },
            'process_alive': False,
        }

    def test_command_launch_reuses_shared_executor_and_prompt(self):
        def run(argv, cwd, env, timeout, folder, metadata, **kwargs):
            kwargs['on_started'](SimpleNamespace(pid=os.getpid()))
            self.assertEqual(argv, ['host.exe'])
            self.assertEqual(cwd, self.root)
            self.assertEqual(env['GSP_DRIVER_JOB'], self.job['job_file'])
            self.assertEqual(kwargs['input_data'], b'full prompt')
            return self.evidence(Path(folder))

        with patch('gsp.driver.prompt', return_value='full prompt'), \
             patch('gsp.driver.execute_command', side_effect=run) as execute:
            self.driver.launch(self.job)

        execute.assert_called_once()
        self.assertEqual(
            self.driver.submit.call_args.args, ('abc', self.job['result'], 0))

    def prepare_native_capacity(self):
        self.driver.data['config']['adapter'] = 'codex'
        from gsp.identity_store import IdentityStore
        (self.project.meta/'project.json').write_text(json.dumps({'id':'capacity-fixture'}),encoding='utf-8')
        IdentityStore(self.root).initialize()
        self.project.ledger_state=lambda task:{'work_uuid':'fixture-work'}

    def test_native_launch_reuses_worker_adapter_with_exact_resume(self):
        self.prepare_native_capacity()
        from gsp.resource_registry import ResourceRegistry

        def run(job, cwd, env, timeout, folder, contract_text, **kwargs):
            self.assertEqual(1,ResourceRegistry(self.root).scheduler_snapshot()['occupied'])
            kwargs['on_started'](SimpleNamespace(pid=os.getpid()))
            self.assertEqual(job, self.job)
            self.assertEqual(cwd, self.root)
            self.assertEqual(contract_text, 'full prompt')
            self.assertEqual(kwargs['executable'], 'codex.exe')
            self.assertEqual(kwargs['thread_id'], self.job['native_resume_id'])
            return {'status': 'completed', 'accepted': False,
                    'native_session_id': self.job['native_resume_id'],
                    'execution': self.evidence(Path(folder))}

        with patch('gsp.driver.prompt', return_value='full prompt'), \
             patch('gsp.driver.run_worker', side_effect=run) as worker:
            self.driver.launch(self.job)

        worker.assert_called_once()
        self.assertEqual(0,ResourceRegistry(self.root).scheduler_snapshot()['occupied'])
        self.assertEqual(
            self.driver.submit.call_args.args, ('abc', self.job['result'], 0))

    def test_native_timeout_keeps_project_capacity_until_verified_recovery(self):
        self.prepare_native_capacity()
        from gsp.resource_registry import ResourceRegistry
        execution=self.evidence(self.attempt)
        execution.update(status='timeout',returncode=1)
        with patch('gsp.driver.prompt',return_value='full prompt'), \
             patch('gsp.driver.run_worker',return_value={'status':'timeout','execution':execution}):
            with self.assertRaisesRegex(WorkflowError,'HOST_TIMEOUT'):
                self.driver.launch(self.job)
        self.assertEqual(1,ResourceRegistry(self.root).scheduler_snapshot()['occupied'])
        self.driver.submit.assert_not_called()

    def test_native_capacity_recovery_verifies_original_terminal_and_logs(self):
        self.prepare_native_capacity()
        from gsp.resource_registry import ResourceRegistry,ResourceOwner
        registry=ResourceRegistry(self.root)
        owner=ResourceOwner('fixture-work','native-worker:abc',1)
        granted=registry.reserve_execution_slot('native-worker:abc',owner)
        self.job['execution_slot']={'request_uid':'native-worker:abc','owner':vars(owner),
            'identity':granted['resources'][0]['identity']}
        terminal=command_execution.execute_command([sys.executable,'-c',"print('original terminal')"],
            self.root,dict(os.environ),10,self.attempt,{})
        self.job['worker']=terminal['process_identity']
        path=Path(terminal['stdout_path'])
        original=path.read_bytes()
        path.write_bytes(b'tampered')
        with self.assertRaisesRegex(WorkflowError,'NATIVE_CAPACITY_RECOVERY_UNKNOWN'):
            self.driver._capacity_recovery(self.job,release=True)
        self.assertEqual(1,registry.scheduler_snapshot()['occupied'])
        path.write_bytes(original)
        self.driver._capacity_recovery(self.job,release=True)
        self.assertEqual(0,registry.scheduler_snapshot()['occupied'])
        self.driver._capacity_recovery(self.job,release=True)
        self.assertEqual(0,registry.scheduler_snapshot()['occupied'])

    def test_unknown_terminal_state_is_not_submitted(self):
        def run(argv, cwd, env, timeout, folder, metadata, **kwargs):
            kwargs['on_started'](SimpleNamespace(pid=os.getpid()))
            value = self.evidence(Path(folder))
            value.update(status='timeout', returncode=1, launch_error='TimeoutExpired',
                         interruption='timeout')
            return value

        with patch('gsp.driver.prompt', return_value='full prompt'), \
             patch('gsp.driver.execute_command', side_effect=run):
            with self.assertRaisesRegex(WorkflowError, 'HOST_TIMEOUT'):
                self.driver.launch(self.job)
        self.driver.submit.assert_not_called()

    def test_command_launch_runs_real_fixture_through_shared_executor(self):
        self.driver.data['config']['host_command'] = [
            sys.executable, str(ROOT / 'tests/fixtures/driver_worker.py')]
        self.job['phase'] = 'review'
        Path(self.job['job_file']).write_text(
            json.dumps(self.job), encoding='utf-8')

        with patch('gsp.driver.prompt', return_value='fixture prompt'):
            self.driver.launch(self.job)

        response = Path(self.job['result'])
        self.assertTrue(response.is_file())
        self.assertEqual(json.loads(response.read_text(encoding='utf-8'))['job_id'], 'abc')
        self.assertTrue(Path(self.attempt, 'stdout.txt').is_file())
        self.assertTrue(Path(self.attempt, 'stderr.txt').is_file())
        self.assertEqual(
            self.driver.submit.call_args.args, ('abc', self.job['result'], 0))

    def test_check_reuses_shared_executor_with_actual_source_and_environment(self):
        source = self.root / 'bound-source'
        source.mkdir()
        environment = {'BUILD_ROOT': str(self.root / 'isolated-build'),
                       'MARKER': 'actual-build-environment'}

        def run(argv, cwd, env, timeout, folder, metadata, **kwargs):
            kwargs['on_started'](SimpleNamespace(pid=os.getpid()))
            self.assertEqual(cwd, source)
            self.assertEqual(env, environment)
            self.assertEqual(metadata['build_context']['mode'], 'isolated')
            Path(folder).mkdir(parents=True, exist_ok=True)
            Path(folder, 'stdout.txt').write_text('check stdout\n', encoding='utf-8')
            Path(folder, 'stderr.txt').write_text('check stderr\n', encoding='utf-8')
            return {
                'status': 'completed', 'returncode': 0,
                'stdout_path': str(Path(folder, 'stdout.txt')),
                'stderr_path': str(Path(folder, 'stderr.txt')),
                'log_integrity': {'stdout': {'truncated': False},
                                  'stderr': {'truncated': False}},
                'process_alive': False,
            }

        with patch('gsp.driver.execute_command', side_effect=run):
            completed = self.driver.run_owned_check(
                ['check.exe'], 5, source_root=source, env=environment,
                build_context={'mode': 'isolated', 'root': environment['BUILD_ROOT']},
            )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, 'check stdout\n')
        self.assertEqual(completed.stderr, 'check stderr\n')
        self.assertIsNone(self.driver.data['pending_check'])
        self.assertEqual(
            [call.args[0] for call in self.driver.save.call_args_list],
            ['check-started', 'check-exited'],
        )

    def test_check_timeout_keeps_process_identity_unknown(self):
        identity = {'pid': 424242, 'token': 'fixture-token'}

        def run(argv, cwd, env, timeout, folder, metadata, **kwargs):
            kwargs['on_started'](SimpleNamespace(pid=identity['pid']))
            Path(folder).mkdir(parents=True, exist_ok=True)
            Path(folder, 'stdout.txt').write_text('', encoding='utf-8')
            Path(folder, 'stderr.txt').write_text('timeout\n', encoding='utf-8')
            return {
                'status': 'timeout', 'returncode': 1,
                'launch_error': 'TimeoutExpired', 'interruption': 'timeout',
                'process_identity': identity, 'process_alive': False,
                'stdout_path': str(Path(folder, 'stdout.txt')),
                'stderr_path': str(Path(folder, 'stderr.txt')),
                'log_integrity': {'stdout': {'truncated': False},
                                  'stderr': {'truncated': False}},
            }

        with patch('gsp.driver.execute_command', side_effect=run), \
             patch('gsp.driver.identity', return_value=identity):
            with self.assertRaisesRegex(WorkflowError, 'CHECK_TIMEOUT'):
                self.driver.run_owned_check(['slow.exe'], 5)

        self.assertEqual(self.driver.data['pending_check'], identity)
        self.assertEqual(
            [call.args[0] for call in self.driver.save.call_args_list],
            ['check-started'],
        )

    def test_driver_check_passes_resolved_source_root_and_environment(self):
        source = self.root / 'resolved-source'
        source.mkdir()
        context = {
            'mode': 'isolated', 'root': str(self.root / 'build'),
            'provenance': 'dispatch', 'compatibility_key': 'fixture',
        }
        environment = {
            'BUILD_ROOT': context['root'], 'CARGO_TARGET_DIR': context['root'],
        }
        self.driver.data['config'].update(
            checks=['check.exe'], check_kind='tests')
        self.project.workspace_root = lambda state, task_id=None: source
        lease_attached = {'value': False}

        def attach(process):
            self.assertEqual(process.pid, os.getpid())
            lease_attached['value'] = True

        def run_check(project, state, run, args):
            return args.runner(['check.exe'], 5, on_started=attach)

        def execute(argv, cwd, env, timeout, folder, metadata, **kwargs):
            kwargs['on_started'](SimpleNamespace(pid=os.getpid()))
            self.assertEqual(cwd, source)
            self.assertEqual(env, environment)
            Path(folder).mkdir(parents=True, exist_ok=True)
            Path(folder, 'stdout.txt').write_text('ok\n', encoding='utf-8')
            Path(folder, 'stderr.txt').write_text('', encoding='utf-8')
            return {
                'status': 'completed', 'returncode': 0,
                'stdout_path': str(Path(folder, 'stdout.txt')),
                'stderr_path': str(Path(folder, 'stderr.txt')),
                'log_integrity': {'stdout': {'truncated': False},
                                  'stderr': {'truncated': False}},
                'process_alive': False,
            }

        with patch.object(self.driver, 'check_topology_supported'), \
             patch('gsp.build_context.resolve_context', return_value=context), \
             patch('gsp.build_context.command_environment', return_value=environment), \
             patch('gsp.driver.run_check', side_effect=run_check), \
             patch('gsp.driver.execute_command', side_effect=execute):
            completed = self.driver.check({'id': 'work'}, 'T01')

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, 'ok\n')
        self.assertTrue(lease_attached['value'])

    def test_runtime_runner_context_is_explicit_and_typeerror_safe(self):
        calls = []
        context = {
            'source_root': self.root / 'source',
            'env': {'BUILD_ROOT': str(self.root / 'build')},
            'build_context': {'mode': 'isolated'},
            'on_started': lambda process: None,
        }

        def legacy(argv, timeout):
            calls.append(('legacy', argv, timeout))

        def contextual(argv, timeout, **kwargs):
            calls.append(('contextual', argv, timeout, kwargs))

        def broken(argv, timeout, source_root=None, env=None,
                   build_context=None, on_started=None):
            raise TypeError('inside runner')

        _invoke_check_runner(legacy, ['check.exe'], 3, **context)
        _invoke_check_runner(contextual, ['check.exe'], 4, **context)
        self.assertEqual(
            calls[0], ('legacy', ['check.exe'], 3))
        self.assertEqual(
            calls[1], ('contextual', ['check.exe'], 4, context))
        with self.assertRaisesRegex(TypeError, 'inside runner'):
            _invoke_check_runner(broken, ['check.exe'], 5, **context)

    def test_core_error_is_one_process_wide_type(self):
        self.assertIs(WorkflowError, CoreWorkflowError)
        self.assertIs(command_execution.WorkflowError, CoreWorkflowError)

    def test_long_execution_suspends_and_reacquires_one_stable_writer(self):
        with locked(self.root):
            self.assertEqual(inspect_lock(self.root)['status'], 'live')
            with suspend_writer_lock(self.root):
                self.assertEqual(inspect_lock(self.root)['status'], 'suspended')
                probe = __import__('subprocess').run([sys.executable, '-B', '-c',
                    'from gsp.runtime import locked; from pathlib import Path; import sys\n'
                    'with locked(Path(sys.argv[1])): print("admitted")', str(self.root)],
                    capture_output=True, text=True, timeout=15)
                self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)
                self.assertIn('admitted', probe.stdout)
            self.assertEqual(inspect_lock(self.root)['status'], 'live')
        self.assertEqual(inspect_lock(self.root)['status'], 'absent')

    def test_execution_control_guard_rejects_ledger_change(self):
        state_file = self.root / '.gsp/runs/work/state.json'
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state = {'id': 'work', 'work_uuid': 'work-a',
                 'controller_generation': 1, 'cancel_generation': 1,
                 'inflight': {'id': 'evidence-a', 'phase': 'command'}}
        state_file.write_text(json.dumps(state), encoding='utf-8')
        guard = _execution_control_guard(self.project, state, 'evidence-a')
        state['controller_generation'] = 2
        state_file.write_text(json.dumps(state), encoding='utf-8')
        with self.assertRaisesRegex(WorkflowError, 'CONTROL_CHANGED'):
            _assert_execution_control(guard)


if __name__ == '__main__':
    unittest.main()
