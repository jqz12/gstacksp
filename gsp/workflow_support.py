"""workflow support; shared business implementation, independent of CLI."""
import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import contextlib
try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None
try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX
    _msvcrt = None
from . import __version__
from . import core as _core
from .validation_context import (ValidationContext, activate, current_context,
                                 deactivate, invalidate)
from .operation_journal import REPLAYABLE_STATE_COMMANDS

from .core import (WorkflowError, atomic, cached_value, digest, inside, now, read, redact, retry_access, sha)
from .git_operations import (_GIT_MUTATIONS, _git_run, git_identity, is_ancestor)
from .workflow_locks import (suspend_writer_lock)

def archive_delivery(doc):
    file = doc / 'delivery.md'
    if not file.exists():
        return None
    content = retry_access(lambda: file.read_text(encoding='utf-8'))
    if '\n\nREADY：' not in content:
        return None
    expected = content.encode('utf-8')
    for existing in sorted(doc.glob('delivery-history-*.md')):
        if retry_access(existing.read_bytes) == expected:
            return content
    history = doc / f'delivery-history-{sha(expected)}.md'
    if history.exists() and retry_access(history.read_bytes) != expected:
        raise WorkflowError('DELIVERY_HISTORY_CONFLICT')
    if not history.exists():
        atomic(history, content)
    return content


def invalidate_delivery(doc, reason):
    if archive_delivery(doc) is None:
        return
    atomic(doc / 'delivery.md', '# 交付证据已失效\n\nNOT_READY：' + reason + '\n')


def slug(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', value):
        raise WorkflowError('INVALID_ID use letters, numbers, underscore or hyphen (max 80)')
    return value


def stable_action_uid(event, *parts):
    """Return a retry-stable request identity without storing candidate state."""
    return 'act-' + sha(json.dumps(
        {'event': event, 'request': list(parts)},
        ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    ).encode())


def revalidation_action_uid(project, state, task_id, phase, entry=None):
    from .identity_store import IdentityStore
    state_file = project.paths(state["id"])[0] / "state.json"
    anchor = IdentityStore(project.root).state_write_anchor(state_file)
    return stable_action_uid("revalidation", state["id"], task_id, phase,
                             state.get("verification_epoch", 0), entry, anchor)


def project_command_effects(project, state, doc, operation_uid, committed_at):
    """Rebuild one operation's frozen auxiliary effects without duplicates."""
    marker = state.get("_command_commit") or {}
    effects = marker.get("effects") or {}
    decision = effects.get("decision")
    report = effects.get("report")
    if decision is None and report is None:
        return
    if decision is not None:
        if marker.get("command") != "task-reopen" or decision.get("kind") != "rework":
            raise WorkflowError("COMMAND_EFFECT_INVALID")
        token = "<!-- gsp-operation:" + operation_uid + ":rework -->"
        line = (f'\n- {committed_at} [rework] {decision["task"]}: {redact(decision["reason"])}; '
                f'affected: {", ".join(decision["affected"])} {token}\n')
        path = doc / "decisions.md"
        before = path.read_text(encoding="utf-8") if path.is_file() else "# Decisions\n"
        if token in before:
            if line not in before:
                raise WorkflowError("COMMAND_EFFECT_CONFLICT")
        else:
            atomic(path, before + line)
    if report is not None:
        if marker.get("command") != "session-report":
            raise WorkflowError("COMMAND_EFFECT_INVALID")
        from .sessions import apply_report_effect
        apply_report_effect(project, state, report)


ACTION_TIME_FIELDS = frozenset({
    'at', 'created_at', 'updated_at', 'completed_at', 'selected_at',
    'started_at', 'finished_at', 'checked_at',
})


ACTION_EPHEMERAL_FIELDS = frozenset({'func', 'handler'})


def _stable_action_value(value):
    if isinstance(value, dict):
        return {
            key: _stable_action_value(item)
            for key, item in value.items()
            if key not in ACTION_TIME_FIELDS
        }
    if isinstance(value, list):
        return [_stable_action_value(item) for item in value]
    if isinstance(value, tuple):
        return [_stable_action_value(item) for item in value]
    return value


def _stable_request_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_stable_request_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _stable_request_value(item)
            for key, item in value.items()
        }
    raise WorkflowError(
        'ACTION_REQUEST_ARGUMENT_INVALID '
        + type(value).__name__
    )


def _stable_request_arguments(args):
    """Return the complete public CLI contract, excluding host-only helpers."""
    if args is None or not hasattr(args, '__dict__'):
        return {}
    result = {}
    for name, value in vars(args).items():
        if (
            not isinstance(name, str)
            or name.startswith('_')
            or name in ACTION_EPHEMERAL_FIELDS
            or (name == 'argv' and not value)
        ):
            continue
        result[name] = _stable_request_value(value)
    return result


def _stable_action_context(state):
    fields = ('status', 'evidence', 'tasks', 'review', 'plan_seal',
              'design_approval', 'scope', 'git_topology', 'documents',
              'controller_generation', 'cancel_generation')
    return _stable_action_value({
        field: state.get(field) for field in fields if field in state
    })


def _stable_report_action_context(state):
    """Keep report semantics while ignoring workflow-state projections."""
    fields = (
        'work_uuid', 'current_contract', 'attempt_uid', 'verification_epoch',
        'controller_generation', 'cancel_generation',
    )
    if state.get('current_contract') is None:
        fields += ('design_approval', 'plan_seal')
    return _stable_action_value({
        field: state.get(field) for field in fields if field in state
    })


def request_action_uid(command, args, state=None, context=None):
    fields = {'arguments': _stable_request_arguments(args)}
    if state is not None:
        fields['_run_identity'] = {
            'id': state.get('id'),
            'number': state.get('number'),
            'work_uuid': state.get('work_uuid'),
            'documents': state.get('documents'),
        }
        fields['_operation_context'] = (
            _stable_report_action_context(state)
            if command == 'session-report'
            else _stable_action_context(state)
        )
    if context is not None:
        fields['_transition'] = _stable_action_value(context)
    return stable_action_uid(command, fields)


def request_binding_context(binding):
    if binding is None:
        return None
    return {
        'binding_uid': binding.binding_uid,
        'handle_path': binding.handle_path,
        'work_uid': binding.work_uid,
        'kind': binding.kind,
        'host': binding.host,
        'host_thread_id': binding.host_thread_id,
        'capabilities': sorted(binding.capabilities),
        'controller_generation': binding.controller_generation,
        'cancel_generation': binding.cancel_generation,
        'cancelled': binding.cancelled,
    }


def request_report_action_context(binding, state):
    return {
        'caller': request_binding_context(binding),
        'work_uuid': state.get('work_uuid'),
        'current_contract': state.get('current_contract'),
        'attempt_uid': state.get('attempt_uid'),
        'verification_epoch': state.get('verification_epoch'),
    }


def explicit_work_number_from_run_id(run_id):
    if not run_id.startswith('GSP-'):
        return None
    suffix = run_id[4:]
    if not suffix.isdigit():
        raise WorkflowError('WORK_NUMBER_INVALID expected canonical GSP-000001')
    number = int(suffix)
    canonical = f'GSP-{number:06d}'
    if number < 1 or run_id != canonical:
        raise WorkflowError('WORK_NUMBER_INVALID expected canonical GSP-000001')
    return canonical




def topology_required(state):
    """Whether this state opted into the Git topology gate.

    States created before topology support, legacy-layout runs and projects
    created outside a Git repository keep their historical execution path.
    New Git-backed work items get ``enforce=True`` at creation time.
    """
    return bool((state.get('git_topology') or {}).get('enforce'))


def topology_selected(state):
    mode = (state.get('git_topology') or {}).get('effective_mode')
    return mode in {'in-place', 'branch', 'worktree'}


def _sequential_parent_group(state):
    """A persisted controller choice; live write authority remains the lease."""
    group = state.get('workspace_group') or {}
    parent = state.get('git_topology') or {}
    if (group.get('schema') != 1 or group.get('mode') != 'sequential'
            or not group.get('group_uid')
            or group.get('work_uid') != state.get('work_uuid')
            or group.get('project_uid') != state.get('project_id')
            or not group.get('workspace_root') or not parent.get('workspace_root')
            or Path(group['workspace_root']).resolve() != Path(parent['workspace_root']).resolve()
            or group.get('branch') != parent.get('branch')):
        return None
    root = Path(group['workspace_root']).resolve()
    if any((task.get('topology') or {}).get('workspace_root')
           and Path(task['topology']['workspace_root']).resolve() != root
           for task in state.get('tasks') or []):
        return None
    if any((task.get('topology') or {}).get('branch') not in (None, group['branch'])
           for task in state.get('tasks') or []):
        return None
    return group


def parallel_required(state):
    if _sequential_parent_group(state):
        return False
    tasks = state.get('tasks') or []
    roles = {t.get('role', 'implementation') for t in tasks}
    intent = (state.get('git_topology') or {}).get('intent', 'single-task')
    return (intent in {'parallel-multi-task', 'full-stack'} or
            (intent == 'cross-session' and len([t for t in tasks if t.get('role') != 'observer']) > 1) or
            bool({'frontend', 'backend', 'integration'} & roles))


def integration_dependencies(state, integration):
    """Return all transitive task dependencies for an integration task."""
    by_id = {str(task.get('id')): task for task in state.get('tasks') or []}
    result = set()
    pending = list(integration.get('depends') or [])
    while pending:
        task_id = pending.pop()
        if task_id in result:
            continue
        task = by_id.get(task_id)
        if task is None:
            continue
        result.add(task_id)
        pending.extend(task.get('depends') or [])
    return result


def validate_parallel_bindings(state):
    """Validate task-level writable workspace bindings for a parent run.

    The function is intentionally pure: it reports blockers and never creates
    branches, changes task state, or merges child work.  Read-only observers
    may omit a topology; writable parallel roles must claim a unique branch
    and workspace.
    """
    tasks = list(state.get('tasks') or [])
    roles = {str(task.get('role') or 'implementation') for task in tasks}
    intent = str((state.get('git_topology') or {}).get('intent') or 'single-task')
    parallel = parallel_required(state)
    writable_roles = {'implementation', 'frontend', 'backend', 'integration'}
    blocking = []
    integration = [task for task in tasks if task.get('role') == 'integration']
    if parallel and not integration:
        blocking.append({'code': 'GIT_INTEGRATION_REQUIRED', 'detail': 'parallel or full-stack work requires one integration task'})
    if len(integration) > 1:
        blocking.append({'code': 'GIT_INTEGRATION_OWNER_CONFLICT', 'detail': 'only one integration task may own the parent merge point'})

    workspaces = {}
    branches = {}
    for task in tasks:
        role = str(task.get('role') or 'implementation')
        if role not in writable_roles:
            continue
        topology = task.get('topology') or {}
        effective = topology.get('effective_mode')
        if parallel and not effective:
            blocking.append({'code': 'GIT_TASK_TOPOLOGY_REQUIRED', 'task': task.get('id'),
                             'detail': 'each writable parallel task needs its own topology binding'})
            continue
        if not effective:
            continue
        workspace = topology.get('workspace_root')
        branch = topology.get('branch')
        if workspace:
            previous = workspaces.get(str(workspace))
            if previous and previous != task.get('id'):
                if not _sequential_parent_group(state):
                    blocking.append({'code': 'GIT_WORKTREE_CONFLICT', 'task': task.get('id'),
                                     'detail': f'{workspace} is claimed by {previous} and {task.get("id")}'})
            else:
                workspaces[str(workspace)] = task.get('id')
        if branch:
            previous = branches.get(str(branch))
            if previous and previous != task.get('id'):
                if not _sequential_parent_group(state):
                    blocking.append({'code': 'GIT_BRANCH_CONFLICT', 'task': task.get('id'),
                                     'detail': f'{branch} is claimed by {previous} and {task.get("id")}'})
            else:
                branches[str(branch)] = task.get('id')

    if parallel and integration:
        integration_task = integration[0]
        integration_id = integration_task.get('id')
        child_ids = {task.get('id') for task in tasks if task.get('id') != integration_id}
        missing_dependencies = sorted(child_ids - integration_dependencies(state, integration_task))
        if missing_dependencies:
            blocking.append({'code': 'GIT_INTEGRATION_DEPENDENCY_MISSING', 'task': integration_id,
                             'detail': ', '.join(missing_dependencies)})
        pending = [task.get('id') for task in tasks if task.get('id') != integration_id and task.get('status') != 'done']
        if pending:
            blocking.append({'code': 'GIT_CHILD_TASKS_PENDING', 'tasks': pending,
                             'detail': 'all frontend/backend child tasks must finish before integration'})
        if integration_task.get('status') != 'done':
            blocking.append({'code': 'GIT_INTEGRATION_PENDING', 'task': integration_id,
                             'detail': 'integration task must finish before the parent is ready'})
    return {
        'valid': not blocking,
        'parallel': parallel,
        'integration_task': integration[0].get('id') if integration else None,
        'writable_tasks': [task.get('id') for task in tasks if str(task.get('role') or 'implementation') in writable_roles],
        'blocking': blocking,
    }


def commit_number_warnings(root, previous_head, current_head, task_number):
    """Report missing task numbers without blocking HEAD reconciliation."""
    if not previous_head or not current_head or previous_head == current_head or not task_number:
        return []
    try:
        result = subprocess.run(
            ['git', 'log', '--no-merges', '--format=%H%x00%B%x00', f'{previous_head}..{current_head}'],
            cwd=root, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        if result.returncode != 0:
            return [{'level': 'info', 'code': 'GIT_COMMIT_NUMBER_UNCHECKED', 'detail': result.stderr.strip() or 'git log failed'}]
        chunks = [part for part in result.stdout.split('\x00') if part]
        warnings = []
        for index in range(0, len(chunks), 2):
            commit = chunks[index]
            body = chunks[index + 1] if index + 1 < len(chunks) else ''
            if task_number not in body:
                warnings.append({'level': 'warning', 'code': 'GIT_COMMIT_TASK_NUMBER_MISSING', 'commit': commit, 'task': task_number})
        return warnings
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [{'level': 'info', 'code': 'GIT_COMMIT_NUMBER_UNCHECKED', 'detail': type(exc).__name__}]


SKIP_DIRS = {'.git', '.gsp', '.agents', '.claude', '__pycache__', 'node_modules', '.venv', 'venv', '.pytest_cache'}


def snapshot(root, deadline=None, progress=None):
    from .verification_inputs import scan
    return scan(root, deadline=deadline, progress=progress)


def fingerprint(root):
    root = Path(root).resolve()
    return cached_value('fingerprint', str(root),
                        lambda: sha(json.dumps(snapshot(root), sort_keys=True).encode()))


def _identity_at(root):
    if not inside(root, Path(root) / '.gsp') or not inside(root, Path(root) / 'docs/gsp'):
        raise WorkflowError('PROJECT_PATH_ESCAPE workflow storage resolves outside project')
    ident = read(Path(root) / '.gsp/project.json')
    if ident.get('root') != str(root):
        raise WorkflowError('PROJECT_MOVED revalidate project identity before migration')
    return ident


def _ledger_state_at(root, task_id):
    slug(task_id)
    ident = _identity_at(root)
    state_file = Path(root) / '.gsp/runs' / task_id / 'state.json'
    if not state_file.is_file():
        raise WorkflowError('INVALID_STATE ' + str(state_file))
    state = read(state_file)
    if state.get('project_id') != ident['id'] or state.get('root') != str(root):
        raise WorkflowError('PROJECT_MISMATCH refusing cross-project continuation')
    return state


def _brief_control_root(raw_root):
    """Resolve a ledger location from filesystem registration; grant no writes."""
    invocation = Path(raw_root).resolve()
    marker = invocation / '.git'
    if not marker.is_file():
        return invocation
    try:
        text = marker.read_text(encoding='utf-8').strip()
        if not text.startswith('gitdir: '):
            raise ValueError('invalid Git marker')
        gitdir = Path(text[8:].strip())
        gitdir = (gitdir if gitdir.is_absolute() else invocation / gitdir).resolve()
        common_file = gitdir / 'commondir'
        if not common_file.is_file():
            return invocation  # Separate Git dir; only an existing local ledger is read.
        common = Path(common_file.read_text(encoding='utf-8').strip())
        common = (common if common.is_absolute() else gitdir / common).resolve()
        backlink = Path((gitdir / 'gitdir').read_text(encoding='utf-8').strip())
        backlink = (backlink if backlink.is_absolute() else gitdir / backlink).resolve()
        if (gitdir.parent != common / 'worktrees' or common.name.lower() != '.git'
                or not common.is_dir() or backlink != marker.resolve()):
            raise ValueError('unproven linked registration')
        from .identity_store import IdentityStore
        modern = [invocation / '.gsp/project.json',
                  invocation / '.gsp/orchestration/registry.sqlite',
                  invocation / '.gsp/orchestration/registry.json']
        if any(path.exists() for path in modern) or IdentityStore(invocation).local_allocator_surfaces():
            raise WorkflowError('CONTROL_ROOT_SPLIT_AUTHORITY linked worktree has independent allocator state')
        primary = common.parent
        if (primary / '.git').resolve() != common:
            raise ValueError('unproven primary')
        return primary
    except (OSError, ValueError) as exc:
        raise WorkflowError('CONTROL_ROOT_TOPOLOGY_UNSUPPORTED cannot locate linked ledger') from exc


def _format_brief_state(state):
    inflight = state.get('inflight')
    return {
        'task': state.get('id'),
        'number': state.get('number'),
        'recorded_status': state.get('status'),
        'validation': 'NOT_CHECKED',
        'not_authoritative': True,
        'brief': True,
        'updated': state.get('updated'),
        'tasks': [{'id': task.get('id'), 'status': task.get('status')}
                  for task in state.get('tasks') or []],
        'inflight': ({'id': inflight.get('id'), 'task': inflight.get('task'),
                      'phase': inflight.get('phase'), 'at': inflight.get('at')}
                     if isinstance(inflight, dict) else None),
        'evidence_count': len(state.get('evidence') or []),
        'validation_note': 'ledger snapshot only; run status without --brief for authoritative checks',
    }


def test_count(output):
    from .test_results import cargo_summary
    cargo=cargo_summary(output)
    if cargo is not None:return cargo['passed']+cargo['failed']
    counts = []
    for pattern in (r'Ran\s+(\d+)\s+tests?\b', r'(\d+)\s+passed\b', r'(\d+)\s+pass\b',
                    r'#\s*tests\s+(\d+)', r'Tests:\s+(\d+)\s+passed', r'(\d+)\s+tests?\s+passed'):
        counts.extend(int(n) for n in re.findall(pattern, output, re.I))
    return 0 if 0 in counts else (max(counts) if counts else None)


def contract_hash(project, state, doc):
    key = (str(Path(doc).resolve()), project.task_definition_hash(state))
    return cached_value('contract', key, lambda: sha(json.dumps(
        {'design': digest(project.document(state, 'design')),
         'plan': digest(project.document(state, 'plan')),
         'tasks': key[1]}, sort_keys=True).encode()))


def command_input_hashes(root, argv):
    """Bind directly named local script entrypoints, including ignored .gsp files."""
    root=Path(root).resolve();result={}
    for arg in argv:
        if not isinstance(arg,str) or arg.startswith('-') or '\n' in arg:continue
        path=root/arg
        if path.suffix.lower() not in ('.py','.ps1','.sh','.cmd','.bat','.js','.mjs'):continue
        if path.is_file() and inside(root,path):
            if path.is_symlink():raise WorkflowError('COMMAND_INPUT_LINK_REFUSED')
            result[path.resolve().relative_to(root).as_posix()]=digest(path)
    return result


def verify_command_inputs(root, receipt):
    for rel,expected in receipt.get('command_inputs',{}).items():
        path=Path(root)/rel
        if not inside(root,path) or not path.is_file() or path.is_symlink() or digest(path)!=expected:
            raise WorkflowError('COMMAND_INPUT_CHANGED '+rel)


def _path_key(value):
    if value is None:
        return None
    try:
        return os.path.normcase(str(Path(value).resolve()))
    except OSError:
        return os.path.normcase(str(value))


def evidence_entry(row):
    """Stable identity for one command entry; verdicts never cross entries."""
    payload = {'kind': row.get('kind'), 'argv': [str(arg) for arg in row.get('argv') or []]}
    return sha(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode())


def select_evidence(rows, query):
    """Select the effective pass per entry without hiding applicable failures.

    Rows are ordered oldest to newest by the caller. A failure blocks its own
    entry until a later pass for the same entry appears; passes from another
    kind or command entry cannot clear it. Invalid rows remain in the ledger
    but are reported instead of silently reused.
    """
    query = dict(query or {})
    task = query.get('task')
    expected_workspace = _path_key(query.get('workspace_root'))
    contract = query.get('contract')
    fingerprint = query.get('fingerprint')
    epoch = int(query.get('verification_epoch') or 0)
    expected_entry = query.get('entry')
    requested_kind = query.get('kind')
    allow_historical = bool(query.get('allow_historical'))
    invalid_by_id = dict(query.get('command_inputs_invalid') or {})
    groups = {'tests': {}, 'check': {}}
    invalid = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if task is not None and row.get('task') != task:
            continue
        kind = row.get('kind')
        if kind not in groups:
            invalid.append({'id': row.get('id'), 'task': row.get('task'), 'kind': kind,
                            'entry': None, 'reason': 'unsupported-kind',
                            'detail': None, 'at': row.get('at')})
            continue
        if requested_kind and kind != requested_kind:
            continue
        entry = evidence_entry(row)
        reason = None
        detail = None
        if (expected_workspace and row.get('workspace_root') and
                _path_key(row.get('workspace_root')) != expected_workspace):
            reason = 'workspace-changed'
            detail = str(row.get('workspace_root'))
        elif contract is not None and row.get('contract') != contract:
            reason = 'contract-changed'
        elif (not allow_historical and fingerprint is not None and
              row.get('fingerprint') != fingerprint):
            reason = 'input-changed'
        elif (not allow_historical and
              int(row.get('verification_epoch') or 0) < epoch):
            reason = 'before-recovery'
        elif expected_entry and entry != expected_entry:
            reason = 'entry-changed'
        elif row.get('id') in invalid_by_id:
            reason = 'command-input-changed'
            detail = str(invalid_by_id[row.get('id')])
        if reason:
            invalid.append({'id': row.get('id'), 'task': row.get('task'), 'kind': kind,
                            'entry': entry, 'reason': reason, 'detail': detail,
                            'at': row.get('at')})
            continue
        groups[kind].setdefault(entry, []).append((index, row))
    effective = {'tests': [], 'check': []}
    entries = {'tests': {}, 'check': {}}
    blocking = []
    for kind in ('tests', 'check'):
        passes = []
        for entry, items in groups[kind].items():
            index, row = items[-1]
            entries[kind][entry] = row
            if row.get('status') == 'pass':
                passes.append((index, row))
            else:
                blocking.append({'index': index, 'id': row.get('id'), 'task': row.get('task'),
                                 'kind': kind, 'entry': entry, 'status': row.get('status'),
                                 'at': row.get('at')})
        effective[kind] = [row for _, row in sorted(passes, key=lambda item: item[0])]
    blocking.sort(key=lambda row: row['index'])
    for row in blocking:
        row.pop('index', None)
    return {'tests': effective['tests'], 'check': effective['check'],
            'entries': entries, 'blocking_failures': blocking,
            'invalid_reasons': invalid}


def _evidence_folder(project, state, receipt):
    relative = next((path for path in state['evidence']
                     if path.endswith('/' + receipt['id'] + '/receipt.json')), None)
    if not relative:
        raise WorkflowError('EVIDENCE_PATH_UNKNOWN ' + str(receipt.get('id')))
    return project.root / relative


def _raise_selection_error(task_id, selection):
    reasons = {row['reason'] for row in selection['invalid_reasons']}
    if 'workspace-changed' in reasons:
        raise WorkflowError('EVIDENCE_WORKSPACE_CHANGED ' + task_id)
    if 'contract-changed' in reasons:
        raise WorkflowError('EVIDENCE_CONTRACT_CHANGED ' + task_id)
    if 'before-recovery' in reasons:
        raise WorkflowError('EVIDENCE_BEFORE_RECOVERY ' + task_id)
    if 'command-input-changed' in reasons:
        detail = next(row.get('detail') for row in selection['invalid_reasons']
                      if row['reason'] == 'command-input-changed')
        raise WorkflowError('COMMAND_INPUT_CHANGED ' + (detail or task_id))
    if 'entry-changed' in reasons:
        raise WorkflowError('EVIDENCE_ENTRY_CHANGED ' + task_id)
    raise WorkflowError('EVIDENCE_STALE_OR_FAILED ' + task_id)


def head_observation_change(previous, current, validation):
    """Classify one observe-head observation without trusting tree hashes alone."""
    previous = dict(previous or {})
    current = dict(current or {})
    validation = dict(validation or {})
    binding_changed = any(previous.get(key) != current.get(key)
                          for key in ('root', 'branch', 'base_head'))
    head_changed = previous.get('head') != current.get('head')
    identity_changed = binding_changed or head_changed
    checks = dict(validation.get('checks') or {})
    failed = sorted(name for name, ok in checks.items() if not ok)
    verification_changed = bool(binding_changed or failed or
                                validation.get('inputs_changed') or
                                validation.get('verification_changed'))
    if not identity_changed and not verification_changed:
        reason = 'no-change'
    elif identity_changed and not verification_changed:
        reason = 'identity-changed-content-preserved'
    elif binding_changed:
        reason = 'binding-changed' + (' ' + ','.join(failed) if failed else '')
    else:
        reason = 'verification-changed' + (' ' + ','.join(failed) if failed else '')
    return {
        'identity_changed': identity_changed,
        'verification_changed': verification_changed,
        'preserve_content_evidence': not verification_changed,
        'reason': reason,
        'details': dict(validation.get('details') or {}),
    }


def _invoke_check_runner(runner, argv, timeout, *, source_root, env,
                         build_context, on_started):
    """Pass execution context only when the runner's signature accepts it."""
    values = {
        'source_root': source_root,
        'env': env,
        'build_context': build_context,
        'on_started': on_started,
    }
    try:
        parameters = inspect.signature(runner).parameters
    except (TypeError, ValueError):
        supported = {}
    else:
        if any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            supported = values
        else:
            supported = {
                name: value for name, value in values.items()
                if name in parameters and parameters[name].kind in {
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                }
            }
    return runner(argv, timeout, **supported)


def _execution_control_guard(project, state, evidence_id):
    """Freeze the authoritative ledger generation around a child process."""
    run, _ = project.paths(state['id'])
    state_file = run / 'state.json'
    try:
        state_sha = digest(state_file)
    except FileNotFoundError:
        state_sha = None
    return {
        'state_file': state_file,
        'state_sha256': state_sha,
        'work_uuid': state.get('work_uuid'),
        'controller_generation': state.get('controller_generation'),
        'cancel_generation': state.get('cancel_generation'),
        'evidence_id': evidence_id,
    }


def _assert_execution_control(guard):
    """Fail closed before publishing if control changed while unlocked."""
    if guard['state_sha256'] is None:
        return
    state_file = guard['state_file']
    try:
        current = read(state_file)
        current_sha = digest(state_file)
    except (OSError, WorkflowError) as exc:
        raise WorkflowError(
            'CONTROL_CHANGED authoritative ledger unavailable after execution; '
            'preserve inflight evidence and inspect the recorded child'
        ) from exc
    inflight = current.get('inflight') or {}
    changed = []
    if current_sha != guard['state_sha256']:
        changed.append('state')
    for name in ('work_uuid', 'controller_generation', 'cancel_generation'):
        if current.get(name) != guard[name]:
            changed.append(name)
    if (
        inflight.get('id') != guard['evidence_id']
        or inflight.get('phase') not in {'command', 'post-scan'}
    ):
        changed.append('inflight')
    if changed:
        raise WorkflowError(
            'CONTROL_CHANGED ' + ','.join(changed)
            + ' during execution; preserve inflight evidence and inspect the recorded child'
        )


def _raise_for_unknown_execution(execution, folder, *, allow_truncated=False):
    """Keep inflight and owned lease evidence for any non-terminal child."""
    status = execution.get('status')
    interruption = execution.get('interruption')
    if execution.get('process_alive'):
        raise WorkflowError(
            'CHECK_EXECUTION_UNKNOWN process identity is still alive; '
            f'preserve inflight evidence {folder.name}'
        )
    if interruption == 'timeout' or status == 'timeout':
        raise WorkflowError(
            'CHECK_TIMEOUT outcome unknown; inspect partial changes and preserved inflight'
        )
    if interruption == 'cancelled' or status == 'cancelled':
        raise WorkflowError(
            execution.get('cancel_reason')
            or 'STOP_REQUESTED check terminated; inspect partial changes and preserved inflight'
        )
    if interruption or status == 'interrupted':
        raise WorkflowError(
            'CHECK_EXECUTION_UNKNOWN child did not reach a stable terminal state; '
            f'preserve inflight evidence {folder.name}'
        )
    integrity = execution.get('log_integrity') or {}
    if not allow_truncated and any((value or {}).get('truncated') for value in integrity.values()):
        raise WorkflowError(
            'CHECK_LOG_TRUNCATED outcome unknown; preserve inflight and inspect full process evidence'
        )


def run_check(project, state, run, args):
    from .gate_rules import guard_inputs
    guard_inputs(project,state,args.task,binding=getattr(args,'_gsp_binding',None),phase='execute')
    if topology_required(state) and not topology_selected(state):
        raise WorkflowError('GIT_TOPOLOGY_REQUIRED choose and record in-place, branch or worktree before running checks')
    if state.get('inflight'):
        raise WorkflowError('EXECUTION_UNKNOWN reconcile the previous command before rerunning')
    if args.task == 'all':
        if not state['tasks'] or any(t['status'] != 'done' for t in state['tasks']):
            raise WorkflowError('TASKS_INCOMPLETE final regression requires all tasks done')
    elif project.item(state, args.task)['status'] != 'running':
        raise WorkflowError('TASK_NOT_RUNNING start the task first')
    source_root = (project.verification_root(state) if args.task == 'all' else
                   project.workspace_root(state, task_id=args.task))
    if args.task == 'all' or project.item(state, args.task).get('role') == 'integration':
        project.validate_integration(state)
    argv = list(args.argv)
    if argv and argv[0] == '--':
        argv.pop(0)
    if not argv:
        raise WorkflowError('COMMAND_REQUIRED use -- followed by executable and individual arguments')
    if args.timeout <= 0:
        raise WorkflowError('INVALID_TIMEOUT must be positive')
    scan_timeout = getattr(args, 'scan_timeout', 120)
    if scan_timeout <= 0:
        raise WorkflowError('INVALID_SCAN_TIMEOUT must be positive')
    start = time.monotonic()
    deadline = start + args.timeout
    from .build_policy import (isolation_env, load_policy, reclaim_new_entries,
                               remove_tree, snapshot_names, tree_size)
    from .build_context import build_lease, command_environment, resolve_context
    policy = load_policy(project.root)
    build_context = resolve_context(project, state, args.task, argv, os.environ,
                                    source_root=source_root)
    from .resource_lifecycle import register_build_resource
    build_resource = register_build_resource(project, state, args.task, build_context)
    evidence_id = uuid.uuid4().hex
    folder = run / 'evidence' / evidence_id
    folder.mkdir(parents=True, exist_ok=False)
    isolation = None
    if policy['temp_isolation']:
        isolation = project.meta / 'tmp' / 'runs' / evidence_id
        isolation.mkdir(parents=True, exist_ok=False)
    temp_before = {rel: snapshot_names(source_root, rel) for rel in policy['temp_roots']}
    state['inflight'] = {'id': evidence_id, 'task': args.task, 'phase':'pre-scan', 'at': now(), 'argv': [redact(a) for a in argv]}
    if args.task == 'all':
        if not state.get('controller_required'):
            state['status'] = 'checking'
        else:
            if state['status'] == 'ready':
                state['status'] = 'execute'
            state['review'] = None
    project.write(state, 'run-start', evidence_id,
                  action_uid=stable_action_uid('run-start', evidence_id))
    launch_error = None
    phase='pre-scan'
    def progress(row):
        atomic(folder/'scan-progress.json', {'phase':phase, **row})
        print(f'GSP_SCAN {phase}: files={row["files"]} bytes={row["bytes"]} elapsed={row["elapsed_s"]}s',file=sys.stderr,flush=True)
    try:
        before = snapshot(source_root, deadline=time.monotonic() + scan_timeout, progress=progress)
        command_inputs=command_input_hashes(source_root,argv)
        remaining=deadline-time.monotonic()
        if remaining<=0:raise WorkflowError('SCAN_TIMEOUT budget expired before command launch')
    except WorkflowError as exc:
        atomic(folder/'scan-error.json',{'phase':phase,'error':str(exc),'command_started':False,'at':now()})
        state.pop('inflight',None)
        state['status']='needs-verification'
        project.write(state,'scan-failed',evidence_id,
                      action_uid=stable_action_uid('scan-failed', evidence_id, 'pre-scan'))
        raise
    command_started = False
    control_guard = None
    execution = None
    reuse_identity = None
    reused_from = None
    lease_context = build_lease(project, build_context, evidence_id)
    try:
        lease = lease_context.__enter__()
        if build_resource is not None:
            from .resource_registry import ResourceRegistry
            ResourceRegistry(project.root).commit(build_resource['request_uid'], build_resource['owner'])
    except WorkflowError:
        state.pop('inflight', None)
        state['status'] = 'needs-verification'
        project.write(state, 'command-blocked', evidence_id,
                      action_uid=stable_action_uid('command-blocked', evidence_id))
        raise
    try:
        state['inflight']['phase']='command'
        project.write(state,'command-start',evidence_id,
                      action_uid=stable_action_uid('command-start', evidence_id))
        command_started = True
        control_guard = _execution_control_guard(project, state, evidence_id)
        runner = getattr(args, 'runner', None)
        remaining=deadline-time.monotonic()
        if remaining<=0:
            atomic(folder/'scan-error.json',{'phase':'pre-command','error':'SCAN_TIMEOUT','command_started':False,'at':now()})
            state.pop('inflight',None);state['status']='needs-verification'
            project.write(state,'scan-failed',evidence_id,
                          action_uid=stable_action_uid('scan-failed', evidence_id, 'pre-command'))
            raise WorkflowError('SCAN_TIMEOUT budget expired before command launch')
        command_env = command_environment(os.environ, build_context,
                                          isolation_env(isolation) if isolation else {})
        interruption = None
        process_identity = None
        log_integrity = None
        if runner:
            with suspend_writer_lock(project.root):
                result = _invoke_check_runner(
                    runner, argv, remaining, source_root=source_root,
                    env=command_env, build_context=build_context,
                    on_started=lease.attach if lease is not None else None,
                )
            stdout, stderr, code = result.stdout, result.stderr, result.returncode
        else:
            from .command_execution import execute_command
            from .check_reuse import declared_run_requirement
            from .evidence import EvidenceCache
            requirement = declared_run_requirement(
                project, state, argv, source_root, command_env,
                sha(json.dumps(before, sort_keys=True).encode()),
                task_id=args.task, kind=args.kind)
            reuse_identity = requirement['identity'] if requirement is not None else None
            cached = EvidenceCache(project.root).select(reuse_identity, timeout=remaining)
            with suspend_writer_lock(project.root):
                execution = cached['execution'] if cached else execute_command(
                    argv, source_root, command_env, remaining, folder,
                    {'task': args.task, 'argv': [redact(a) for a in argv],
                     'build_context': {key: build_context.get(key) for key in
                                       ('mode', 'root', 'provenance', 'compatibility_key')}},
                    on_started=lease.attach if lease is not None else None)
            if cached:
                reused_from = cached['receipt']
                # Retain the published receipt/log layout without rewriting logs.
                for name in ('stdout', 'stderr'):
                    origin = Path(execution[name + '_path'])
                    target = folder / (name + '.txt')
                    try:
                        os.link(origin, target)
                    except OSError:
                        shutil.copyfile(origin, target)
            _assert_execution_control(control_guard)
            # A terminal process with incomplete output gets an explicit
            # failing receipt below. It never supplies passing evidence.
            _raise_for_unknown_execution(execution, folder, allow_truncated=True)
            code = execution['returncode']
            launch_error = execution['launch_error']
            interruption = execution.get('interruption')
            process_identity = execution.get('process_identity')
            log_integrity = execution.get('log_integrity')
            stdout = Path(execution['stdout_path']).read_text(encoding='utf-8', errors='replace')
            stderr = Path(execution['stderr_path']).read_text(encoding='utf-8', errors='replace')
        _assert_execution_control(control_guard)
    except (OSError, subprocess.TimeoutExpired) as exc:
        stdout, stderr, code = '', str(exc), None
        launch_error = type(exc).__name__
        if control_guard is not None:
            _assert_execution_control(control_guard)
        raise WorkflowError(
            'CHECK_TIMEOUT outcome unknown; inspect partial changes and preserved inflight'
            if isinstance(exc, subprocess.TimeoutExpired)
            else 'CHECK_EXECUTION_UNKNOWN child launch failed; inspect inflight evidence'
        ) from exc
    finally:
        lease_context.__exit__(None, None, None)
    # A tested command may commit, rewrite generated files or change the build
    # environment. Never reuse pre-command identity or digest values.
    invalidate('command-finished')
    if runner:
        atomic(folder/'command-result.json',{'exit':code,'launch_error':launch_error,'at':now()})
    hygiene = {'temp_isolation_dir': str(isolation) if isolation else None,
               'disabled': not policy['temp_isolation'],
               'build_mode': build_context.get('mode'),
               'build_root': build_context.get('root'),
               'created': 0, 'removed': 0, 'retained': 0, 'retained_bytes': 0,
               'truncated': False, 'over_budget': False, 'errors': []}
    command_succeeded = code == 0 and launch_error is None
    for rel in policy['temp_roots']:
        row = reclaim_new_entries(source_root, rel, temp_before.get(rel, {}),
                                  retain=policy['retain_on_failure'],
                                  success=command_succeeded)
        for key in ('created', 'removed', 'retained', 'retained_bytes'):
            hygiene[key] += row[key]
        hygiene['truncated'] = hygiene['truncated'] or row['truncated']
        hygiene['errors'].extend(row['errors'])
    if isolation is not None:
        try:
            remove_tree(isolation, project.root)
        except OSError as exc:
            hygiene['errors'].append({'name': str(isolation),
                                      'code': 'ISOLATION_REMOVE_FAILED',
                                      'detail': str(exc)})
    if policy['limits']['max_temp_entries'] > 0:
        remaining = sum(len(snapshot_names(source_root, rel)) for rel in policy['temp_roots'])
        hygiene['over_budget'] = remaining > policy['limits']['max_temp_entries']
    if policy['limits']['max_build_bytes'] > 0:
        build_base = project.meta / 'build' / str(state.get('number') or '')
        if tree_size(build_base) > policy['limits']['max_build_bytes']:
            hygiene['over_budget'] = True
    phase='post-scan'
    state['inflight']['phase']=phase
    project.write(state,'post-scan',evidence_id,
                  action_uid=stable_action_uid('post-scan', evidence_id))
    try:
        after = snapshot(source_root, deadline=time.monotonic() + scan_timeout, progress=progress)
    except WorkflowError as exc:
        atomic(folder/'scan-error.json',{'phase':phase,'error':str(exc),'command_started':True,'exit':code,'at':now()})
        # Preserve inflight: a real command ran but its side effects are unverified.
        project.write(state,'scan-failed',evidence_id,
                      action_uid=stable_action_uid('scan-failed', evidence_id, 'post-scan'))
        raise
    changes = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    from .test_results import cargo_summary
    summary=cargo_summary(stdout+'\n'+stderr) if args.kind=='tests' else None
    count = test_count(stdout + '\n' + stderr) if args.kind == 'tests' else None
    status = 'pass' if code == 0 else 'fail'
    if launch_error:
        status = 'not-run'
    elif code != 0:
        status = 'fail'
    elif summary and summary['failed']:
        status = 'failed-output'
    elif args.kind == 'tests' and count == 0:
        status = 'zero-tests'
    elif args.kind == 'tests' and count is None:
        status = 'test-count-unknown'
    elif args.kind == 'tests' and re.search(r'(?im)^FAILED\b|^\(fail\)|^#\s*fail\s+[1-9]|\b[1-9]\d*\s+failed\b', stdout + '\n' + stderr):
        status = 'failed-output'
    log_truncated = bool((log_integrity or {}).get('stdout', {}).get('truncated') or
                         (log_integrity or {}).get('stderr', {}).get('truncated'))
    if log_truncated and status == 'pass':
        status = 'log-truncated'
    command_inputs_changed=command_input_hashes(source_root,argv)!=command_inputs
    if (before != after or command_inputs_changed) and status == 'pass':
        status = 'inputs-changed'
    if runner:
        atomic(folder / 'stdout.txt', redact(stdout))
        atomic(folder / 'stderr.txt', redact(stderr))
    receipt = {'schema': 1, 'id': evidence_id, 'task': args.task, 'kind': args.kind, 'at': now(),
               'argv': [redact(a) for a in argv], 'exit': code, 'launch_error': launch_error,
               'interruption': interruption, 'process_identity': process_identity,
               'log_integrity': log_integrity,
               'execution_identity': reuse_identity,
               'execution_result': (str(Path(execution['folder']) / 'command-result.json')
                                    if execution is not None else None),
               'reused': reused_from is not None, 'reused_from': reused_from,
               'duration_s': round(time.monotonic() - start, 3), 'status': status, 'tests_ran': count, 'test_summary':summary,
                'fingerprint': sha(json.dumps(after, sort_keys=True).encode()), 'changed_paths': changes, 'scope_violations': [], 'command_inputs':command_inputs,
               'contract': contract_hash(project, state, project.paths(state['id'])[1]),
               'workspace_root': str(source_root), 'git': git_identity(source_root),
               'verification_epoch': state.get('verification_epoch', 0),
               'build_context': build_context,
               'hygiene': hygiene,
               'logs': {name: digest(folder / name) for name in ('stdout.txt', 'stderr.txt')}}
    atomic(folder / 'receipt.json', receipt)
    if reuse_identity is not None and reused_from is None:
        from .evidence import EvidenceCache
        if not EvidenceCache(project.root).publish(reuse_identity, folder / 'receipt.json'):
            raise WorkflowError('QUALITY_EVIDENCE_PERSISTENCE_UNKNOWN recover the original inflight receipt')
    state['evidence'].append((folder / 'receipt.json').relative_to(project.root).as_posix())
    state.pop('inflight', None)
    project.write(state, 'check', f'{evidence_id} {status}',
                  action_uid=stable_action_uid('check', evidence_id, status))
    if args.task == 'all' and status == 'pass':
        from .dispatch import finalize_integration_cleanup
        if getattr(args,'_gsp_binding',None) is not None:
            finalize_integration_cleanup(project,state,receipt,binding=args._gsp_binding)
        else:
            finalize_integration_cleanup(project, state, receipt)
    return receipt, status == 'pass'


def _evidence_for_uncached(project, state, task_id, current_fingerprint=None, current_contract=None,
                           allow_historical=False, allow_missing_workspace=False, defer_no_tests=False,
                           kind=None, entry=None, allow_head_change=False, context=None):
    all_rows = [read(project.root / p) for p in state['evidence']]
    # A whole-project `all` receipt is the final regression lane, not the
    # task's own evidence. Mixing it into this lookup would let it satisfy
    # task completion or hide the task's own tests.
    rows = [r for r in all_rows if r.get('task') == task_id]
    if not rows:
        raise WorkflowError('EVIDENCE_MISSING ' + task_id)
    try:
        source_root = (project.verification_root(state, allow_head_change=allow_head_change)
                       if task_id == 'all'
                       else project.workspace_root(state, task_id=task_id,
                                                   allow_head_change=allow_head_change))
    except WorkflowError:
        if not allow_missing_workspace:
            raise
        source_root = (project.verification_root(state, allow_head_change=True) if task_id == 'all'
                       else project.workspace_root(state, validate=False, task_id=task_id))
    source_exists = source_root.is_dir()
    if not source_exists and not allow_missing_workspace:
        raise WorkflowError('GIT_WORKSPACE_MISSING bound source workspace does not exist')
    contract = current_contract or contract_hash(project, state, project.paths(state['id'])[1])
    expected_fingerprint = current_fingerprint
    if expected_fingerprint is None and not allow_historical:
        if not source_exists:
            raise WorkflowError('GIT_WORKSPACE_MISSING bound source workspace does not exist')
        expected_fingerprint = fingerprint(source_root)
    input_invalid = {}
    if source_exists:
        for row in rows:
            if row.get('command_inputs'):
                try:
                    verify_command_inputs(source_root, row)
                except WorkflowError as exc:
                    input_invalid[row.get('id')] = str(exc)
    selection = select_evidence(rows, {
        'task': task_id, 'kind': kind, 'entry': entry,
        'workspace_root': str(source_root), 'fingerprint': expected_fingerprint,
        'contract': contract, 'verification_epoch': state.get('verification_epoch', 0),
        'allow_historical': allow_historical,
        'command_inputs_invalid': input_invalid,
    })
    if selection['blocking_failures']:
        failure = selection['blocking_failures'][0]
        extra = len(selection['blocking_failures']) - 1
        suffix = f' (+{extra} more)' if extra else ''
        raise WorkflowError('EVIDENCE_APPLICABLE_FAILURE ' +
                            f"{task_id} {failure['kind']}:{failure['entry']} " +
                            f"{failure['status']}{suffix}")
    candidates = selection[kind] if kind else selection['tests'] + selection['check']
    if not candidates:
        _raise_selection_error(task_id, selection)
    primary = (selection[kind][-1] if kind else
               (selection['tests'][-1] if selection['tests'] else selection['check'][-1]))
    for row in selection['tests'] + selection['check']:
        folder = _evidence_folder(project, state, row)
        logs = row.get('logs')
        if not isinstance(logs, dict):
            raise WorkflowError('EVIDENCE_INCOMPLETE ' + str(row.get('id')))
        for name, expected in logs.items():
            try:
                actual = digest(folder.parent / name)
            except OSError as exc:
                raise WorkflowError('EVIDENCE_TAMPERED ' + name) from exc
            if actual != expected:
                raise WorkflowError('EVIDENCE_TAMPERED ' + name)
    no_tests = all(t.get('no_tests_reason') for t in state['tasks']) if task_id == 'all' else project.item(state, task_id).get('no_tests_reason')
    if primary['kind'] == 'check' and not no_tests and not defer_no_tests:
        raise WorkflowError('TESTS_REQUIRED supply an explicit task-specific no-tests reason for non-test changes')
    return primary


def evidence_for(project, state, task_id, current_fingerprint=None, current_contract=None,
                 allow_historical=False, allow_missing_workspace=False, defer_no_tests=False,
                 kind=None, entry=None, allow_head_change=False, context=None):
    """Select one effective receipt, deduplicated inside the active command phase."""
    active = context if context is not None else current_context()
    if active is None:
        return _evidence_for_uncached(
            project, state, task_id, current_fingerprint, current_contract,
            allow_historical, allow_missing_workspace, defer_no_tests,
            kind, entry, allow_head_change, None)
    key = ('evidence', task_id, current_fingerprint, current_contract,
           bool(allow_historical), bool(allow_missing_workspace), bool(defer_no_tests),
           kind, entry, bool(allow_head_change), int(state.get('verification_epoch') or 0),
           tuple(state.get('evidence') or []), project.task_definition_hash(state))
    return active.get('evidence', key, lambda: _evidence_for_uncached(
        project, state, task_id, current_fingerprint, current_contract,
        allow_historical, allow_missing_workspace, defer_no_tests,
        kind, entry, allow_head_change, active))


def _validate_delivery_uncached(project, state, doc, allow_head_change=False, context=None):
    """One validation path for the controller and the delivery command."""
    if state.get('inflight'):
        raise WorkflowError('EXECUTION_UNKNOWN reconcile the original execution before delivery')
    if state.get('revalidation'):
        raise WorkflowError('REVALIDATION_PENDING ' + ', '.join(entry['task'] for entry in state['revalidation']))
    parallel = validate_parallel_bindings(state)
    if not parallel['valid']:
        raise WorkflowError(parallel['blocking'][0]['code'] + ' ' + str(parallel['blocking']))
    project.validate_integration(state, allow_head_change=allow_head_change,
                                 context=context, verify_evidence=False)
    from .todos import Backlog
    backlog = Backlog(project.root).summary()
    if not backlog['exists']:
        raise WorkflowError('BACKLOG_MISSING restore the project backlog')
    blockers = [r['id'] for r in backlog['items'] if r['status'] == 'open' and r['blocking'] == 'yes' and r['origin'] == state['id']]
    blockers += [r['id'] for r in backlog['orphaned_records'] if not r['completed'] and r['blocking'] == 'yes' and r['origin'] == state['id']]
    if blockers:
        raise WorkflowError('BACKLOG_BLOCKING unresolved required work: ' + ', '.join(blockers))
    if state.get('inflight'):
        raise WorkflowError('EXECUTION_UNKNOWN')
    if not state['tasks'] or any(t['status'] != 'done' for t in state['tasks']):
        raise WorkflowError('TASKS_INCOMPLETE')
    from .acceptance import acceptance_track_status
    for task in state['tasks']:
        if 'acceptance_cards' in task:
            status = acceptance_track_status(task['acceptance_cards'])
            task.setdefault('acceptance_track', {})['status'] = status
            if status != 'acceptance_verified':
                raise WorkflowError('ACCEPTANCE_PENDING ' + task['id'])
    fp = fingerprint(project.verification_root(state, allow_head_change=allow_head_change))
    contract = contract_hash(project, state, doc)
    try:
        evidence_for(project, state, 'all', current_fingerprint=fp, current_contract=contract,
                     allow_head_change=allow_head_change, context=context)
    except WorkflowError as exc:
        raise WorkflowError('FINAL_REGRESSION_REQUIRED ' + str(exc)) from exc
    from .dispatch import attested_removed_tasks
    attested_ids = attested_removed_tasks(project, state)
    checks = [evidence_for(
        project, state, task['id'], current_contract=contract,
        allow_historical=not bool(task.get('topology')) or task['id'] in attested_ids,
        allow_missing_workspace=task['id'] in attested_ids,
        allow_head_change=allow_head_change,
        context=context,
    ) for task in state['tasks']]
    review = state.get('review') or {}
    if (not review or review.get('blocking') or review.get('fingerprint') != fp or
            review.get('sha256') != digest(doc / 'review.md') or review.get('contract') != contract):
        raise WorkflowError('REVIEW_MISSING_STALE_OR_BLOCKING')
    if state.get('convergence_schema') == 1 and (review.get('independent_proof') or {}).get('independence') != 'verified-context':
        raise WorkflowError('INDEPENDENT_REVIEW_REQUIRED current review has no separate verified reviewer')
    from .code_quality import evaluate_required_quality, load_stack
    quality_source = project.verification_root(state, allow_head_change=allow_head_change)
    quality_stack = load_stack(project.root, source_root=quality_source)
    if any(row['required'] and 'release' in row['modes'] for row in quality_stack['checks']):
        from .quality_workflow import quality_build_context
        _, quality_env = quality_build_context(project, state)
        quality_blockers = evaluate_required_quality(project, state, fp, env=quality_env)
        if quality_blockers:
            first = quality_blockers[0]
            raise WorkflowError(
                str(first.get('code') or 'QUALITY_REQUIRED')
                + (' ' + str(first['detail']) if first.get('detail') else '')
            )
        if _measure_quality_source(quality_source) != fp:
            raise WorkflowError('QUALITY_INPUTS_CHANGED source changed during final quality evaluation')
    return fp, checks, backlog


def validate_delivery(project, state, doc, allow_head_change=False, context=None):
    """Run one full delivery validation per unchanged command phase."""
    from .gate_rules import guard_inputs
    guard_inputs(project,state,phase='accept')
    active = context if context is not None else current_context()
    if active is None:
        return _validate_delivery_uncached(project, state, doc, allow_head_change, None)
    key = ('delivery', str(Path(doc).resolve()), bool(allow_head_change),
           int(state.get('verification_epoch') or 0),
           tuple(state.get('evidence') or []),
           tuple((task.get('id'), task.get('status'), task.get('evidence'))
                 for task in state.get('tasks') or []),
           json.dumps(state.get('review') or {}, sort_keys=True, ensure_ascii=False))
    return active.get('delivery_validation', key, lambda: _validate_delivery_uncached(
        project, state, doc, allow_head_change, active))


def _measure_quality_source(root):
    invalidate('quality-source-measurement')
    return fingerprint(root)


def _independent_review_proof(project, state, text, candidate, contract, caller_binding, task_id=None):
    from .review_evidence import review_metadata, validate_reviewer
    from .session_context import load_binding
    try:
        metadata = review_metadata(text)
        if metadata.get('candidate_digest') != candidate or metadata.get('contract_digest') != contract:
            raise ValueError('REVIEW_STALE independent review must bind the current candidate and contract')
        raw = metadata.get('reviewer_context')
        if not isinstance(raw, str) or not raw:
            raise ValueError('INDEPENDENT_REVIEW_REQUIRED reviewer_context is missing')
        path = (project.root / raw).resolve()
        if not inside(project.root, path) or path.is_symlink():
            raise ValueError('INDEPENDENT_REVIEW_REQUIRED reviewer context is outside this control root')
        binding = load_binding(path, _recover_once=True)
        excluded = {caller_binding.host_thread_id} if caller_binding is not None else set()
        registry = project.meta / 'runs' / state['id'] / 'sessions/index.json'
        if registry.is_file():
            for row in read(registry).get('sessions', []):
                if row.get('role') in {'worker', 'controller', 'integration'} and (
                        task_id is None or row.get('task') in {None, task_id}):
                    excluded.add(row.get('host_thread_id') or row.get('thread'))
        if binding.project_uid != project.identity()['id']:
            raise ValueError('INDEPENDENT_REVIEW_REQUIRED reviewer belongs to another project')
        return validate_reviewer(binding, state, excluded,
            excluded_binding_uid=caller_binding.binding_uid if caller_binding is not None else state.get('controller_binding_uid'))
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc


@contextmanager
def _quality_execution_window(project, state, evidence_id):
    guard = _execution_control_guard(project, state, evidence_id)
    with suspend_writer_lock(project.root):
        yield
    _assert_execution_control(guard)


def _hygiene_release(project, state):
    """Block unexplained leftovers from the latest full regression receipt."""
    receipts = []
    for relative in state.get('evidence', []):
        if not relative.endswith('/receipt.json'):
            continue
        path = project.root / relative
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
            receipts.append((json.loads(raw), sha(raw)))
        except (WorkflowError, OSError, ValueError):
            continue
    final = [(row, content_digest) for row, content_digest in receipts if row.get('task') == 'all']
    if not final:
        return {'required': False, 'reason': 'no final full-regression receipt'}
    receipt, content_digest = final[-1]
    hygiene = receipt.get('hygiene') or {}
    retained = int(hygiene.get('retained') or 0)
    errors = list(hygiene.get('errors') or [])
    truncated = bool(hygiene.get('truncated'))
    if not (retained or errors or truncated):
        return {'required': False, 'reason': 'no unexplained leftovers'}
    detail = f'retained={retained} truncated={truncated} errors={len(errors)}'
    binding = {'schema': 1, 'work_uid': state.get('work_uuid') or state['id'],
               'receipt_id': receipt.get('id'), 'receipt_digest': content_digest,
               'disposition': 'retain'}
    decision = state.get('hygiene_release')
    if (not errors and not truncated and isinstance(decision, dict)
            and all(decision.get(key) == value for key, value in binding.items())
            and isinstance(decision.get('reason'), str) and decision['reason'].strip()):
        return {'required': True, 'accepted': True, 'detail': detail,
                'reason': decision['reason'], 'binding': binding}
    _, doc = project.paths(state['id'])
    decisions = doc / 'decisions.md'
    if decisions.is_file():
        for line in decisions.read_text(encoding='utf-8').splitlines():
            explicit = re.search(r'\[(?:user|agent)\]\s+HYGIENE_LEFTOVER\s+(?:ACCEPT|接受)[:：]?\s+'
                                 r'receipt=(\S+)\s+digest=([0-9a-f]{64})\s+(.+)', line)
            if (not errors and not truncated and explicit
                    and explicit[1] == receipt.get('id') and explicit[2] == content_digest):
                return {'required': True, 'accepted': True, 'detail': detail,
                        'reason': redact(line.strip()), 'binding': binding}
    raise WorkflowError('HYGIENE_LEFTOVER undecided temporary/artifact residue: ' + detail +
                        f' receipt={receipt.get("id")} digest={content_digest}')


def _delivery_git_state(project, state):
    """Record whether the delivered workspace is clean and merged; never blocks."""
    workspace = project.verification_root(state)
    status = _git_run(workspace, 'status', '--porcelain', '-uall')
    dirty = [line[3:].strip() for line in status.stdout.splitlines() if line.strip()] \
        if status.returncode == 0 else ['<status-unavailable>']
    identity = git_identity(workspace)
    base = (state.get('git_topology') or {}).get('base_branch') or 'main'
    merged = bool(identity.get('head')) and is_ancestor(
        workspace, identity['head'], base).returncode == 0
    return {'workspace': str(workspace), 'git_clean': not dirty,
            'dirty_files': dirty[:20], 'base_branch': base,
            'branch_merged': merged}
