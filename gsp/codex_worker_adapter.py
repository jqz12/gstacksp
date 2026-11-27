"""Thin Codex exec/resume adapter over the shared command executor."""
import hashlib
import json
from pathlib import Path
import uuid

from .command_execution import execute_command
from .hosts import codex_argv


def context_key(job):
    """Business association only; Codex owns the actual conversation/history."""
    task = job.get('task') or {}
    contract = job.get('task_contract') or {}
    return str(contract.get('task_uid') or task.get('uid') or task.get('id') or '')


def correction_context(job, contexts, *, correcting):
    if not correcting or job.get('phase') == 'review':
        return None  # Independent SDD tasks and reviewers always start fresh.
    key = context_key(job)
    if not key:
        return None
    previous = contexts.get(key) or {}
    value = previous.get('native_session_id')
    try:
        exact = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('NATIVE_CONTEXT_REQUIRED recover the exact original executor or let Sol take over') from exc
    if previous.get('model_alias') != 'group/deepseek-2':
        raise ValueError('WORKER_MODEL_REQUIRED original executor model is unverified')
    return exact


def worker_argv(job, executable=None, thread_id=None):
    argv = codex_argv(job, executable)
    if thread_id is None:
        return argv
    try:
        exact_id = str(uuid.UUID(thread_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('NATIVE_THREAD_ID_REQUIRED exact UUID, never --last') from exc
    index = argv.index('exec')
    options = argv[index + 1:]
    sandbox = options.index('--sandbox')
    mode = options[sandbox + 1]
    del options[sandbox:sandbox + 2]
    return argv[:index] + ['-c', 'sandbox_mode=' + json.dumps(mode),
                           'exec', 'resume', exact_id] + options


def parse_native_events(text):
    threads, completions, warnings = [], [], []
    usage_events=[]
    seen = set()
    actual_model = 'unknown'
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            warnings.append('invalid-jsonl')
            continue
        if not isinstance(event, dict):
            warnings.append('invalid-event')
            continue
        if event.get('type') == 'thread.started' and isinstance(event.get('thread_id'), str):
            threads.append(event['thread_id'])
        if event.get('type') == 'model.selected' and isinstance(event.get('model'), str):
            actual_model = event['model']
        if event.get('type') != 'turn.completed':
            continue
        usage_events.append(event)
        ident = event.get('response_id') or event.get('id')
        if ident is not None:
            if ident in seen:
                continue
            seen.add(ident)
        completions.append(event)
    keys = ('input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_tokens')
    usage = {key: None for key in keys}
    # Anonymous multiple turns cannot safely be aggregated into unique usage.
    if completions and (len(completions) == 1 or len(seen) == len(completions)):
        for key in keys:
            values = [(row.get('usage') or {}).get(key) for row in completions]
            if all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in values):
                usage[key] = sum(values)
    if not threads:
        warnings.append('missing-thread-started')
    elif len(set(threads)) != 1:
        warnings.append('multiple-thread-started')
    if not completions:
        warnings.append('missing-turn-completed')
    return {'native_session_id': threads[-1] if threads else None,
            'completed_turns': len(completions), 'usage': usage,
            'usage_scope': 'native-events; descendant inclusion unknown',
            'actual_model': actual_model, 'warnings': sorted(set(warnings)),
            'usage_events':usage_events,'accepted': False}


def _verified_log(execution, name):
    integrity = (execution.get('log_integrity') or {}).get(name) or {}
    path_value = execution.get(name + '_path')
    try:
        data = Path(path_value).read_bytes()
    except (OSError, TypeError, ValueError):
        return False
    return (
        integrity.get('truncated') is False
        and isinstance(integrity.get('bytes'), int)
        and integrity.get('bytes') == len(data)
        and isinstance(integrity.get('sha256'), str)
        and hashlib.sha256(data).hexdigest() == integrity.get('sha256')
    )


def run_worker(job, cwd, env, timeout, folder, contract_text, *,
               executable=None, thread_id=None, on_started=None,
               cancel_check=None):
    argv = worker_argv(job, executable, thread_id)
    execution = execute_command(
        argv, cwd, env, timeout, folder,
        {'task': job.get('task'), 'argv': argv, 'requested_model': 'group/deepseek-2',
         'requested_effort': 'max', 'native_resume_id': thread_id},
        on_started=on_started, input_data=contract_text.encode('utf-8'),
        cancel_check=cancel_check)
    intact = all(_verified_log(execution, name) for name in ('stdout', 'stderr'))
    terminal_alive = bool(execution.get('process_alive'))
    stdout = Path(execution['stdout_path'])
    events = parse_native_events(stdout.read_text(encoding='utf-8', errors='replace'))
    from .orchestration_scheduler import aggregate_usage
    # The launched native argv disables descendants. Explicit contradictory
    # native scope facts still remain unknown to the shared aggregator.
    events['usage_events']=[{**row,'scope':row.get('scope','flat')}
                            for row in events['usage_events']]
    events['usage_decision']=aggregate_usage(events['usage_events'])
    status = execution['status']
    if not intact or terminal_alive or events['warnings']:
        status = 'evidence-incomplete'
    if thread_id is not None and events['native_session_id'] != thread_id:
        status = 'native-thread-mismatch'
    return {**events, 'execution': execution, 'status': status,
            'requested_model': 'group/deepseek-2', 'requested_effort': 'max',
            'accepted': False}
