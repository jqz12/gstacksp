"""Durable local headless controller. Host prose never directly completes a task."""
from argparse import Namespace
import copy
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from .core import (WorkflowError, atomic, digest, inside, now, read, redact, sha)
from .project import (Project)
from .workflow_locks import (locked)
from .workflow_operations import (dispatch)
from .workflow_support import (fingerprint, run_check, slug, stable_action_uid)
from .hosts import child_options, codex_argv, identity, is_alive, prompt, terminate_owned
from .codex_worker_adapter import run_worker
from .command_execution import execute_command


def _object(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


RESULT_SCHEMA = _object({
    'job_id': {'type': 'string'}, 'outcome': {'type': 'string', 'enum': ['completed','failed','needs_input']},
    'summary': {'type': 'string'}, 'question_kind': {'type': 'string', 'enum': ['technical','design','authorization','dependency']},
    'question': {'type': 'string'}, 'recommendation': {'type': 'string'},
    'plan': {'anyOf': [{'type': 'null'}, _object({'text': {'type': 'string'}, 'tasks': {'type': 'array', 'items': _object({
        'id': {'type': 'string'}, 'title': {'type': 'string'}, 'acceptance': {'type': 'string'},
        'depends': {'type': 'array', 'items': {'type': 'string'}}, 'no_tests_reason': {'type': 'string'}})}})]},
    'findings': {'type': 'array', 'items': _object({'blocking': {'type': 'boolean'}, 'text': {'type': 'string'}})},
})


class Driver:
    def __init__(self, project, task):
        self.project, self.task = project, slug(task)
        self.run, self.doc = project.paths(task)
        self.folder = self.run / 'driver'
        if not inside(project.root, self.folder):
            raise WorkflowError('DRIVER_ESCAPE')
        self.file = self.folder / 'state.json'
        self.lease = project.meta / 'driver.lock'

    def load(self):
        self.data = read(self.file)
        if self.data['project_id'] != self.project.identity()['id'] or self.data['root'] != str(self.project.root):
            raise WorkflowError('DRIVER_PROJECT_MISMATCH')
        return self.data

    def _append_event(self, record):
        path = self.folder / 'events.jsonl'
        event_uid = record.get('event_uid')
        if event_uid:
            if path.exists():
                for raw in path.read_text(encoding='utf-8').splitlines():
                    try:
                        existing = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(existing, dict) or existing.get('event_uid') != event_uid:
                        continue
                    keys = ('at', 'event', 'revision', 'dispatches', 'reason')
                    if any(existing.get(key) != record.get(key) for key in keys):
                        raise WorkflowError('DRIVER_EVENT_CONFLICT ' + event_uid)
                    return False
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        return True

    def save(self, event, event_uid=None, event_at=None):
        self.data['updated'] = now()
        self.data['revision'] = self.data.get('revision', 0) + 1
        record = {
            'at': event_at or now(),
            'event': event,
            'revision': self.data['revision'],
            'dispatches': self.data['dispatches'],
            'reason': self.data.get('reason'),
        }
        if event_uid:
            record['event_uid'] = event_uid
        atomic(self.file, self.data)
        self._append_event(record)
        self.report()

    def report(self):
        d = self.data
        text = (f'# {self.task} — 无人值守驱动报告\n\n状态：**{d["status"]}**\n\n'
                f'- 阶段：{d["phase"]}\n- 原因：{d.get("reason") or "none"}\n'
                f'- 适配器：{d["config"]["adapter"]}（command 为显式命令集成/fixture，不代表真实 Codex）\n'
                f'- 派发：{d["dispatches"]}/{d["config"]["max_dispatches"]}\n'
                f'- 修复轮次：{json.dumps(d["fix_rounds"], ensure_ascii=False)}\n'
                '- 实际费用：unknown；派发/时间上限不是金额计费上限。\n'
                f'- gate：{d.get("gate") or "none"}\n'
                f"- 任务与证据：[交付](delivery.md)、[审查](review.md)、[计划]({self.project.document_link(read(self.run / 'state.json'), 'plan', self.doc)})、[规格]({self.project.document_link(read(self.run / 'state.json'), 'design', self.doc)})。\n\n"
                '恢复入口：先 drive-status 查看当前 gate/attempt；等待普通答复用 drive-answer，'
                '执行未知先检查进程/现场并写报告再 drive-recover，之后 drive-start。不要直接重置状态或消耗计数。\n')
        path = self.doc / 'driver-report.md'
        if path.exists() and path.read_text(encoding='utf-8') == text:
            return False
        atomic(path, text)
        return True

    def _recovery_request(self, report, reason):
        binding = json.dumps(
            self.data.get('binding'), ensure_ascii=False, sort_keys=True, separators=(',', ':')
        ).encode('utf-8')
        return {
            'evidence': str(report),
            'evidence_sha256': sha(report.read_bytes()),
            'reason': redact(reason),
            'reason_sha256': sha(reason.encode('utf-8')),
            'binding_sha256': sha(binding),
        }

    @staticmethod
    def _recovery_event(event, revision, dispatches, reason, event_at, event_uid):
        return {
            'at': event_at,
            'event': event,
            'revision': revision,
            'dispatches': dispatches,
            'reason': reason,
            'event_uid': event_uid,
        }

    def _pending_recovery_request_matches(self, pending, request):
        if pending.get('request') != request:
            raise WorkflowError('RECOVERY_INPUT_CHANGED pending recovery request differs')

    def _recovery_plan_proof(self, state):
        plan = self.project.document(state, 'plan').resolve()
        try:
            relative = plan.relative_to(self.project.root.resolve()).as_posix()
        except ValueError as exc:
            raise WorkflowError('RECOVERY_PLAN_CHANGED plan path escapes the project') from exc
        sealed = bool(state.get('plan_seal'))
        if sealed:
            try:
                self.project.valid_plan(state, self.doc)
            except (WorkflowError, OSError, TypeError, KeyError) as exc:
                raise WorkflowError('RECOVERY_PLAN_CHANGED sealed plan is no longer valid') from exc
        exists = plan.is_file()
        if plan.exists() and not exists:
            raise WorkflowError('RECOVERY_PLAN_CHANGED plan path is not a file')
        try:
            content_sha256 = sha(plan.read_bytes()) if exists else None
        except OSError as exc:
            raise WorkflowError('RECOVERY_PLAN_CHANGED plan bytes cannot be read') from exc
        proof = {
            'schema': 1,
            'path': relative,
            'sealed': sealed,
            'exists': exists,
            'sha256': content_sha256,
        }
        if sealed:
            proof['seal_sha256'] = state['plan_seal'].get('sha256')
            proof['tasks_sha256'] = state['plan_seal'].get('tasks_sha256')
        return proof

    def _pending_recovery_plan_matches(self, pending, state):
        frozen = pending.get('plan_proof')
        if not isinstance(frozen, dict) or frozen.get('schema') != 1:
            raise WorkflowError('RECOVERY_PLAN_CHANGED pending recovery has no frozen plan proof')
        if frozen != self._recovery_plan_proof(state):
            raise WorkflowError('RECOVERY_PLAN_CHANGED physical plan differs from recovery intent')

    def _recovery_operation(self, action_uid):
        from .identity_store import IdentityStore
        from .operation_journal import project_write_logical_key
        key = project_write_logical_key(self.task, action_uid)
        return IdentityStore(self.project.root).operation_for_logical_key(key)

    def _repair_pending_recovery_projection(self, pending):
        self._append_event(pending['prepared_event'])
        self.report()

    def _repair_completed_recovery_projection(self, record):
        self._append_event(record['prepared_event'])
        self._append_event(record['completed_event'])
        self.report()

    def _accepted_recovery_workflow(self, pending):
        from .identity_store import IdentityStore
        from .operation_journal import project_write_logical_key

        state_file = self.run / 'state.json'
        current = sha(state_file.read_bytes())
        if current == pending['expected_state_digest']:
            return current
        store = IdentityStore(self.project.root)
        key = project_write_logical_key(self.task, pending['action_uid'])
        operation = store.operation_for_logical_key(key)
        if operation is None or operation.get('expected_state_digest') != pending['expected_state_digest']:
            raise WorkflowError('RECOVERY_STATE_CHANGED workflow advanced outside the frozen operation')
        accepted = {
            value for value in (
                operation.get('new_state_digest'),
                operation.get('result_ref'),
            ) if value
        }
        candidate = store.get_operation_candidate(operation['operation_uid'])
        if candidate is not None:
            accepted.add(candidate.candidate_digest)
        if current not in accepted:
            raise WorkflowError('RECOVERY_STATE_CHANGED workflow differs from the frozen operation')
        return current

    def _completed_recovery_noop(self, request):
        if self.data.get('pending_recovery'):
            return None
        recoveries = self.data.get('recoveries') or []
        if not recoveries:
            return None
        record = recoveries[-1]
        if (record.get('request') != request
                or record.get('completed_driver_revision') != self.data.get('revision')
                or record.get('completed_status') != self.data.get('status')
                or self.data.get('pending')
                or self.data.get('pending_check')
                or self.stop_requested()):
            return None
        if sha((self.run / 'state.json').read_bytes()) != record.get('workflow_result_digest'):
            return None
        operation = self._recovery_operation(record.get('action_uid'))
        if (operation is None or operation.get('operation_uid') != record.get('operation_uid')
                or operation.get('phase') != 'committed'):
            return None
        return record

    def _recovery_is_required(self):
        return (self.data.get('status') in ('waiting', 'blocked', 'stopped')
                or bool(self.data.get('pending'))
                or bool(self.data.get('pending_check'))
                or bool(self.data.get('pending_fix')))

    def initialize(self, args):
        with locked(self.project.root):
            if self.file.exists():
                raise WorkflowError('DRIVER_EXISTS limits and consumed attempts cannot be reset by reinitializing')
            state, _, doc = self.project.state(self.task)
            self.project.valid_design(state, doc)
            if state['mode'] != 'unattended' or not state.get('mode_quote'):
                raise WorkflowError('UNATTENDED_AUTHORIZATION_REQUIRED create the task with actual unattended authorization')
            if state.get('plan_seal'):
                self.project.valid_plan(state, doc)
            checks = read(args.checks)
            if not isinstance(checks, list) or not checks or any(not isinstance(v, str) or not v for v in checks):
                raise WorkflowError('CHECK_ARGV_REQUIRED JSON array of actual executable and arguments')
            command = read(args.host_command) if args.host_command else None
            if command is not None and (not isinstance(command, list) or not command or any(not isinstance(v, str) or not v for v in command)):
                raise WorkflowError('HOST_ARGV_REQUIRED')
            config = read(self.project.meta / 'config.json')
            rounds = config.get('max_fix_rounds', 3)
            if (type(rounds) is not int or rounds < 0 or not math.isfinite(args.timeout) or not math.isfinite(args.max_seconds)
                    or args.timeout <= 0 or args.max_seconds <= 0 or args.max_dispatches < 1):
                raise WorkflowError('INVALID_DRIVER_LIMIT')
            self.folder.mkdir(parents=True, exist_ok=False)
            self.data = {'schema': 1, 'task': self.task, 'root': str(self.project.root), 'project_id': state['project_id'],
                         'status': 'runnable', 'phase': 'execute' if state.get('plan_seal') else 'plan',
                         'reason': None, 'gate': None, 'pending': None, 'dispatches': 0, 'fix_rounds': {},
                         'pending_fix': None, 'review_findings': [], 'plan_findings': [], 'answers': [], 'deadline': None,
                         'binding': {'design': state['design_approval']['sha256'], 'scope': state['scope'], 'authorizations': state['authorizations'], 'mode_quote': state['mode_quote']},
                         'config': {'checks': checks, 'check_kind': args.check_kind, 'adapter': 'command' if command else 'codex',
                                    'host_command': command, 'codex_bin': args.codex_bin, 'max_fix_rounds': rounds,
                                    'timeout': args.timeout, 'max_seconds': args.max_seconds, 'max_dispatches': args.max_dispatches,
                                    'quote': args.quote}, 'created': now()}
            self.save('initialized')
            return self.data

    def validate_workflow(self):
        state, _, doc = self.project.state(self.task)
        self.check_topology_supported(state)
        self.project.valid_design(state, doc)
        b = self.data['binding']
        if (state['design_approval']['sha256'] != b['design'] or state['scope'] != b['scope'] or state['authorizations'] != b['authorizations']
                or state['mode'] != 'unattended' or state['mode_quote'] != b['mode_quote']):
            raise WorkflowError('CONTROL_CHANGED design/scope/authorization differs from initialized run')
        if state.get('inflight'):
            raise WorkflowError('EXECUTION_UNKNOWN project check still has inflight state')
        if state.get('plan_seal'):
            self.project.valid_plan(state, doc)
        return state

    def check_topology_supported(self, state):
        if (self.project.workspace_root(state) != self.project.root or
                any(t.get('topology') for t in state.get('tasks', []))):
            raise WorkflowError('DRIVER_TOPOLOGY_UNSUPPORTED use Native execution for bound worktrees; headless workers still target the control root')

    def advance_stage(self, state, target):
        if state.get('controller_required') and state['status'] != target:
            result, ok = dispatch(self.project, Namespace(command='advance', id=self.task, to=target))
            if not ok:
                raise WorkflowError('CONTROLLER_BLOCKED ' + json.dumps(result['blockers'], ensure_ascii=False))
            state, _, _ = self.project.state(self.task)
        return state

    def gate(self, reason, question, kind='dependency'):
        ident = uuid.uuid4().hex
        atomic(self.folder / 'gates' / (ident + '.json'), {'id': ident, 'status': 'pending', 'reason': reason,
               'kind': kind, 'question': redact(question), 'at': now(), 'attempt': (self.data.get('pending') or {}).get('id')})
        self.data.update(status='waiting', reason=reason, gate=ident)
        self.save('waiting')
        return {'action': 'waiting', 'gate': ident, 'reason': reason, 'question': question}

    def block(self, reason):
        self.data.update(status='blocked', reason=reason)
        self.save('blocked')
        return {'action': 'blocked', 'reason': reason}

    def check(self, state, task):
        self.check_topology_supported(state)
        remaining = self.data['deadline'] - time.time()
        if remaining <= 0:
            raise WorkflowError('DEADLINE_EXCEEDED')
        argv = list(self.data['config']['checks'])
        source_root = (
            self.project.verification_root(state)
            if task == 'all'
            else self.project.workspace_root(state, task_id=task)
        )
        from .build_context import command_environment, resolve_context
        build_context = resolve_context(
            self.project, state, task, argv, os.environ, source_root=source_root,
        )
        command_env = command_environment(os.environ, build_context)

        def runner(command, timeout, **context):
            return self.run_owned_check(
                command, timeout,
                source_root=context.get('source_root', source_root),
                env=context.get('env', command_env),
                build_context=context.get('build_context', build_context),
                on_started=context.get('on_started'),
            )

        args = Namespace(
            task=task, kind=self.data['config']['check_kind'], argv=argv,
            timeout=min(self.data['config']['timeout'], remaining),
            runner=runner,
        )
        return run_check(self.project, state, self.run, args)

    def stop_requested(self):
        return (self.folder/'stop.json').exists() or read(self.file).get('stop_requested', False)

    def run_owned_check(self, argv, timeout, *, source_root=None, env=None,
                        build_context=None, on_started=None):
        remaining = min(timeout, self.data['deadline'] - time.time())
        if remaining <= 0:
            raise WorkflowError('DEADLINE_EXCEEDED')
        source_root = Path(source_root or self.project.root).resolve()
        command_env = dict(os.environ if env is None else env)
        folder = self.folder / 'checks' / uuid.uuid4().hex
        started = {}

        def attach_started(process):
            with locked(self.project.root):
                self.load()
                self.data['pending_check'] = identity(process.pid)
                self.save('check-started')
                started['driver_digest'] = digest(self.file)
            if on_started is not None:
                on_started(process)

        def cancel_check():
            if self.stop_requested():
                return ('STOP_REQUESTED check terminated; inspect partial changes')
            return None

        execution = execute_command(
            argv, source_root, command_env, remaining, folder,
            {
                'task': self.task,
                'argv': [redact(value) for value in argv],
                'build_context': {
                    key: (build_context or {}).get(key)
                    for key in ('mode', 'root', 'provenance', 'compatibility_key')
                },
            },
            on_started=attach_started,
            cancel_check=cancel_check,
        )
        if ('driver_digest' in started
                and digest(self.file) != started['driver_digest']):
            raise WorkflowError(
                'CONTROL_CHANGED driver bookkeeping changed during check')
        status = execution.get('status')
        if status == 'cancelled':
            raise WorkflowError(
                execution.get('cancel_reason')
                or 'STOP_REQUESTED check terminated; inspect partial changes')
        if status == 'timeout':
            raise WorkflowError(
                'CHECK_TIMEOUT outcome unknown; inspect partial changes')
        if execution.get('process_alive'):
            raise WorkflowError(
                'CHECK_EXECUTION_UNKNOWN process identity is still alive')
        integrity = execution.get('log_integrity') or {}
        if any((value or {}).get('truncated') for value in integrity.values()):
            raise WorkflowError(
                'CHECK_LOG_TRUNCATED outcome unknown; inspect partial evidence')
        code = execution.get('returncode')
        if status not in {'completed', 'failed'} or code is None:
            raise WorkflowError(
                'CHECK_EXECUTION_UNKNOWN missing terminal outcome')
        stdout = Path(execution['stdout_path']).read_text(
            encoding='utf-8', errors='replace')
        stderr = Path(execution['stderr_path']).read_text(
            encoding='utf-8', errors='replace')
        with locked(self.project.root):
            self.load()
            self.data['pending_check'] = None
            self.save('check-exited')
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    def next(self):
        with locked(self.project.root):
            self.load()
            pending_recovery = self.data.get('pending_recovery')
            if pending_recovery:
                state, _, _ = self.project.state(self.task, recovery=True)
                self._pending_recovery_plan_matches(pending_recovery, state)
            if self.stop_requested():
                self.data.update(status='stopped', reason='STOP_REQUESTED')
                self.save('stopped')
                return {'action': 'stopped'}
            if pending_recovery:
                self._repair_pending_recovery_projection(pending_recovery)
                return {'action': 'recovery-pending', 'reason': 'RECOVERY_PENDING'}
            if self.data['status'] == 'completed':
                state, _, _ = self.project.state(self.task)
                if state['status'] != 'ready':
                    return self.gate('STATE_CHANGED', 'Completed run inputs changed; inspect and reverify.')
            if self.data['status'] in ('waiting', 'blocked', 'completed', 'stopped'):
                return {'action': self.data['status'], 'reason': self.data.get('reason')}
            if self.data['pending']:
                return {'action': 'awaiting_result', 'job': self.data['pending']}
            try:
                state = self.validate_workflow()
            except (WorkflowError, OSError) as exc:
                return self.gate('STATE_CHANGED', str(exc), 'design')
            if self.data['deadline'] is None:
                self.data['deadline'] = time.time() + self.data['config']['max_seconds']
            if time.time() >= self.data['deadline']:
                return self.block('DEADLINE_EXCEEDED')
            phase = self.data['phase']
            task = None
            if phase == 'execute' or (phase == 'verify' and state['status'] == 'needs-verification'):
                try:
                    state = self.advance_stage(state, 'execute')
                except WorkflowError as exc:
                    return self.gate('CONTROLLER_BLOCKED', str(exc))
            if phase == 'execute':
                open_tasks = [t for t in state['tasks'] if t['status'] != 'done']
                if open_tasks:
                    task = next((t for t in open_tasks if all(self.project.item(state, d)['status'] == 'done' for d in t['depends'])), None)
                    if task is None:
                        return self.gate('DEPENDENCIES_PENDING', 'No eligible task; reconcile the plan and dependencies.')
                    if task['status'] != 'running':
                        dispatch(self.project, Namespace(command='task-start', id=self.task, task=task['id']))
                        state, _, _ = self.project.state(self.task)
                else:
                    phase = self.data['phase'] = 'verify'
            if phase == 'verify':
                try:
                    receipt, passed = self.check(state, 'all')
                except WorkflowError as exc:
                    reason = ('STOP_REQUESTED' if str(exc).startswith('STOP_REQUESTED') else
                              'EXECUTION_UNKNOWN' if str(exc).startswith('CHECK_TIMEOUT') else 'CHECK_BLOCKED')
                    return self.gate(reason, str(exc))
                if not passed:
                    self.data.update(phase='fix-review', pending_fix='review', review_findings=[{'blocking': True, 'text': 'Final regression failed: ' + receipt['status']}])
                    phase = 'fix-review'
                else:
                    self.data['phase'] = phase = 'review'
                state, _, _ = self.project.state(self.task)
                if passed:
                    try:
                        state = self.advance_stage(state, 'review')
                    except WorkflowError as exc:
                        return self.gate('CONTROLLER_BLOCKED', str(exc))
            if phase == 'deliver':
                try:
                    state = self.advance_stage(state, 'checking')
                    state = self.advance_stage(state, 'ready')
                    dispatch(self.project, Namespace(command='ready', id=self.task))
                    dispatch(self.project, Namespace(command='save', id=self.task))
                except WorkflowError as exc:
                    return self.gate('DELIVERY_BLOCKED', str(exc))
                self.data.update(status='completed', reason=None)
                self.save('completed')
                return {'action': 'completed'}
            key = self.data.get('pending_fix')
            if key:
                count = self.data['fix_rounds'].get(key, 0)
                if count >= self.data['config']['max_fix_rounds']:
                    return self.block('FIX_LIMIT')
            if self.data['dispatches'] >= self.data['config']['max_dispatches']:
                return self.block('DISPATCH_LIMIT')
            if task is not None:
                from .gate_rules import guard_inputs
                guard_inputs(self.project,state,task['id'],phase='dispatch')
            if key:
                self.data['fix_rounds'][key] = self.data['fix_rounds'].get(key, 0) + 1
            ident = uuid.uuid4().hex
            folder = self.folder / 'attempts' / ident
            folder.mkdir(parents=True, exist_ok=False)
            actual_phase = 'fix-task' if phase == 'execute' and key else phase
            job = {'id': ident, 'project': str(self.project.root), 'phase': actual_phase, 'task': task,
                   'design': str(self.project.document(state, 'design')), 'plan_file': str(self.project.document(state, 'plan')),
                   'workflow_state': str(self.run / 'state.json'), 'driver_state': str(self.file),
                   'workflow_guard': digest(self.run / 'state.json'), 'input_fingerprint': fingerprint(self.project.root),
                   'review_findings': self.data['review_findings'], 'plan_findings': self.data.get('plan_findings', []),
                   'answers': self.data['answers'][-5:],
                   'checks': self.data['config']['checks'], 'check_kind': self.data['config']['check_kind'],
                   'schema': str(folder / 'schema.json'), 'result': str(folder / 'response.json'),
                   'job_file': str(folder / 'job.json'), 'at': now(), 'worker': None}
            if task is not None:
                from .dispatch import _task_contract, _contract_inputs
                texts,paths=_contract_inputs(self.project,state)
                job['task_contract'] = _task_contract(self.project,state,task['id'],texts,paths)
                job['model_alias'] = 'group/deepseek-2'
                job['effort'] = 'max'
                if self.data['config']['adapter'] == 'codex':
                    from .codex_worker_adapter import correction_context
                    try:
                        job['native_resume_id'] = correction_context(
                            job, self.data.get('native_contexts') or {}, correcting=bool(key))
                    except ValueError as exc:
                        return self.gate('NATIVE_CONTEXT_REQUIRED', str(exc))
            atomic(folder / 'schema.json', RESULT_SCHEMA)
            atomic(folder / 'job.json', job)
            self.data.update(pending=job, status='running', reason=None)
            self.data['dispatches'] += 1
            self.save('dispatched')
            return {'action': 'dispatch', 'job': job}

    def submit(self, ident, result_file, exit_code=0):
        with locked(self.project.root):
            self.load()
            job = self.data['pending']
            if not job or job['id'] != ident:
                raise WorkflowError('JOB_MISMATCH no matching pending attempt')
            if self.data['status'] == 'waiting':
                raise WorkflowError('RECOVERY_REQUIRED pending execution already stopped')
            if job.get('worker') and is_alive(job['worker']):
                raise WorkflowError('WORKER_ACTIVE cannot submit before the owned host exits')
            if digest(self.run / 'state.json') != job['workflow_guard']:
                return self.gate('CONTROL_CHANGED', 'Worker or concurrent actor changed workflow bookkeeping.')
            try:
                state = self.validate_workflow()
                file = Path(result_file)
                if not inside(self.project.root, file) or file.resolve() != Path(job['result']).resolve() or exit_code != 0:
                    raise WorkflowError('HOST_RESULT_UNTRUSTED')
                if file.stat().st_size > 2_000_000:
                    raise WorkflowError('HOST_RESULT_TOO_LARGE')
                result = read(file)
                self.validate_result(result, job)
                if job['phase'] in ('plan', 'review') and fingerprint(self.project.root) != job['input_fingerprint']:
                    raise WorkflowError('READ_ONLY_JOB_CHANGED_INPUTS')
            except (WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
                return self.gate('EXECUTION_UNKNOWN', 'Host result is not acceptable: ' + str(exc))
            accepted = Path(job['result']).parent / 'accepted.json'
            if accepted.exists():
                raise WorkflowError('RESULT_ALREADY_ACCEPTED inspect partially applied attempt; do not replay')
            rejected = accepted.with_name('rejected.json')
            if rejected.exists():
                raise WorkflowError('RESULT_ALREADY_REJECTED inspect interrupted rejection; do not replay')
            if job['phase'] == 'plan' and result['outcome'] == 'completed':
                # Check before accepting, writing the bound plan or registering tasks.
                _, quality = self.plan_candidate(state, result['plan'])
                if not quality['structural_ok']:
                    atomic(rejected, {'job': ident, 'sha256': digest(file), 'at': now(), 'quality': quality})
                    self.data.update(pending=None, phase='plan', status='runnable', reason=None,
                                     pending_fix='plan', plan_findings=quality['findings'])
                    self.save('plan-rejected')
                    return {'action': 'continue', 'phase': 'plan', 'quality': quality}
            atomic(accepted, {'job': ident, 'sha256': digest(file), 'exit': exit_code, 'at': now()})
            self.data['pending'] = None
            outcome = result['outcome']
            if outcome == 'needs_input':
                if result['question_kind'] == 'technical' and result['recommendation'].strip():
                    answer = result['recommendation']
                    self.record_answer('agent-recommendation', answer)
                    self.data['status'] = 'runnable'
                    self.save('recommended')
                    return {'action': 'continue', 'recommendation': answer}
                self.save('result-needs-input')
                return self.gate('USER_INPUT', result['question'], result['question_kind'])
            if outcome == 'failed':
                self.data['pending_fix'] = 'task:' + job['task']['id'] if job['task'] else 'review'
                if job['phase'] == 'plan':
                    return self.gate('PLAN_FAILED', result['summary'])
                if job['phase'] == 'review':
                    self.data['phase'] = 'fix-review'
                self.data['status'] = 'runnable'
                self.save('worker-failed')
                return {'action': 'continue'}
            if job['phase'] == 'plan':
                self.apply_plan(state, result['plan'])
                self.data.update(phase='execute', pending_fix=None, plan_findings=[])
            elif job['phase'] in ('execute', 'fix-task'):
                receipt, passed = self.check(state, job['task']['id'])
                # The check persists short driver bookkeeping while the long
                # command lock is suspended. Reload that bounded update and
                # clear the accepted attempt on the current object.
                self.load()
                pending = self.data.get('pending') or {}
                if pending.get('id') != job['id'] or self.data.get('pending_check'):
                    return self.gate(
                        'CONTROL_CHANGED',
                        'Driver bookkeeping changed while the accepted check ran.',
                    )
                self.data['pending'] = None
                if passed:
                    dispatch(self.project, Namespace(command='task-done', id=self.task, task=job['task']['id']))
                    self.data['pending_fix'] = None
                else:
                    self.data['pending_fix'] = 'task:' + job['task']['id']
                    self.data['review_findings'] = [{'blocking': True, 'text': 'Actual check failed: ' + receipt['status']}]
            elif job['phase'] == 'fix-review':
                self.data.update(phase='verify', pending_fix=None)
            elif job['phase'] == 'review':
                findings = result['findings']
                blocking = sum(r['blocking'] for r in findings)
                text = '# Headless code review\n\n' + redact(result['summary']) + '\n\n'
                text += '\n'.join(('- BLOCKING: ' if r['blocking'] else '- Follow-up: ') + redact(r['text']) for r in findings)
                text += '\n\nReviewer: fresh host context; same provider is not an independent vendor. Coverage limited to actual code/test reading; browser and remote services not implied.\n'
                atomic(self.doc / 'review.md', text)
                # A fresh model context does not establish vendor independence.
                dispatch(self.project, Namespace(command='review', id=self.task, reviewer='self' if self.data['config']['adapter'] == 'command' else 'fresh-context', blocking=blocking))
                self.data['review_findings'] = findings
                self.data.update(phase='fix-review' if blocking else 'deliver', pending_fix='review' if blocking else None)
            self.data['status'] = 'runnable'
            self.save('submitted')
            return {'action': 'continue', 'phase': self.data['phase']}

    @staticmethod
    def validate_result(result, job):
        if not isinstance(result, dict) or set(result) != set(RESULT_SCHEMA['properties']):
            raise WorkflowError('RESULT_SCHEMA keys do not match')
        if result['job_id'] != job['id'] or result['outcome'] not in ('completed', 'failed', 'needs_input'):
            raise WorkflowError('RESULT_SCHEMA stale ID or outcome')
        if any(not isinstance(result[k], str) for k in ('summary','question_kind','question','recommendation')):
            raise WorkflowError('RESULT_SCHEMA text field invalid')
        if result['question_kind'] not in ('technical','design','authorization','dependency') or not isinstance(result['findings'], list):
            raise WorkflowError('RESULT_SCHEMA invalid question/findings')
        if any(not isinstance(r, dict) or set(r) != {'blocking','text'} or type(r['blocking']) is not bool or not isinstance(r['text'], str) for r in result['findings']):
            raise WorkflowError('RESULT_SCHEMA invalid finding')
        if result['outcome'] == 'needs_input' and not result['question'].strip():
            raise WorkflowError('RESULT_SCHEMA empty question')
        if job['phase'] == 'plan' and result['outcome'] == 'completed':
            plan = result['plan']
            if not isinstance(plan, dict) or set(plan) != {'text','tasks'} or not isinstance(plan['text'], str) or len(plan['text'].strip()) < 15 or not isinstance(plan['tasks'], list) or not plan['tasks']:
                raise WorkflowError('RESULT_SCHEMA complete plan required')
            seen = set()
            for task in plan['tasks']:
                if not isinstance(task, dict) or set(task) != {'id','title','acceptance','depends','no_tests_reason'}:
                    raise WorkflowError('RESULT_SCHEMA invalid task')
                slug(task['id'])
                if task['id'] in seen or task['id'] == 'all' or not isinstance(task['depends'], list) or any(d not in seen for d in task['depends']):
                    raise WorkflowError('RESULT_SCHEMA duplicate/reserved task or invalid dependencies')
                if any(not isinstance(task[k], str) or (k != 'no_tests_reason' and not task[k].strip()) for k in ('title','acceptance','no_tests_reason')):
                    raise WorkflowError('RESULT_SCHEMA missing task acceptance')
                seen.add(task['id'])

    def plan_candidate(self, state, plan):
        from .plan_quality import MAX_PLAN_BYTES, check_plan
        if state['tasks']:
            raise WorkflowError('PLAN_RECONCILIATION_REQUIRED preserve existing tasks; seal their plan explicitly')
        spec_link = self.project.document_link(state, 'design', self.project.document(state, 'plan').parent)
        text = f'Spec: [approved design]({spec_link})\n\n' + plan['text']
        if len(text.encode('utf-8')) > MAX_PLAN_BYTES:
            quality = {'schema': 1, 'structural_ok': False, 'semantic_review': 'NOT_CHECKED',
                       'findings': [{'code': 'PLAN_TOO_LARGE', 'task': None, 'detail': 'Plan limit is 1 MiB UTF-8'}]}
        else:
            quality = check_plan(text, [task['id'] for task in plan['tasks']])
        return text, quality

    def apply_plan(self, state, plan):
        text, quality = self.plan_candidate(state, plan)
        if not quality['structural_ok']:
            raise WorkflowError('PLAN_STRUCTURE_INVALID ' + json.dumps(quality['findings'], ensure_ascii=False))
        atomic(self.project.document(state, 'plan'), text)
        for task in plan['tasks']:
            dispatch(self.project, Namespace(command='task-add', id=self.task, task=task['id'], title=task['title'],
                acceptance=task['acceptance'], depends=','.join(task['depends']), no_tests_reason=task['no_tests_reason'] or None))
        dispatch(self.project, Namespace(command='seal-plan', id=self.task, check_structure=True))

    def record_answer(self, source, answer):
        self.data['answers'].append({'at': now(), 'source': source, 'answer': redact(answer)})
        with (self.doc / 'decisions.md').open('a', encoding='utf-8') as stream:
            stream.write(f'\n- {now()} [{source}] {redact(answer)}\n')

    def answer(self, args):
        with locked(self.project.root):
            self.load()
            if self.data['status'] != 'waiting' or self.data['gate'] != args.gate:
                raise WorkflowError('GATE_MISMATCH stale or already consumed gate')
            if self.data['reason'] in ('EXECUTION_UNKNOWN','CONTROL_CHANGED','STATE_CHANGED','STOP_REQUESTED'):
                raise WorkflowError('RECOVERY_REQUIRED inspect execution/control changes before continuing')
            gate_file = self.folder / 'gates' / (args.gate + '.json')
            gate = read(gate_file)
            if not args.answer.strip() or not args.quote.strip():
                raise WorkflowError('ANSWER_REQUIRED')
            gate.update(status='answered', answer=redact(args.answer), user_quote=redact(args.quote), answered_at=now())
            atomic(gate_file, gate)
            self.record_answer('caller-attributed-user', args.answer)
            self.data.update(status='runnable', gate=None, reason=None)
            self.save('answered')
            return self.data

    @contextmanager
    def own_lease(self):
        self.load()
        owner = {**identity(os.getpid()), 'task': self.task, 'nonce': uuid.uuid4().hex, 'at': now()}
        candidate = self.project.meta / ('lease-candidate-' + owner['nonce'] + '.json')
        atomic(candidate, owner)
        try:
            try:
                os.link(candidate, self.lease)  # Publish a fully written owner, exclusively.
            except FileExistsError as exc:
                raise WorkflowError('DRIVER_BUSY inspect owner; dead leases require drive-recover') from exc
        finally:
            candidate.unlink(missing_ok=True)
        try:
            yield
        finally:
            if self.lease.exists() and read(self.lease).get('nonce') == owner['nonce']:
                self.lease.unlink()

    def loop(self):
        with self.own_lease():
            while True:
                action = self.next()
                if action['action'] == 'awaiting_result':
                    with locked(self.project.root):
                        self.load()
                        return self.gate('EXECUTION_UNKNOWN', 'An earlier attempt has no accepted result; inspect it before redispatch.')
                if action['action'] != 'dispatch':
                    return self.load()
                job = action['job']
                try:
                    self.launch(job)
                except (WorkflowError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                    with locked(self.project.root):
                        self.load()
                        reason = 'STOP_REQUESTED' if str(exc).startswith('STOP_REQUESTED') else 'EXECUTION_UNKNOWN'
                        self.gate(reason, str(exc))
                    return self.data

    def launch(self, job):
        config = self.data['config']
        argv = config['host_command'] if config['adapter'] == 'command' else codex_argv(job, config['codex_bin'])
        env = dict(os.environ, GSP_DRIVER_JOB=job['job_file'], GSP_DRIVER_RESULT=job['result'])
        folder = Path(job['result']).parent
        remaining = min(
            config['timeout'],
            self.data['deadline'] - time.time(),
        )
        if remaining <= 0:
            raise WorkflowError('DEADLINE_EXCEEDED')
        slot=None
        if config['adapter']=='codex':
            from .resource_registry import ResourceRegistry,ResourceOwner,ResourceRegistryError
            from .orchestration_scheduler import aggregate_usage
            with locked(self.project.root):
                self.load()
                usage=aggregate_usage([event for row in self.data.get('native_usage',[])
                                       for event in row.get('events',[])])
                if usage.get('remaining_budget')==0:
                    raise WorkflowError('NATIVE_BUDGET_EXHAUSTED')
                state=self.project.ledger_state(self.task)
                owner=ResourceOwner(state['work_uuid'],'native-worker:'+job['id'],
                                    int(state.get('controller_generation') or 1))
                request_uid='native-worker:'+job['id']
                try:
                    granted=ResourceRegistry(self.project.root).reserve_execution_slot(request_uid,owner)
                except ResourceRegistryError as exc:
                    raise WorkflowError(str(exc)) from exc
                slot={'request_uid':request_uid,'owner':vars(owner),
                      'identity':granted['resources'][0]['identity']}
                job['execution_slot']=slot
                self.data['pending']['execution_slot']=slot
                self.save('native-capacity-reserved')
        started = {}

        def on_started(process):
            with locked(self.project.root):
                self.load()
                self.data['pending']['worker'] = identity(process.pid)
                self.save('host-started')
                started['driver_digest'] = digest(self.file)

        def cancel_check():
            if self.stop_requested():
                return ('STOP_REQUESTED host terminated; inspect partial changes '
                        'before recovery')
            return None

        prompt_text = prompt(job)
        metadata = {
            'task': (job.get('task') or {}).get('id'),
            'job': job['id'],
            'argv': [redact(value) for value in argv],
        }
        if config['adapter'] == 'command':
            execution = execute_command(
                argv, self.project.root, env, remaining, folder, metadata,
                on_started=on_started, input_data=prompt_text.encode('utf-8'),
                cancel_check=cancel_check,
            )
            worker_status = None
        else:
            worker = run_worker(
                job, self.project.root, env, remaining, folder, prompt_text,
                executable=config['codex_bin'],
                thread_id=job.get('native_resume_id'),
                on_started=on_started, cancel_check=cancel_check,
            )
            execution = worker['execution']
            worker_status = worker.get('status')
            # Failed quality is distinct from a live/unknown host. A confirmed
            # process terminal can release capacity without accepting its work.
            if (slot and not execution.get('process_alive')
                    and type(execution.get('returncode')) is int
                    and execution.get('status') in {'completed','failed'}
                    and worker_status in {'completed','failed'}):
                ResourceRegistry(self.project.root).release_execution_slot(
                    slot['request_uid'],slot['owner'],identity=slot['identity'])
                ResourceRegistry(self.project.root).release(slot['request_uid'],slot['owner'])

        if ('driver_digest' in started
                and digest(self.file) != started['driver_digest']):
            raise WorkflowError(
                'CONTROL_CHANGED driver bookkeeping changed during host execution')
        if execution.get('status') == 'cancelled':
            raise WorkflowError(
                execution.get('cancel_reason')
                or 'STOP_REQUESTED host terminated; inspect partial changes')
        if execution.get('status') == 'timeout':
            raise WorkflowError('HOST_TIMEOUT outcome unknown; inspect partial changes')
        if execution.get('launch_error') and execution.get('process_identity') is None:
            raise WorkflowError(
                'HOST_LAUNCH_FAILED outcome unknown; inspect partial changes')
        if execution.get('process_alive'):
            raise WorkflowError(
                'HOST_EXECUTION_UNKNOWN process identity is still alive')
        integrity = execution.get('log_integrity') or {}
        if any((value or {}).get('truncated') for value in integrity.values()):
            raise WorkflowError(
                'HOST_LOG_TRUNCATED outcome unknown; inspect partial evidence')
        if worker_status not in (None, 'completed'):
            raise WorkflowError(
                'HOST_EXECUTION_UNKNOWN worker evidence status ' + worker_status)
        code = execution.get('returncode')
        if execution.get('status') != 'completed' and code in (None, 0):
            raise WorkflowError(
                'HOST_EXECUTION_UNKNOWN missing terminal outcome')
        if config['adapter'] == 'codex':
            from .codex_worker_adapter import context_key
            native_id = worker.get('native_session_id')
            try:
                native_id = str(uuid.UUID(native_id))
            except (ValueError, TypeError, AttributeError) as exc:
                raise WorkflowError('NATIVE_CONTEXT_REQUIRED completed worker has no exact native identity') from exc
            with locked(self.project.root):
                self.load()
                self.data['pending']['native_session_id'] = native_id
                usage_record={'job_id':job['id'],'native_session_id':native_id,
                              'events':worker.get('usage_events') or [],
                              'requested_model':'group/deepseek-2','requested_effort':'max'}
                records=self.data.setdefault('native_usage',[])
                previous=next((row for row in records if row.get('job_id')==job['id']),None)
                if previous is not None and previous!=usage_record:
                    raise WorkflowError('NATIVE_USAGE_CHANGED inspect the original response records')
                if previous is None:
                    records.append(usage_record)
                key = context_key(job)
                if key:
                    self.data.setdefault('native_contexts', {})[key] = {
                        'native_session_id': native_id, 'model_alias': 'group/deepseek-2',
                        'effort': 'max', 'last_job': job['id'],
                    }
                self.save('native-context-recorded')
        self.submit(job['id'], job['result'], code)

    def rejected_plan_for_recovery(self, state, pending):
        """Reconcile a durable rejection before changing either state file."""
        if not pending or pending['phase'] != 'plan':
            return None
        attempt = self.folder / 'attempts' / slug(pending['id'])
        marker = attempt / 'rejected.json'
        if not marker.exists():
            return None
        try:
            response = attempt / 'response.json'
            rejected = read(marker)
            if (Path(pending['result']).resolve() != response.resolve()
                    or (attempt / 'accepted.json').exists()
                    or response.stat().st_size > 2_000_000
                    or rejected['job'] != pending['id']
                    or rejected['sha256'] != digest(response)):
                raise WorkflowError('rejection identity or response changed')
            result = read(response)
            self.validate_result(result, pending)
            if result['outcome'] != 'completed':
                raise WorkflowError('rejection does not describe a completed candidate')
            _, quality = self.plan_candidate(state, result['plan'])
            if quality['structural_ok'] or rejected['quality'] != quality:
                raise WorkflowError('rejection diagnostics do not match candidate')
            return quality
        except (WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
            raise WorkflowError('PLAN_REJECTION_MISMATCH inspect rejected attempt: ' + str(exc)) from exc

    def _capacity_recovery(self,job,*,release=False):
        slot=(job or {}).get('execution_slot')
        if not slot:
            return
        from .resource_registry import ResourceRegistry
        from .codex_worker_adapter import _verified_log
        path=Path(job['result']).parent/'command-result.json'
        if not inside(self.folder,path) or path.is_symlink() or not path.is_file():
            raise WorkflowError('NATIVE_CAPACITY_RECOVERY_UNKNOWN original terminal evidence required')
        terminal=read(path)
        if (terminal.get('process_alive') or terminal.get('status') not in
                {'completed','failed','timeout','cancelled','launch-failed'}
                or type(terminal.get('returncode')) is not int
                or not all(_verified_log(terminal,name) for name in ('stdout','stderr'))):
            raise WorkflowError('NATIVE_CAPACITY_RECOVERY_UNKNOWN terminal/log evidence is incomplete')
        process=(job or {}).get('worker')
        if not isinstance(process,dict) or not process.get('pid') or not process.get('token') or is_alive(process):
            raise WorkflowError('NATIVE_CAPACITY_RECOVERY_UNKNOWN process identity is live or unknown')
        if release:
            registry=ResourceRegistry(self.project.root)
            registry.release_execution_slot(slot['request_uid'],slot['owner'],identity=slot['identity'])
            registry.release(slot['request_uid'],slot['owner'])

    def recover(self, args):
        report = Path(args.evidence).resolve()
        if not inside(self.project.root, report) or not report.is_file() or not args.reason.strip():
            raise WorkflowError('RECOVERY_EVIDENCE_REQUIRED local investigation report and reason required')
        reason = args.reason.strip()
        self.load()
        request = self._recovery_request(report, reason)
        pending_recovery = self.data.get('pending_recovery')
        if pending_recovery:
            self._pending_recovery_request_matches(pending_recovery, request)
            if self.stop_requested():
                raise WorkflowError('RECOVERY_INPUT_CHANGED stop request appeared during recovery')
        elif not self._recovery_is_required() and self._completed_recovery_noop(request) is None:
            raise WorkflowError('RECOVERY_NOT_REQUIRED no interrupted driver operation to recover')

        lease = read(self.lease) if self.lease.exists() else None
        if lease and (lease.get('task') != self.task or is_alive(lease)):
            raise WorkflowError('DRIVER_BUSY live or another task owner cannot be recovered')
        worker = (self.data.get('pending') or {}).get('worker')
        if is_alive(worker) or is_alive(self.data.get('pending_check')):
            raise WorkflowError('WORKER_ACTIVE inspect/wait for the recorded worker before recovery')

        with locked(self.project.root):
            self.load()
            pending_recovery = self.data.get('pending_recovery')
            if pending_recovery:
                self._pending_recovery_request_matches(pending_recovery, request)
                if self.stop_requested():
                    raise WorkflowError('RECOVERY_INPUT_CHANGED stop request appeared during recovery')
            else:
                completed = self._completed_recovery_noop(request)
                if completed is not None:
                    self._repair_completed_recovery_projection(completed)
                    return {
                        'action': 'no-change',
                        'status': 'already-recovered',
                        'epoch': completed.get('verification_epoch'),
                        'operation_uid': completed.get('operation_uid'),
                    }
                if not self._recovery_is_required():
                    raise WorkflowError('RECOVERY_NOT_REQUIRED no interrupted driver operation to recover')

            lease = read(self.lease) if self.lease.exists() else None
            if lease and (lease.get('task') != self.task or is_alive(lease)):
                raise WorkflowError('DRIVER_BUSY live or another task owner cannot be recovered')
            worker = (self.data.get('pending') or {}).get('worker')
            if is_alive(worker) or is_alive(self.data.get('pending_check')):
                raise WorkflowError('WORKER_ACTIVE inspect/wait for the recorded worker before recovery')

            if pending_recovery and lease is not None:
                lease_digest = sha(json.dumps(
                    lease, ensure_ascii=False, sort_keys=True, separators=(',', ':')
                ).encode('utf-8'))
                if lease_digest != pending_recovery.get('stale_lease_sha256'):
                    raise WorkflowError('RECOVERY_INPUT_CHANGED stale driver lease changed')

            state, _, _ = self.project.state(self.task, recovery=True)
            self.project.valid_design(state, self.doc)
            if state['design_approval']['sha256'] != self.data['binding']['design']:
                raise WorkflowError('DESIGN_RECONFIGURATION_REQUIRED do not rebind approval through recovery')
            pending = self.data.get('pending')
            self._capacity_recovery(pending)
            if pending_recovery:
                self._pending_recovery_plan_matches(pending_recovery, state)
                if self.data.get('revision') != pending_recovery.get('prepared_revision'):
                    raise WorkflowError('RECOVERY_STATE_CHANGED driver state advanced during recovery')
                self._repair_pending_recovery_projection(pending_recovery)
            else:
                plan_proof = self._recovery_plan_proof(state)
                rejected_plan = self.rejected_plan_for_recovery(state, pending)
                sealed_plan = state.get('plan_seal') and self.data['phase'] == 'plan'
                if sealed_plan:
                    self.project.valid_plan(state, self.doc)
                candidate = copy.deepcopy(state)
                interrupted_check = candidate.pop('inflight', None)
                candidate['verification_epoch'] = candidate.get('verification_epoch', 0) + 1
                candidate['review'] = None
                candidate['status'] = 'needs-verification'
                driver_updates = {
                    'status': 'runnable',
                    'gate': None,
                    'reason': None,
                    'pending': None,
                    'pending_check': None,
                    'stop_requested': False,
                }
                if rejected_plan:
                    driver_updates.update(
                        phase='plan', pending_fix='plan', plan_findings=rejected_plan['findings']
                    )
                elif pending and pending['phase'] == 'review':
                    driver_updates['phase'] = 'verify'
                elif candidate.get('plan_seal') and candidate['tasks'] and all(
                        task['status'] == 'done' for task in candidate['tasks']):
                    driver_updates.update(phase='verify', pending_fix=None, plan_findings=[])
                elif sealed_plan:
                    driver_updates.update(phase='execute', pending_fix=None, plan_findings=[])

                recoveries = self.data.get('recoveries') or []
                sequence = len(recoveries) + 1
                previous_incident_uid = (
                    recoveries[-1].get('incident_uid') if recoveries else None
                )
                expected_state_digest = sha((self.run / 'state.json').read_bytes())
                incident_material = {
                    'task': self.task,
                    'sequence': sequence,
                    'previous_incident_uid': previous_incident_uid,
                    'driver_revision': self.data.get('revision', 0),
                    'driver_status': self.data.get('status'),
                    'driver_reason': self.data.get('reason'),
                    'driver_gate': self.data.get('gate'),
                    'attempt': pending,
                    'expected_state_digest': expected_state_digest,
                    'request': request,
                    'plan_proof': copy.deepcopy(plan_proof),
                }
                incident_uid = stable_action_uid('driver-recovery-incident', incident_material)
                action_uid = stable_action_uid('driver-recovery', incident_uid)
                prepared_revision = self.data.get('revision', 0) + 1
                completed_revision = prepared_revision + 1
                prepared_at = now()
                completed_at = now()
                prepared_event_uid = stable_action_uid(
                    'driver-recovery-prepared-event', incident_uid
                )
                completed_event_uid = stable_action_uid(
                    'driver-recovery-completed-event', incident_uid
                )
                lease_digest = (
                    sha(json.dumps(
                        lease, ensure_ascii=False, sort_keys=True, separators=(',', ':')
                    ).encode('utf-8'))
                    if lease is not None else None
                )
                check_process = self.data.get('pending_check')
                recovery_record = {
                    'at': completed_at,
                    'incident_uid': incident_uid,
                    'sequence': sequence,
                    'previous_incident_uid': previous_incident_uid,
                    'action_uid': action_uid,
                    'request': request,
                    'evidence': str(report),
                    'sha256': request['evidence_sha256'],
                    'reason': request['reason'],
                    'attempt': copy.deepcopy(pending),
                    'interrupted_check': copy.deepcopy(interrupted_check),
                    'check_process': copy.deepcopy(check_process),
                    'verification_epoch': candidate['verification_epoch'],
                    'expected_state_digest': expected_state_digest,
                    'plan_proof': copy.deepcopy(plan_proof),
                }
                pending_recovery = {
                    'incident_uid': incident_uid,
                    'action_uid': action_uid,
                    'request': request,
                    'expected_state_digest': expected_state_digest,
                    'candidate_state': candidate,
                    'driver_updates': driver_updates,
                    'recovery_record': recovery_record,
                    'prepared_revision': prepared_revision,
                    'completed_revision': completed_revision,
                    'stale_lease_sha256': lease_digest,
                    'plan_proof': copy.deepcopy(plan_proof),
                    'prepared_event': self._recovery_event(
                        'recovery-prepared', prepared_revision, self.data['dispatches'],
                        self.data.get('reason'), prepared_at, prepared_event_uid,
                    ),
                    'completed_event': self._recovery_event(
                        'recovered', completed_revision, self.data['dispatches'],
                        None, completed_at, completed_event_uid,
                    ),
                }
                self.data['pending_recovery'] = pending_recovery
                self.save(
                    'recovery-prepared',
                    event_uid=prepared_event_uid,
                    event_at=prepared_at,
                )

            self._pending_recovery_plan_matches(pending_recovery, state)
            self._accepted_recovery_workflow(pending_recovery)
            self.project.write(
                copy.deepcopy(pending_recovery['candidate_state']),
                'driver-recovery',
                pending_recovery['request']['reason'],
                action_uid=pending_recovery['action_uid'],
            )
            operation = self._recovery_operation(pending_recovery['action_uid'])
            if operation is None or operation.get('phase') != 'committed':
                raise WorkflowError('RECOVERY_OPERATION_UNRESOLVED project write did not commit')

            lease = read(self.lease) if self.lease.exists() else None
            if lease and (lease.get('task') != self.task or is_alive(lease)):
                raise WorkflowError('DRIVER_BUSY live or another task owner cannot be completed')
            worker = (self.data.get('pending') or {}).get('worker')
            if is_alive(worker) or is_alive(self.data.get('pending_check')):
                raise WorkflowError('WORKER_ACTIVE inspect/wait for the recorded worker before recovery')
            if lease is not None:
                lease_digest = sha(json.dumps(
                    lease, ensure_ascii=False, sort_keys=True, separators=(',', ':')
                ).encode('utf-8'))
                if lease_digest != pending_recovery.get('stale_lease_sha256'):
                    raise WorkflowError('RECOVERY_INPUT_CHANGED stale driver lease changed')
                self.lease.unlink()

            record = copy.deepcopy(pending_recovery['recovery_record'])
            self._capacity_recovery(record.get('attempt'),release=True)
            record.update({
                'operation_uid': operation['operation_uid'],
                'workflow_result_digest': sha((self.run / 'state.json').read_bytes()),
                'completed_driver_revision': pending_recovery['completed_revision'],
                'completed_status': 'runnable',
                'prepared_event': copy.deepcopy(pending_recovery['prepared_event']),
                'completed_event': copy.deepcopy(pending_recovery['completed_event']),
            })
            self.data.update(copy.deepcopy(pending_recovery['driver_updates']))
            self.data.setdefault('recoveries', []).append(record)
            self.data.pop('pending_recovery', None)
            (self.folder / 'stop.json').unlink(missing_ok=True)
            self.save(
                'recovered',
                event_uid=pending_recovery['completed_event']['event_uid'],
                event_at=pending_recovery['completed_event']['at'],
            )
            return self.data

    def status(self):
        self.load()
        if self.data['status'] == 'completed':
            self.next()  # Revoke an obsolete completion, rather than displaying stale success.
        return self.load()

    def stop(self, quote):
        if not quote.strip():
            raise WorkflowError('USER_QUOTE_REQUIRED')
        self.load()
        request={'at':now(),'quote':redact(quote),'task':self.task}
        # A check holds the workflow writer lock; stopping must remain reachable.
        atomic(self.folder/'stop.json',request)
        return {'stop_requested':True,'request':request,'status':self.data['status']}

    def start(self):
        self.load()
        if self.lease.exists():
            raise WorkflowError('DRIVER_BUSY inspect/recover existing lease first')
        script = Path(__file__).resolve().parents[1] / 'scripts/gsp.py'
        if not script.is_file():
            script = Path(__file__).resolve().parents[1] / 'gsp.py'
        argv = [sys.executable, str(script), '--project', str(self.project.root), 'drive-run', self.task]
        with (self.folder / 'daemon.log').open('a', encoding='utf-8') as log:
            try:
                process = subprocess.Popen(argv, cwd=self.project.root, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **child_options(detached=True))
            except OSError as exc:
                raise WorkflowError('DETACH_UNAVAILABLE use drive-run in a separately started terminal; background independence was not established: ' + str(exc)) from exc
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.lease.exists() and read(self.lease).get('pid') == process.pid:
                return {'started': identity(process.pid), 'status': self.load()['status'], 'log': str(self.folder / 'daemon.log')}
            if process.poll() is not None:
                return {'started': False, 'exit': process.returncode, 'status': self.load()['status'], 'log': str(self.folder / 'daemon.log')}
            time.sleep(.1)
        return {'started': identity(process.pid), 'status': 'starting', 'log': str(self.folder / 'daemon.log')}


def configure(sub):
    q = sub.add_parser('drive-init'); q.add_argument('id'); q.add_argument('--checks', required=True); q.add_argument('--quote', required=True)
    q.add_argument('--check-kind', choices=['tests','check'], default='tests'); q.add_argument('--host-command'); q.add_argument('--codex-bin')
    q.add_argument('--max-dispatches', type=int, default=20); q.add_argument('--timeout', type=float, default=900); q.add_argument('--max-seconds', type=float, default=14400)
    for name in ('drive-next','drive-run','drive-start','drive-status'):
        sub.add_parser(name).add_argument('id')
    q = sub.add_parser('drive-submit'); q.add_argument('id'); q.add_argument('--job', required=True); q.add_argument('--result', required=True); q.add_argument('--exit-code', type=int, required=True)
    q = sub.add_parser('drive-answer'); q.add_argument('id'); q.add_argument('--gate', required=True); q.add_argument('--answer', required=True); q.add_argument('--quote', required=True)
    q = sub.add_parser('drive-recover'); q.add_argument('id'); q.add_argument('--evidence', required=True); q.add_argument('--reason', required=True)
    q = sub.add_parser('drive-stop'); q.add_argument('id'); q.add_argument('--quote', required=True)


def handle(project, args):
    driver = Driver(project, args.id)
    if args.command == 'drive-init': return driver.initialize(args)
    if args.command == 'drive-status': return driver.status()
    if args.command == 'drive-next': return driver.next()
    if args.command == 'drive-submit': return driver.submit(args.job, args.result, args.exit_code)
    if args.command == 'drive-answer': return driver.answer(args)
    if args.command == 'drive-recover': return driver.recover(args)
    if args.command == 'drive-start': return driver.start()
    if args.command == 'drive-stop': return driver.stop(args.quote)
    return driver.loop()
