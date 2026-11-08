"""Durable local headless controller. Host prose never directly completes a task."""
from argparse import Namespace
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

from .runtime import (Project, WorkflowError, atomic, digest, dispatch, fingerprint, inside,
                      locked, now, read, redact, run_check, sha, slug)
from .hosts import child_options, codex_argv, identity, is_alive, prompt, terminate_owned


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

    def save(self, event):
        self.data['updated'] = now()
        self.data['revision'] = self.data.get('revision', 0) + 1
        atomic(self.file, self.data)
        with (self.folder / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'at': now(), 'event': event, 'revision': self.data['revision'],
                                     'dispatches': self.data['dispatches'], 'reason': self.data.get('reason')}) + '\n')
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
        atomic(self.doc / 'driver-report.md', text)

    def initialize(self, args):
        with locked(self.project.root):
            if self.file.exists():
                raise WorkflowError('DRIVER_EXISTS limits and consumed attempts cannot be reset by reinitializing')
            state, _, doc = self.project.state(self.task)
            self.project.valid_design(state, doc)
            if state['mode'] != 'unattended' or not state.get('mode_quote'):
                raise WorkflowError('UNATTENDED_AUTHORIZATION_REQUIRED create the task with actual unattended authorization')
            if state['scope']['mode'] == 'pending':
                raise WorkflowError('SCOPE_PENDING')
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
                         'pending_fix': None, 'review_findings': [], 'answers': [], 'deadline': None,
                         'binding': {'design': state['design_approval']['sha256'], 'scope': state['scope'], 'authorizations': state['authorizations'], 'mode_quote': state['mode_quote']},
                         'config': {'checks': checks, 'check_kind': args.check_kind, 'adapter': 'command' if command else 'codex',
                                    'host_command': command, 'codex_bin': args.codex_bin, 'max_fix_rounds': rounds,
                                    'timeout': args.timeout, 'max_seconds': args.max_seconds, 'max_dispatches': args.max_dispatches,
                                    'quote': args.quote}, 'created': now()}
            self.save('initialized')
            return self.data

    def validate_workflow(self):
        state, _, doc = self.project.state(self.task)
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
        remaining = self.data['deadline'] - time.time()
        if remaining <= 0:
            raise WorkflowError('DEADLINE_EXCEEDED')
        args = Namespace(task=task, kind=self.data['config']['check_kind'], argv=self.data['config']['checks'],
                         timeout=min(self.data['config']['timeout'], remaining), runner=self.run_owned_check)
        return run_check(self.project, state, self.run, args)

    def stop_requested(self):
        return (self.folder/'stop.json').exists() or read(self.file).get('stop_requested', False)

    def run_owned_check(self, argv, timeout):
        process = subprocess.Popen(argv, cwd=self.project.root, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8', errors='replace', **child_options())
        try:
            self.data['pending_check'] = identity(process.pid)
            self.save('check-started')
            deadline = min(time.time()+timeout, self.data['deadline'])
            while True:
                if self.stop_requested():
                    raise WorkflowError('STOP_REQUESTED check terminated; inspect partial changes')
                remaining=deadline-time.time()
                if remaining <= 0:
                    raise WorkflowError('CHECK_TIMEOUT outcome unknown; inspect partial changes')
                try:
                    stdout,stderr=process.communicate(timeout=min(.1,remaining))
                    break
                except subprocess.TimeoutExpired:
                    pass
            self.data['pending_check'] = None
            self.save('check-exited')
            return subprocess.CompletedProcess(argv,process.returncode,stdout,stderr)
        except BaseException:
            terminate_owned(process)
            process.communicate()
            raise

    def next(self):
        with locked(self.project.root):
            self.load()
            if self.stop_requested():
                self.data.update(status='stopped', reason='STOP_REQUESTED')
                self.save('stopped')
                return {'action': 'stopped'}
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
            if phase == 'deliver':
                try:
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
                   'review_findings': self.data['review_findings'], 'answers': self.data['answers'][-5:],
                   'checks': self.data['config']['checks'], 'check_kind': self.data['config']['check_kind'],
                   'schema': str(folder / 'schema.json'), 'result': str(folder / 'response.json'),
                   'job_file': str(folder / 'job.json'), 'at': now(), 'worker': None}
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
                self.data['phase'] = 'execute'
            elif job['phase'] in ('execute', 'fix-task'):
                receipt, passed = self.check(state, job['task']['id'])
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

    def apply_plan(self, state, plan):
        if state['tasks']:
            raise WorkflowError('PLAN_RECONCILIATION_REQUIRED preserve existing tasks; seal their plan explicitly')
        spec_link = self.project.document_link(state, 'design', self.project.document(state, 'plan').parent)
        atomic(self.project.document(state, 'plan'), f'Spec: [approved design]({spec_link})\n\n' + plan['text'])
        for task in plan['tasks']:
            dispatch(self.project, Namespace(command='task-add', id=self.task, task=task['id'], title=task['title'],
                acceptance=task['acceptance'], depends=','.join(task['depends']), no_tests_reason=task['no_tests_reason'] or None))
        dispatch(self.project, Namespace(command='seal-plan', id=self.task))

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
        with (folder / 'stdout.log').open('w', encoding='utf-8') as stdout, (folder / 'stderr.log').open('w', encoding='utf-8') as stderr:
            process = subprocess.Popen(argv, cwd=self.project.root, env=env, stdin=subprocess.PIPE,
                                       stdout=stdout, stderr=stderr, text=True, encoding='utf-8', **child_options())
            try:
                with locked(self.project.root):
                    self.load()
                    self.data['pending']['worker'] = identity(process.pid)
                    self.save('host-started')
                    expected_driver = digest(self.file)
                # A wedged host may never read stdin. Keep timeout/stop monitoring
                # independent of pipe capacity, including for large task context.
                input_errors = []
                def feed():
                    try:
                        process.stdin.write(prompt(job))
                        process.stdin.close()
                    except (OSError, ValueError) as exc:
                        input_errors.append(str(exc))
                feeder = threading.Thread(target=feed, daemon=True)
                feeder.start()
                deadline = min(time.time() + config['timeout'], self.data['deadline'])
                while process.poll() is None:
                    if self.stop_requested():
                        terminate_owned(process)
                        raise WorkflowError('STOP_REQUESTED host terminated; inspect partial changes before recovery')
                    if time.time() >= deadline:
                        terminate_owned(process)
                        raise WorkflowError('HOST_TIMEOUT outcome unknown; inspect partial changes')
                    time.sleep(.1)
                feeder.join(timeout=1)
                if feeder.is_alive() or input_errors:
                    raise WorkflowError('HOST_INPUT_INCOMPLETE inspect host stdin failure')
                if digest(self.file) != expected_driver:
                    raise WorkflowError('CONTROL_CHANGED driver bookkeeping changed during host execution')
                code = process.returncode
            except BaseException:
                terminate_owned(process)
                raise
        # Logs remain local. Common credential patterns are redacted; no upload occurs.
        for name in ('stdout.log', 'stderr.log'):
            file = folder / name
            atomic(file, redact(file.read_text(encoding='utf-8', errors='replace')))
        self.submit(job['id'], job['result'], code)

    def recover(self, args):
        report = Path(args.evidence).resolve()
        if not inside(self.project.root, report) or not report.is_file() or not args.reason.strip():
            raise WorkflowError('RECOVERY_EVIDENCE_REQUIRED local investigation report and reason required')
        self.load()
        lease = read(self.lease) if self.lease.exists() else None
        if lease and (lease.get('task') != self.task or is_alive(lease)):
            raise WorkflowError('DRIVER_BUSY live or another task owner cannot be recovered')
        worker = (self.data.get('pending') or {}).get('worker')
        if is_alive(worker) or is_alive(self.data.get('pending_check')):
            raise WorkflowError('WORKER_ACTIVE inspect/wait for the recorded worker before recovery')
        lock = self.project.meta / 'writer.lock'
        if lock.exists():
            writer = read(lock)
            if not lease or writer.get('pid') != lease.get('pid') or is_alive(writer):
                raise WorkflowError('WRITER_LOCK_RECOVERY_REQUIRED inspect its distinct owner')
            lock.unlink()  # Exact verified dead driver-owned lock, never age-based stealing.
        if lease:
            self.lease.unlink()
        with locked(self.project.root):
            self.load()
            state, _, _ = self.project.state(self.task)
            self.project.valid_design(state, self.doc)
            if state['design_approval']['sha256'] != self.data['binding']['design']:
                raise WorkflowError('DESIGN_RECONFIGURATION_REQUIRED do not rebind approval through recovery')
            interrupted_check = state.pop('inflight', None)
            state['verification_epoch'] = state.get('verification_epoch', 0) + 1
            state['review'] = None
            state['status'] = 'needs-verification'
            self.project.write(state, 'driver-recovery', args.reason)
            pending = self.data.get('pending')
            if pending and pending['phase'] == 'review':
                self.data['phase'] = 'verify'
            elif state.get('plan_seal') and state['tasks'] and all(t['status'] == 'done' for t in state['tasks']):
                self.data.update(phase='verify', pending_fix=None)
            elif state.get('plan_seal') and self.data['phase'] == 'plan':
                self.data['phase'] = 'execute'
            check_process=self.data.get('pending_check')
            self.data.update(status='runnable', gate=None, reason=None, pending=None, pending_check=None, stop_requested=False)
            (self.folder/'stop.json').unlink(missing_ok=True)
            self.data.setdefault('recoveries', []).append({'at': now(), 'evidence': str(report), 'sha256': digest(report), 'reason': redact(args.reason), 'attempt': pending, 'interrupted_check': interrupted_check, 'check_process':check_process})
            self.save('recovered')
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
