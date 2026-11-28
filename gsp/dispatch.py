"""Task dispatch, progress, durable event delivery and cleanup controls."""
from pathlib import Path, PurePosixPath
import os
import re
import uuid

from .core import (WorkflowError, atomic, cached_value, digest, inside, now, read, redact)
from .codex_worker_adapter import run_worker
from .git_operations import (_git_run, git_identity, is_ancestor)
from .hosts import identity
from .workflow_support import (contract_hash, fingerprint, integration_dependencies)
from .orchestration_types import digest_value
from .identity_store import IdentityStore, IdentityStoreError
from .orchestration_types import RequestRef
from .resource_registry import (
    ResourceOwner,
    ResourceRegistry,
    ResourceRegistryError,
)
from .sessions import _save as save_registry
from .sessions import load_registry
from .session_context import require_row_capability
from .task_contracts import compile_task_contract, render_task_contract
from .plan_quality import write_plan_gate
from .worktree_lifecycle import (
    _base,
    _automatic_location,
    build_environment,
    cleanup_worktree,
    inspect_worktree,
    prepare_worktree,
)
from .worktree_adapters import WorktreeAdapter, WorktreeAdapterError

PLATFORMS = {'codex', 'claude', 'zcode', 'manual', 'other'}
DISPATCH_MODES = {'auto', 'adapter', 'manual'}
ACTIVE_DISPATCH = {
    'workspace-pending', 'adapter-ready', 'running', 'reported',
    'dispatched', 'manual-required',
}
CLEANUP_RESULTS = {'archived', 'released', 'removed'}


def _folders(project, state):
    run, doc = project.paths(state['id'])
    machine = run / 'sessions'
    human = doc / 'sessions'
    machine.mkdir(parents=True, exist_ok=True)
    human.mkdir(parents=True, exist_ok=True)
    (human / 'history').mkdir(parents=True, exist_ok=True)
    return machine, human


def _launch_native_dispatch(project, state, data, row, contract, contract_text, timeout):
    """Run one Codex auto dispatch through the same native worker adapter."""
    from .driver import RESULT_SCHEMA

    machine, _ = _folders(project, state)
    attempt = machine / f"{row['id']}-native"
    attempt.mkdir(parents=True, exist_ok=True)
    job = {
        'id': row['id'],
        'task': next(
            item for item in state.get('tasks', [])
            if item.get('id') == row['task']
        ),
        'project': str(project.root),
        'phase': 'execute',
        'job_file': str(attempt / 'job.json'),
        'schema': str(attempt / 'schema.json'),
        'result': str(attempt / 'response.json'),
        'task_contract': contract,
        'model_alias': row['model_alias'],
        'effort': row['effort'],
        'native_resume_id': None,
    }
    atomic(attempt / 'schema.json', RESULT_SCHEMA)
    atomic(attempt / 'job.json', job)
    row.update({
        'status': 'running',
        'worker': None,
        'native_session_id': None,
        'worker_status': None,
        'worker_actual_model': None,
        'worker_usage': None,
        'updated_at': now(),
    })
    _save_dispatch(project, state, data)

    def on_started(process):
        row['worker'] = identity(process.pid)
        row['updated_at'] = now()
        _save_dispatch(project, state, data)

    try:
        worker = run_worker(
            job, Path(row['worktree']), dict(os.environ), timeout,
            attempt, contract_text,
            thread_id=None,
            on_started=on_started,
        )
    except (WorkflowError, ValueError):
        row.update({
            'status': 'dispatch-failed', 'worker_status': 'launch-failed',
            'updated_at': now(),
        })
        _save_dispatch(project, state, data)
        raise

    worker_status = worker.get('status')
    native_id = worker.get('native_session_id')
    if worker_status != 'completed':
        row.update({'status': 'running', 'worker_status': worker_status,
                    'native_session_id': native_id, 'updated_at': now()})
        _save_dispatch(project, state, data)
        raise WorkflowError(
            'NATIVE_DISPATCH_UNKNOWN worker evidence is incomplete; inspect '
            + row['id']
        )
    try:
        native_id = str(uuid.UUID(native_id))
    except (ValueError, TypeError, AttributeError) as exc:
        row.update({'status': 'running', 'worker_status': worker_status,
                    'updated_at': now()})
        _save_dispatch(project, state, data)
        raise WorkflowError(
            'NATIVE_DISPATCH_UNKNOWN completed worker has no exact native identity'
        ) from exc
    if (worker.get('requested_model') != row['model_alias']
            or worker.get('requested_effort') != row['effort']):
        row.update({'status': 'running', 'worker_status': worker_status,
                    'updated_at': now()})
        _save_dispatch(project, state, data)
        raise WorkflowError('WORKER_MODEL_REQUIRED native dispatch model is unverified')

    row.update({
        'status': 'dispatched',
        'native_session_id': native_id,
        'worker_status': worker_status,
        'worker_actual_model': worker.get('actual_model'),
        'worker_usage': worker.get('usage'),
        'updated_at': now(),
    })
    _save_dispatch(project, state, data)
    if row.get('execution_slot_request_uid'):
        ResourceRegistry(project.root).release_execution_slot(
            row['execution_slot_request_uid'],
            row.get('execution_slot_owner'),
            identity=row.get('execution_slot_identity'),
        )


def _default(state):
    return {
        'schema': 1,
        'task': state['id'],
        'next_dispatch': 1,
        'dispatches': [],
        'cleanup': [],
    }


def load_dispatch(project, state):
    machine, _ = _folders(project, state)
    file = machine / 'dispatch.json'
    data = _default(state) if not file.exists() else read(file)
    if data.get('task') != state['id']:
        raise WorkflowError('DISPATCH_REGISTRY_TASK_MISMATCH')
    for key, value in (('schema', 1), ('next_dispatch', 1), ('dispatches', []), ('cleanup', [])):
        data.setdefault(key, value)
    for row in data.get('dispatches', []):
        row.setdefault('managed_worktree', False)
        row.setdefault('worktree_mode', 'bound')
        row.setdefault(
            'worktree_state',
            ('unknown' if row.get('status') == 'preparing' else
             'bound' if row.get('worktree') else 'unknown'),
        )
        row.setdefault('base', None)
        row.setdefault('cleanup_attempts', 0)
        row.setdefault('cleanup_error', None)
        contract = row.get('contract') or row.get('prompt') or row.get('brief')
        if contract:
            row['contract'] = contract
            row.setdefault('brief', contract)
            row.setdefault('prompt', contract)
        row.setdefault('contract_uid', None)
        row.setdefault('content_digest', None)
        row.setdefault('model_alias', 'group/deepseek-2')
        row.setdefault('effort', 'max')
        row.setdefault('resource_request_uid', None)
        row.setdefault('resource_state', None)
        row.setdefault('dispatch_ref', None)
        row.setdefault('execution_slot_request_uid', None)
        row.setdefault('execution_slot_identity', None)
        row.setdefault('execution_slot_owner', None)
        row.setdefault('execution_slot_state', None)
        row.setdefault('execution_slot_released_at', None)
        row.setdefault('execution_slot_released_by', None)
        row.setdefault('execution_slot_release_reason', None)
        row.setdefault('execution_slot_scope', 'project')
        row.setdefault('features', {})
        if isinstance(row.get('features'), dict):
            row['features'].setdefault('multi_agent', False)
    return data


def _save_dispatch(project, state, data):
    machine, _ = _folders(project, state)
    atomic(machine / 'dispatch.json', data)


def _pending_dispatch_intent(data):
    rows = [
        row for row in data.get('dispatches', [])
        if row.get('status') == 'preparing'
    ]
    if not rows:
        return None
    request_uids = {
        row.get('resource_request_uid') for row in rows
    }
    if (
        len(request_uids) != 1
        or not isinstance(next(iter(request_uids)), str)
        or not next(iter(request_uids))
    ):
        raise WorkflowError(
            'DISPATCH_PREPARATION_PENDING multiple prior intents: '
            + ','.join(sorted(str(value) for value in request_uids))
        )
    request_uid = next(iter(request_uids))
    tasks = [row.get('task') for row in rows]
    if (
        any(not isinstance(task, str) or not task for task in tasks)
        or len(set(tasks)) != len(tasks)
    ):
        raise WorkflowError('DISPATCH_PREPARATION_PENDING ' + request_uid)
    tokens = [row.get('scheduler_token') for row in rows]
    try:
        normalized = [
            ResourceRegistry._normalize_scheduler(token) for token in tokens
        ]
    except ResourceRegistryError as exc:
        raise WorkflowError(
            'DISPATCH_SCHEDULER_INTENT_INVALID ' + request_uid
        ) from exc
    if any(token != normalized[0] for token in normalized[1:]):
        raise WorkflowError(
            'DISPATCH_SCHEDULER_INTENT_INVALID ' + request_uid
        )
    return {
        'request_uid': request_uid,
        'tasks': tasks,
        'scheduler_token': normalized[0],
    }


def _assignment(values, label):
    result = {}
    common = []
    for raw in values or []:
        text = str(raw or '').strip()
        if '=' not in text:
            common.append(text)
            continue
        key, value = text.split('=', 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise WorkflowError(f'DISPATCH_{label.upper()}_INVALID use TASK=VALUE')
        if key in result:
            raise WorkflowError(f'DISPATCH_{label.upper()}_DUPLICATE {key}')
        result[key] = value
    if len(common) > 1:
        raise WorkflowError(f'DISPATCH_{label.upper()}_DUPLICATE common value')
    return result, common[0] if common else None


def _task_map(state):
    return {str(task.get('id')): task for task in state.get('tasks', [])}


def _accepted_task_ids(state, data):
    accepted = {
        str(task.get('id')) for task in state.get('tasks', [])
        if task.get('status') in {'done', 'accepted', 'integrated', 'cleaned'}
    }
    for row in data.get('dispatches', []):
        if row.get('status') in {'accepted', 'integrated', 'cleaned'}:
            accepted.add(str(row.get('task')))
    return accepted


def _normalize_path(worktree, raw):
    value = str(raw or '').strip().replace('\\', '/')
    if not value:
        raise WorkflowError('DISPATCH_PATH_REQUIRED')
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            value = candidate.resolve().relative_to(Path(worktree).resolve()).as_posix()
        except ValueError as exc:
            raise WorkflowError('DISPATCH_PATH_ESCAPE ' + value) from exc
    else:
        value = PurePosixPath(value).as_posix()
    value = value.strip('/') or '.'
    if value == '..' or value.startswith('../') or '/..' in value or '\x00' in value:
        raise WorkflowError('DISPATCH_PATH_ESCAPE ' + value)
    return value


def _overlap(left, right):
    return left == '.' or right == '.' or left == right or left.startswith(right + '/') or right.startswith(left + '/')


def _next_id(data):
    number = int(data.get('next_dispatch', 1))
    used = {row.get('id') for row in data.get('dispatches', [])}
    while f'D{number:03d}' in used:
        number += 1
    data['next_dispatch'] = number + 1
    return f'D{number:03d}'


def _next_session_hints(project, state, count, data=None):
    registry = load_registry(project, state)
    used = {row.get('id') for row in registry.get('sessions', [])}
    used.update(row.get('session_hint') for row in (data or {}).get('dispatches', []))
    number = int(registry.get('next_session', 1))
    result = []
    while len(result) < count:
        token = f'S{number:02d}'
        if token not in used:
            result.append(token)
        number += 1
    return result


def _relative(project, path):
    try:
        return path.resolve().relative_to(project.root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _contract_inputs(project, state):
    texts, paths = {}, {}
    for kind in ('design', 'plan'):
        path = project.document(state, kind)
        try:
            texts[kind] = path.read_text(encoding='utf-8')
        except FileNotFoundError:
            texts[kind] = ''
        paths[kind] = _relative(project, path)
    return texts, paths


def _task_contract(project,state,task_id,texts,paths):
    from .contract_store import load_contract
    contract=load_contract(project,state,task_id,required=state.get('operation_schema')==2)
    if contract is not None:
        return contract
    return compile_task_contract(state,task_id,texts['plan'],texts['design'],source_paths=paths)


def _require_dispatch_writes_gate(state, plan, actual_batch):
    facts = write_plan_gate(
        state,
        plan,
        parallel=len(actual_batch) > 1,
    )
    blockers = facts.get('blockers') or []
    if not blockers:
        return facts
    details = []
    for blocker in blockers:
        detail = str(blocker.get('code') or 'WRITES_BLOCKED')
        if blocker.get('task'):
            detail += ' task=' + str(blocker['task'])
        if blocker.get('detail'):
            detail += ': ' + str(blocker['detail'])
        details.append(detail)
    raise WorkflowError('DISPATCH_WRITES_GATE ' + ' | '.join(details))


def _planned_worktree(project, state, task, requested, mode, pending=None):
    if requested is not None or mode == 'bind':
        if requested is None:
            raise WorkflowError('WORKTREE_PATH_REQUIRED')
        worktree = Path(requested).resolve()
        if not worktree.is_dir():
            raise WorkflowError('WORKTREE_PATH_MISSING ' + str(worktree))
        adapter = WorktreeAdapter(project.root)
        try:
            inspection = adapter.bind(
                worktree,
                ownership='bound',
                binding_uid=(pending or {}).get('id') or 'dispatch-preflight',
            )
        except WorktreeAdapterError as exc:
            legacy_non_git_child = (
                str(exc).startswith('WORKTREE_NOT_REGISTERED')
                and inside(project.root, worktree)
                and worktree != Path(project.root).resolve()
                and not (worktree / '.git').exists()
            )
            if not legacy_non_git_child:
                raise WorkflowError(str(exc)) from exc
            inspection = {
                'path': str(worktree),
                'branch': None,
                'head': None,
                'registered': False,
                'common_dir': None,
                'project_uid': None,
            }
        return {
            'worktree': str(worktree),
            'branch': inspection.get('branch'),
            'base': inspection.get('head'),
            'managed_worktree': False,
            'worktree_mode': 'bound',
            'binding_verified': bool(inspection.get('registered')),
            'common_dir': inspection.get('common_dir'),
            'project_uid': inspection.get('project_uid'),
            'build_root': None,
            'build_env': {},
            'build_mode': 'bound',
        }
    worktree, branch = _automatic_location(project, state, task)
    worktree = worktree.resolve()
    base = _base(project, state)
    try:
        project_inspection = WorktreeAdapter(project.root).inspect(
            project.root
        )
    except WorktreeAdapterError as exc:
        raise WorkflowError(str(exc)) from exc
    common_dir = project_inspection['common_dir']
    project_uid = project.identity().get('id')
    if not inside(project.root, worktree) or worktree == project.root:
        raise WorkflowError('WORKTREE_PATH_ESCAPE ' + str(worktree))
    recovering = (
        pending is not None
        and pending.get('status') == 'preparing'
        and Path(pending.get('worktree') or '').resolve() == worktree
        and pending.get('branch') == branch
    )
    if recovering and (
        pending.get('base') != base
        or pending.get('common_dir') != common_dir
        or pending.get('project_uid') != project_uid
    ):
        raise WorkflowError('DISPATCH_WORKTREE_RECOVERY_MISMATCH ' + task['id'])
    if pending is not None and not recovering:
        raise WorkflowError('DISPATCH_WORKTREE_RECOVERY_MISMATCH ' + task['id'])
    if not recovering and worktree.exists() and any(worktree.iterdir()):
        raise WorkflowError('WORKTREE_PATH_CONFLICT ' + str(worktree))
    exists = _git_run(
        project.root, 'show-ref', '--verify', '--quiet',
        'refs/heads/' + branch,
    )
    if exists.returncode == 0 and not recovering:
        raise WorkflowError('WORKTREE_BRANCH_CONFLICT ' + branch)
    return {
        'worktree': str(worktree),
        'branch': branch,
        'base': base,
        'managed_worktree': True,
        'worktree_mode': 'managed',
        'binding_verified': True,
        'common_dir': common_dir,
        'project_uid': project_uid,
        **build_environment(project, state, task),
    }


def _batch_resource_request_uid(state, planned, contracts):
    work_uid = state.get('work_uuid')
    if not isinstance(work_uid, str) or not work_uid:
        raise WorkflowError('DISPATCH_WORK_UID_REQUIRED')
    return digest_value({
        'schema': 1,
        'project_id': state.get('project_id'),
        'work_uid': work_uid,
        'tasks': [
            {
                'task': row['task'],
                'contract_digest': contracts[row['task']]['content_digest'],
                'worktree': row['worktree'],
                'branch': row['branch'],
                'claimed_paths': row['claimed_paths'],
                'writes': row['writes'],
            }
            for row in planned
        ],
    })


def _batch_resources(registry, planned):
    resources = {}

    def add(kind, identity, disposition):
        canonical = (
            registry._canonical_branch(identity)
            if kind == 'branch'
            else registry._canonical_path(identity)
        )
        resources[(kind, canonical)] = (kind, canonical, disposition)

    for row in planned:
        add(
            'path', row['worktree'],
            'delete' if row['managed_worktree'] else 'release',
        )
        if row.get('branch'):
            add('branch', row['branch'], 'release')
        if row.get('build_root'):
            add('build_root', row['build_root'], 'delete')
        for path in row['writes']:
            add('path', str(Path(row['worktree']) / path), 'release')
    return list(resources.values())


def _ensure_candidate_dispatch_ref(
    project, state, candidate, contract, planned,
):
    dispatch_ref = candidate.get('dispatch_ref')
    if dispatch_ref is None:
        dispatch_ref = _allocate_dispatch_ref(
            project, state, candidate['task'], candidate['id'],
            contract, planned,
        )
        candidate['dispatch_ref'] = dispatch_ref
    if (
        not isinstance(dispatch_ref, dict)
        or not isinstance(dispatch_ref.get('uid'), str)
        or not dispatch_ref.get('uid')
    ):
        raise WorkflowError(
            'DISPATCH_IDENTITY_ALLOCATION_FAILED missing dispatch UID'
        )
    return dispatch_ref


def _candidate_execution_slot(candidate, state):
    dispatch_ref = candidate['dispatch_ref']
    owner = ResourceOwner(
        work_uid=state['work_uuid'],
        session_uid=dispatch_ref['uid'],
        generation=1,
    )
    request_uid = candidate.get('execution_slot_request_uid')
    if request_uid is None:
        request_uid = digest_value({
            'schema': 1,
            'kind': 'execution-slot',
            'project_id': state.get('project_id'),
            'work_uid': state.get('work_uuid'),
            'dispatch_uid': dispatch_ref['uid'],
        })
    return request_uid, owner


def _business_request(project, state, logical_key, input_digest, operation_kind):
    work_uid = state.get('work_uuid')
    project_uid = IdentityStore(project.root).project_uid()
    if not isinstance(work_uid, str) or not work_uid:
        raise WorkflowError('DISPATCH_WORK_UID_REQUIRED')
    return RequestRef(
        request_uid=digest_value({
            'project_uid': project_uid,
            'logical_key': logical_key,
            'operation_kind': operation_kind,
        }),
        logical_key=logical_key,
        input_digest=input_digest,
        project_uid=project_uid,
        work_uid=work_uid,
        attempt_uid=None,
        operation_kind=operation_kind,
    )


def _allocate_dispatch_ref(project, state, task_id, alias, contract, planned):
    store = IdentityStore(project.root)
    request = _business_request(
        project,
        state,
        f'dispatch-create:{state["work_uuid"]}:{task_id}:{alias}',
        digest_value({
            'contract_digest': contract['content_digest'],
            'worktree': planned['worktree'],
            'branch': planned['branch'],
            'claimed_paths': planned['claimed_paths'],
            'writes': planned['writes'],
        }),
        'dispatch-create',
    )
    try:
        return store.allocate('dispatch', None, request).to_dict()
    except IdentityStoreError as exc:
        raise WorkflowError('DISPATCH_IDENTITY_ALLOCATION_FAILED ' + str(exc)) from exc


def _rollback_prepared_worktrees(project, prepared):
    """Remove resources created by a dispatch attempt that was not published."""
    pending = []
    for row in reversed(prepared):
        if not row.get('managed_worktree'):
            continue
        result = cleanup_worktree(project, row, 'dispatch rollback', retry=True)
        if result.get('result') == 'cleanup-pending':
            pending.append({
                'path': row.get('worktree'),
                'branch': row.get('branch'),
                'cleanup_error': result.get('cleanup_error'),
            })
    return pending


def _integration_task(state):
    integrations = [task for task in state.get('tasks', [])
                    if task.get('role') == 'integration']
    return integrations[0] if len(integrations) == 1 else None


def _receipt_path(project, state, receipt_id):
    for relative in state.get('evidence', []):
        if relative.endswith('/' + receipt_id + '/receipt.json'):
            path = project.root / relative
            if path.is_file():
                return path
    raise WorkflowError('EVIDENCE_MISSING ' + str(receipt_id))


def _receipt_logs_valid(path, receipt):
    for name, expected in (receipt.get('logs') or {}).items():
        if digest(path.parent / name) != expected:
            raise WorkflowError('EVIDENCE_TAMPERED ' + name)


def _cleanup_record(row, cleanup_result, reason):
    return {
        'id': uuid.uuid4().hex,
        'dispatch_id': row['id'],
        'task': row['task'],
        'result': cleanup_result.get('result'),
        'worktree': row.get('worktree'),
        'reason': redact(reason),
        'attempt': cleanup_result.get('cleanup_attempts', 1),
        'error': cleanup_result.get('cleanup_error'),
        'cleanup_error': cleanup_result.get('cleanup_error'),
        'at': now(),
    }


def _integration_attestation(project, state, receipt):
    """Bind the passing final receipt to every child receipt and commit."""
    integration = _integration_task(state)
    if integration is None:
        return None
    target = project.workspace_root(state, task_id=integration['id'])
    target_identity = git_identity(target)
    tasks = _task_map(state)
    children = []
    for task_id in sorted(integration_dependencies(state, integration)):
        task = tasks.get(task_id)
        if task is None or task.get('status') != 'done':
            raise WorkflowError('GIT_INTEGRATION_DEPENDENCY_MISSING ' + task_id)
        receipt_id = task.get('evidence')
        if not receipt_id:
            raise WorkflowError('EVIDENCE_MISSING ' + task_id)
        path = _receipt_path(project, state, receipt_id)
        child_receipt = read(path)
        if child_receipt.get('task') != task_id or child_receipt.get('status') != 'pass':
            raise WorkflowError('EVIDENCE_STALE_OR_FAILED ' + task_id)
        _receipt_logs_valid(path, child_receipt)
        child_head = (child_receipt.get('git') or {}).get('head')
        if not child_head:
            raise WorkflowError('GIT_CHILD_HEAD_MISSING ' + task_id)
        included = is_ancestor(target, child_head, 'HEAD')
        if included.returncode:
            raise WorkflowError('GIT_INTEGRATION_MISSING_CHILD ' + task_id)
        children.append({
            'task': task_id,
            'receipt': receipt_id,
            'receipt_sha256': digest(path),
            'workspace_root': child_receipt.get('workspace_root'),
            'fingerprint': child_receipt.get('fingerprint'),
            'contract': child_receipt.get('contract'),
            'head': child_head,
        })
    receipt_path = _receipt_path(project, state, receipt['id'])
    return {
        'schema': 1,
        'at': now(),
        'integration_task': integration['id'],
        'workspace_root': str(target),
        'head': target_identity.get('head'),
        'all_receipt': receipt['id'],
        'all_receipt_sha256': digest(receipt_path),
        'all_fingerprint': receipt.get('fingerprint'),
        'contract': contract_hash(project, state, project.paths(state['id'])[1]),
        'children': children,
        'cleanup': [],
    }


def attested_integration(project, state):
    """Return a still-valid post-cleanup integration proof, or ``None``."""
    try:
        data = load_dispatch(project, state)
        attestation = data.get('integration_attestation') or {}
        integration = _integration_task(state)
        if not integration or attestation.get('integration_task') != integration['id']:
            return None
        target = project.workspace_root(state, task_id=integration['id'])
        target_identity = git_identity(target)
        if target_identity.get('head') != attestation.get('head'):
            return None
        receipt_path = _receipt_path(project, state, attestation.get('all_receipt'))
        if digest(receipt_path) != attestation.get('all_receipt_sha256'):
            return None
        receipt = read(receipt_path)
        all_rows = [row for row in state.get('evidence', [])
                    if row.endswith('/' + str(attestation.get('all_receipt')) + '/receipt.json')]
        latest_all = None
        for relative in state.get('evidence', []):
            candidate = read(project.root / relative)
            if candidate.get('task') == 'all':
                latest_all = candidate
        if not all_rows or not latest_all or latest_all.get('id') != attestation.get('all_receipt'):
            return None
        if receipt.get('status') != 'pass' or receipt.get('workspace_root') != str(target):
            return None
        if receipt.get('fingerprint') != attestation.get('all_fingerprint'):
            return None
        if receipt.get('fingerprint') != fingerprint(target):
            return None
        current_contract = contract_hash(project, state, project.paths(state['id'])[1])
        tasks = _task_map(state)
        for child in attestation.get('children', []):
            task = tasks.get(child.get('task'))
            if not task or task.get('status') != 'done' or task.get('evidence') != child.get('receipt'):
                return None
            child_path = _receipt_path(project, state, child.get('receipt'))
            if digest(child_path) != child.get('receipt_sha256'):
                return None
            child_receipt = read(child_path)
            if (child_receipt.get('task') != child.get('task')
                    or child_receipt.get('status') != 'pass'
                    or child_receipt.get('workspace_root') != child.get('workspace_root')
                    or child_receipt.get('fingerprint') != child.get('fingerprint')
                    or child_receipt.get('contract') != current_contract
                    or (child_receipt.get('git') or {}).get('head') != child.get('head')):
                return None
            _receipt_logs_valid(child_path, child_receipt)
            included = is_ancestor(target, child['head'], 'HEAD')
            if included.returncode:
                return None
        if receipt.get('contract') != current_contract:
            return None
        _receipt_logs_valid(receipt_path, receipt)
        return attestation
    except (OSError, KeyError, TypeError, ValueError, WorkflowError):
        return None


def attested_removed_tasks(project, state):
    """Task ids covered by a valid attestation whose child worktrees are gone."""
    key = (state.get('id'), tuple(state.get('evidence') or []),
           int(state.get('verification_epoch') or 0),
           tuple((task.get('id'), task.get('status'), task.get('evidence'))
                 for task in state.get('tasks') or []))

    def load():
        attestation = attested_integration(project, state)
        if not attestation:
            return set()
        return {
            child.get('task') for child in attestation.get('children', [])
            if child.get('workspace_root') and not Path(child['workspace_root']).is_dir()
        }

    return cached_value('attested_removed', key, load)


def finalize_integration_cleanup(project, state, final_receipt, binding=None):
    """Record the integration proof, then remove accepted managed children."""
    if final_receipt.get('task') != 'all' or final_receipt.get('status') != 'pass':
        return {'attested': False, 'cleanup': []}
    attestation = _integration_attestation(project, state, final_receipt)
    if attestation is None:
        return {'attested': False, 'cleanup': []}
    if binding is not None:
        from .resource_operations import ensure_work_evidence
        ensure_work_evidence(project,state,binding=binding)
    data = load_dispatch(project, state)
    data['integration_attestation'] = attestation
    _save_dispatch(project, state, data)
    rows = [row for row in data.get('dispatches', [])
            if row.get('task') in {child['task'] for child in attestation['children']}
            and row.get('status') != 'superseded']
    latest = {}
    for row in rows:
        latest[row['task']] = row
    registry = load_registry(project, state)
    cleaned = []
    for task_id, row in sorted(latest.items()):
        mapping = registry.setdefault('tasks', {}).setdefault(task_id, {})
        if mapping.get('acceptance_status') != 'accepted':
            continue
        if not row.get('managed_worktree') or row.get('worktree_state') in {'removed', 'released'}:
            continue
        cleanup_result = cleanup_worktree(project, row, 'integration regression passed')
        row.update(cleanup_result)
        record = _cleanup_record(row, cleanup_result, 'integration regression passed')
        data.setdefault('cleanup', []).append(record)
        mapping['cleanup_status'] = cleanup_result.get('result')
        mapping['cleanup_error'] = cleanup_result.get('cleanup_error')
        if cleanup_result.get('result') in {'removed', 'released'}:
            row['status'] = 'cleaned'
        else:
            row['status'] = 'accepted'
        row['updated_at'] = now()
        attestation['cleanup'].append({
            'task': task_id,
            'result': cleanup_result.get('result'),
            'error': cleanup_result.get('cleanup_error'),
        })
        cleaned.append(record)
    data['integration_attestation'] = attestation
    _save_dispatch(project, state, data)
    save_registry(project, state, registry)
    return {'attested': True, 'cleanup': cleaned}


def render_task_prompt(state, task, dispatch):
    paths = dispatch.get('claimed_paths') or ['—']
    dependencies = task.get('depends') or ['无']
    plan_path = (state.get('documents') or {}).get('plan') or f"{state.get('doc_dir', 'docs/work')}/plan.md"
    brief_path = dispatch.get('brief') or f"{state.get('doc_dir', 'docs/work')}/sessions/{task['id']}-brief.md"
    interfaces = task.get('interfaces') or ['遵循总体 plan.md 中的公共接口约束']
    tests = task.get('tests') or [str(task.get('acceptance') or '完成任务验收并提交 report')]
    build_env = dispatch.get('build_env') or {}
    return '\n'.join([
        f"# {task['id']} 子会话启动提示词", '',
        f"- GSP：{state.get('number') or state['id']}",
        f"- 任务：{task['id']} {task.get('title') or ''}".rstrip(),
        f"- provisional session：{dispatch.get('session_hint') or '由主会话登记'}",
        '- 角色：worker',
        '- 主会话：controller，结果通过 report 回传',
        f"- 平台目标：{dispatch.get('platform')}",
        f"- 派发方式：{dispatch.get('dispatch_mode')}",
        f"- branch：{dispatch.get('branch') or '由宿主绑定'}",
        f"- worktree：{dispatch.get('worktree') or '由宿主绑定'}",
        f"- 构建根：{dispatch.get('build_root') or '—（worktree 自带构建目录）'}", '',
        '## 必读文件', '',
        f'- 总体计划：{plan_path}',
        f'- 本任务 brief：{brief_path}', '',
        '## 目标', '',
        str(task.get('acceptance') or task.get('title') or '完成计划任务并提交验证报告'), '',
        '## 允许路径', '',
        *[f'- {path}' for path in paths], '',
        '## 公共接口', '',
        *[f'- {item}' for item in interfaces], '',
        '## 测试要求', '',
        *[f'- {item}' for item in tests], '',
        '## 依赖', '',
        *[f'- {item}' for item in dependencies], '',
        '## 构建环境', '',
        *([f'- {key}={value}' for key, value in build_env.items()] or
          ['- 无托管构建根；不得把临时构建目录长期留在 worktree 内。']),
        '- 构建产物与临时文件必须落在上述构建根或运行隔离目录，收尾时必须回收。',
        '',
        '## 禁止范围', '',
        '- 不修改总体 plan、父状态或其他任务的文件。',
        '- 不扩大到未声明的路径、公共契约、凭据或外部动作。', '',
        '## 工作规则', '',
        '- 先读取总体 plan.md 和本任务 brief。',
        '- 局部实现、局部测试和普通机械检查由本子会话决定顺序。',
        '- 公共接口、范围、基线、安全和外部动作问题先返回 CHANGE_REQUEST。', '',
        '## 完成回传', '',
        '执行 `session-report`，状态只能使用 `DONE`、`BLOCKED` 或 `CHANGE_REQUEST`。',
        '报告至少包含 STATUS、TASK、FILES、TESTS、INTERFACES、EVIDENCE 和 NEED_FROM_MAIN。',
        f"示例：`python scripts/gsp.py session-report {state['id']} {task['id']} --session <Snn> --status DONE --summary \"...\"`", '',
    ])


def _workspace_group_result(state, row, *, idempotent=False):
    group_uid = row.get('workspace_group_uid')
    return {
        'task': state['id'],
        'batch_size': 1,
        'workspace_group': {
            'schema': 1,
            'mode': 'sequential',
            'state': row.get('workspace_group_state') or 'pending_host',
            'group_uid': group_uid,
            'resource_request_uid': row.get(
                'workspace_group_resource_request_uid'
            ),
            'workspace_root': row.get('worktree'),
            'branch': row.get('branch'),
            'host_observation': 'unverified',
            'idempotent': bool(idempotent),
        },
        'dispatches': [row],
        'pending_action': {
            'kind': 'register-workspace-group-worker',
            'task': row['task'],
            'workspace_root': row.get('worktree'),
            'branch': row.get('branch'),
            'contract': row.get('contract'),
        },
        'next_action': (
            'Register one trusted worker context with '
            '--workspace-group parent; the parent root/branch are reused and '
            'the new worker receives the sole source-writer lease.'
        ),
    }


def _dispatch_sequential_parent_group(
    project, state, args, controller, data, tasks_by_id, requested,
):
    """Prepare one sequential worker on the explicit parent workspace."""
    if len(requested) != 1:
        raise WorkflowError('WORKSPACE_GROUP_SINGLE_TASK_REQUIRED')
    task_id = requested[0]
    task = tasks_by_id.get(task_id)
    if task is None:
        raise WorkflowError('DISPATCH_TASK_NOT_FOUND ' + str(task_id))
    accepted = _accepted_task_ids(state, data)
    dependencies = set(task.get('depends') or [])
    if dependencies - accepted:
        raise WorkflowError('DISPATCH_DEPENDENCY_BLOCKED ' + task_id)
    if not controller.get('worktree'):
        raise WorkflowError('WORKSPACE_GROUP_PARENT_ROOT_REQUIRED')
    document_texts, source_paths = _contract_inputs(project, state)
    _require_dispatch_writes_gate(
        state, document_texts['plan'], [task],
    )
    try:
        contract = _task_contract(project,state,task_id,document_texts,source_paths)
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc

    from .workspace_groups import (
        SessionWorkspaceLeaseAuthority,
        build_workspace_group,
    )

    group = build_workspace_group(
        project.root,
        project.root,
        controller['worktree'],
        project_uid=IdentityStore(project.root).project_uid(),
        work_uid=state['work_uuid'],
        parent_binding_uid=controller['binding_uid'],
        controller_binding_uid=controller['binding_uid'],
        controller_generation=int(
            controller.get('controller_generation') or 1
        ),
        cancel_generation=int(
            controller.get('cancel_generation') or 1
        ),
    )
    if any((row.get('topology') or {}).get('workspace_root')
           and Path(row['topology']['workspace_root']).resolve() != Path(group.workspace_root).resolve()
           for row in state.get('tasks') or []):
        raise WorkflowError('WORKSPACE_GROUP_MIXED_TOPOLOGY')
    if any(row.get('status') in ACTIVE_DISPATCH and row.get('worktree')
           and Path(row['worktree']).resolve() != Path(group.workspace_root).resolve()
           for row in data.get('dispatches') or []):
        raise WorkflowError('WORKSPACE_GROUP_MIXED_TOPOLOGY')
    lease_authority = SessionWorkspaceLeaseAuthority(
        project.root, state['id']
    )
    # This preview is intentionally before UID/resource/file allocation.  An
    # unaccepted or currently executing predecessor therefore leaves no new
    # dispatch, slot or build-root intent behind.
    preflight = lease_authority.preflight_claim(group, task_uid=task_id)
    if state.get('inflight') is not None:
        raise WorkflowError('WORKSPACE_GROUP_EXECUTION_UNKNOWN')

    existing = next(
        (
            row for row in reversed(data.get('dispatches', []))
            if row.get('task') == task_id
            and row.get('status') not in {'superseded', 'cleaned'}
        ),
        None,
    )
    if existing is not None:
        if (existing.get('workspace_group_uid') == group.group_uid
                and existing.get('content_digest') == contract['content_digest']
                and existing.get('status') == 'preparing'):
            return _complete_workspace_group_intent(project,state,data,existing,contract)
        if (
            existing.get('workspace_group_uid') == group.group_uid
            and existing.get('content_digest') == contract['content_digest']
            and existing.get('status') in {
                'workspace-pending', 'adapter-ready', 'manual-required',
                'running', 'reported',
            }
        ):
            return _workspace_group_result(state, existing, idempotent=True)
        raise WorkflowError('DISPATCH_TASK_EXISTS ' + task_id)

    path_map, common_path = _assignment(
        getattr(args, 'path', []), 'path',
    )
    raw_path = path_map.get(task_id) or common_path
    if not raw_path:
        raise WorkflowError('DISPATCH_PATH_REQUIRED ' + task_id)
    claimed = [_normalize_path(Path(group.workspace_root), raw_path)]
    writes = contract.get('writes') or claimed
    build = build_environment(project, state, task)
    if not build.get('build_root'):
        raise WorkflowError('WORKSPACE_GROUP_BUILD_ROOT_REQUIRED')
    target = {
        'worktree': group.workspace_root,
        'branch': group.branch,
        'base': group.base_sha,
        'managed_worktree': False,
        'worktree_mode': 'workspace-group',
        'binding_verified': True,
        'common_dir': group.common_dir,
        'project_uid': group.project_uid,
        **build,
    }
    human_dir = project.paths(state['id'])[1] / 'sessions'
    contract_target = human_dir / f'{task_id}-contract.md'
    for candidate in data.get('dispatches', []):
        if candidate.get('status') not in ACTIVE_DISPATCH:
            continue
        if candidate.get('worktree') != group.workspace_root:
            continue
        candidate_task = candidate.get('task')
        candidate_worker = candidate.get('source_writer_session_id')
        worker_row = next(
            (
                row for row in preflight['registry'].get('sessions', [])
                if row.get('id') == candidate_worker
            ),
            None,
        )
        accepted = (
            (preflight['registry'].get('tasks') or {})
            .get(candidate_task, {})
            .get('acceptance_status') == 'accepted'
        )
        if (
            candidate.get('workspace_group_uid') == group.group_uid
            and accepted
            and worker_row is not None
            and worker_row.get('status') == 'released'
        ):
            continue
        raise WorkflowError('DISPATCH_WORKTREE_CONFLICT ' + task_id)
    if contract_target.exists() and not getattr(args, 'retry', False):
        raise WorkflowError('DISPATCH_PROMPT_EXISTS ' + task_id)

    target_map, common_platform = _assignment(
        getattr(args, 'target', []), 'target',
    )
    platform = (
        target_map.get(task_id) or common_platform
        or getattr(args, 'platform', None) or 'manual'
    ).lower()
    if platform not in PLATFORMS:
        raise WorkflowError('DISPATCH_PLATFORM_INVALID ' + platform)
    mode = (
        getattr(args, 'dispatch_mode', None)
        or ('auto' if platform == 'codex' else 'manual')
    ).lower()
    if mode not in DISPATCH_MODES:
        raise WorkflowError('DISPATCH_MODE_INVALID ' + mode)
    dispatch_alias = _next_id(data)
    planned = {
        'task': task_id,
        'worktree': group.workspace_root,
        'branch': group.branch,
        'claimed_paths': claimed,
        'writes': list(writes),
    }
    dispatch_ref = _allocate_dispatch_ref(
        project, state, task_id, dispatch_alias, contract, planned,
    )
    candidate = {
        'id': dispatch_alias,
        'task': task_id,
        'session_hint': _next_session_hints(
            project, state, 1, data,
        )[0],
        'platform': platform,
        'dispatch_mode': mode,
        'status': 'workspace-pending',
        'claimed_paths': claimed,
        'dependencies': sorted(dependencies),
        'created_at': now(),
        'updated_at': now(),
        'thread_id': None,
        'contract': _relative(project, contract_target),
        'brief': _relative(project, contract_target),
        'prompt': _relative(project, contract_target),
        'contract_uid': contract['contract_uid'],
        'content_digest': contract['content_digest'],
        'model_alias': 'group/deepseek-2',
        'effort': 'max',
        **target,
        'workspace_group_mode': 'parent',
        'workspace_group_uid': group.group_uid,
        'workspace_group_resource_request_uid': group.resource_request_uid,
        'workspace_group_state': 'pending_host',
        'source_writer_session_id': None,
        'source_writer_binding_uid': None,
        'host_observation': 'unverified',
        'dispatch_ref': dispatch_ref,
        'execution_slot_request_uid': None,
        'execution_slot_identity': None,
        'execution_slot_owner': None,
        'execution_slot_state': None,
        'execution_slot_released_at': None,
        'execution_slot_released_by': None,
        'execution_slot_release_reason': None,
        'execution_slot_scope': 'project',
        'features': {'multi_agent': False},
    }
    slot_request_uid, slot_owner = _candidate_execution_slot(
        candidate, state,
    )
    candidate['execution_slot_request_uid'] = slot_request_uid
    candidate['execution_slot_owner'] = {
        'work_uid': slot_owner.work_uid,
        'session_uid': slot_owner.session_uid,
        'generation': slot_owner.generation,
    }
    task_resource_request_uid = digest_value({
        'schema': 1,
        'kind': 'workspace-group-worker',
        'group_uid': group.group_uid,
        'task_uid': task_id,
        'contract_digest': contract['content_digest'],
        'build_root': target['build_root'],
    })
    candidate['resource_request_uid'] = task_resource_request_uid
    candidate['resource_state'] = 'intent'
    candidate['status'] = 'preparing'
    candidate['scheduler_token'] = getattr(args,'_schedule_token',None)
    data['dispatches'].append(candidate)
    _save_dispatch(project,state,data)
    return _complete_workspace_group_intent(project,state,data,candidate,contract)


def _complete_workspace_group_intent(project,state,data,candidate,contract):
    """Resume the same sequential dispatch after a lost grant/file response."""
    task_id=candidate['task']
    task=_task_map(state)[task_id]
    task_resource_request_uid=candidate['resource_request_uid']
    slot_request_uid=candidate['execution_slot_request_uid']
    slot_owner=ResourceOwner(**candidate['execution_slot_owner'])
    contract_target=project.root/candidate['contract']
    resources = [{
        'kind': 'build_root',
        'identity': candidate['build_root'],
        'disposition': 'delete',
    }]
    registry = ResourceRegistry(project.root)
    try:
        reserved = registry.reserve_execution_batch(
            task_resource_request_uid,
            slot_owner,
            resources,
            [(slot_request_uid, slot_owner)],
            scheduler=candidate.get('scheduler_token'),
        )
    except ResourceRegistryError as exc:
        raise WorkflowError(
            'DISPATCH_RESOURCE_CONFLICT ' + str(exc)
        ) from exc
    slot = reserved['slots'][slot_request_uid]
    candidate['execution_slot_identity'] = (
        slot['resources'][0]['identity']
    )
    candidate['execution_slot_state'] = slot['state']
    candidate['resource_state']=reserved['resources']['state']
    _save_dispatch(project, state, data)
    atomic(
        contract_target,
        render_task_contract(contract, state, task, candidate),
    )
    registry.commit(task_resource_request_uid, slot_owner)
    candidate['resource_state'] = 'committed'
    candidate['status'] = 'workspace-pending'
    _save_dispatch(project, state, data)
    sessions = load_registry(project, state)
    sessions.setdefault('tasks', {}).setdefault(task_id, {}).update({
        'dispatch_id': candidate['id'],
        'dispatch_status': candidate['status'],
        'contract': candidate['contract'],
        'brief': candidate['brief'],
        'prompt': candidate['prompt'],
        'platform': candidate['platform'],
        'dispatch_mode': candidate['dispatch_mode'],
        'contract_uid': candidate['contract_uid'],
        'content_digest': candidate['content_digest'],
        'model_alias': candidate['model_alias'],
        'effort': candidate['effort'],
        'dispatch_ref': candidate['dispatch_ref'],
    })
    save_registry(project, state, sessions)
    return _workspace_group_result(state, candidate)


def dispatch_tasks(project, state, args, caller_binding=None):
    registry = load_registry(project, state)
    controller = next((row for row in registry.get('sessions', []) if row.get('id') == args.session), None)
    if not controller or controller.get('role') != 'controller' or registry.get('controller_session') != args.session:
        raise WorkflowError('CONTROLLER_LEASE_REQUIRED')
    require_row_capability(project, controller, 'dispatch', state.get('work_uuid'))
    from .sessions import require_actor_binding
    require_actor_binding(caller_binding, controller, 'dispatch', state.get('work_uuid'))
    from .gate_rules import guard_inputs
    guard_inputs(project,state,binding=caller_binding,phase='dispatch')
    data = load_dispatch(project, state)
    tasks_by_id = _task_map(state)
    target_map, common_platform = _assignment(getattr(args, 'target', []), 'target')
    path_map, common_path = _assignment(getattr(args, 'path', []), 'path')
    worktree_map, common_worktree = _assignment(getattr(args, 'worktree', []), 'worktree')
    requested = list(getattr(args, 'tasks', []) or [])
    pending_intent = _pending_dispatch_intent(data)
    schedule_token = (
        pending_intent['scheduler_token'] if pending_intent is not None else None
    )
    if pending_intent is not None:
        if requested and set(requested) != set(pending_intent['tasks']):
            raise WorkflowError(
                'DISPATCH_PREPARATION_PENDING '
                + pending_intent['request_uid']
            )
        requested = list(pending_intent['tasks'])
    elif not requested and state.get('operation_schema')==2:
        from .project_scheduler import preview_schedule
        schedule=preview_schedule(project,limit=int(getattr(args,'max_parallel',3)))
        requested=[row['task_id'] for row in schedule['batch'] if row['work_uid']==state['work_uuid']]
        if not requested:
            raise WorkflowError('DISPATCH_FAIR_TURN_PENDING no project slot granted to this work')
        schedule_token={'revision':schedule['revision'],'cursor':{'work_uid':state['work_uuid']}}
    if not requested:
        accepted = _accepted_task_ids(state, data)
        existing = {row.get('task') for row in data.get('dispatches', []) if row.get('status') in ACTIVE_DISPATCH}
        requested = [
            task['id'] for task in state.get('tasks', [])
            if task.get('id') not in accepted and task.get('id') not in existing
            and all(dep in accepted for dep in task.get('depends') or [])
        ]
    if not requested:
        raise WorkflowError('DISPATCH_READY_EMPTY')
    workspace_group = getattr(args, 'workspace_group', None)
    if pending_intent is not None and any(
            row.get('status')=='preparing' and row.get('workspace_group_mode')=='parent'
            for row in data.get('dispatches',[])):
        workspace_group='parent'
    if workspace_group not in {None, 'parent'}:
        raise WorkflowError('WORKSPACE_GROUP_MODE_INVALID')
    if workspace_group == 'parent':
        args._schedule_token=schedule_token
        return _dispatch_sequential_parent_group(
            project, state, args, controller, data, tasks_by_id, requested,
        )
    max_parallel = int(getattr(args, 'max_parallel', 3) or 0)
    if max_parallel < 1 or len(requested) > max_parallel:
        raise WorkflowError('DISPATCH_BATCH_LIMIT')
    if len(set(requested)) != len(requested):
        raise WorkflowError('DISPATCH_TASK_DUPLICATE')
    accepted = _accepted_task_ids(state, data)
    existing_active = {
        row.get('task'): row for row in data.get('dispatches', [])
        if row.get('status') in ACTIVE_DISPATCH
    }
    document_texts, source_paths = _contract_inputs(project, state)
    for task_id in requested:
        if task_id not in tasks_by_id:
            raise WorkflowError('DISPATCH_TASK_NOT_FOUND ' + str(task_id))
    _require_dispatch_writes_gate(
        state,
        document_texts['plan'],
        [tasks_by_id[task_id] for task_id in requested],
    )
    pending_by_task = {
        row.get('task'): row for row in data.get('dispatches', [])
        if row.get('status') == 'preparing'
    }
    supersede = []
    planned, contracts = [], {}
    for index, task_id in enumerate(requested):
        task = tasks_by_id.get(task_id)
        if not task:
            raise WorkflowError('DISPATCH_TASK_NOT_FOUND ' + str(task_id))
        if task_id in existing_active and not getattr(args, 'retry', False):
            raise WorkflowError('DISPATCH_TASK_EXISTS ' + task_id)
        dependencies = set(task.get('depends') or [])
        if dependencies - accepted:
            raise WorkflowError('DISPATCH_DEPENDENCY_BLOCKED ' + task_id)
        if dependencies.intersection(requested):
            raise WorkflowError('DISPATCH_DEPENDENCY_BATCH ' + task_id)
        platform = (
            target_map.get(task_id) or common_platform
            or getattr(args, 'platform', None) or 'manual'
        ).lower()
        if platform not in PLATFORMS:
            raise WorkflowError('DISPATCH_PLATFORM_INVALID ' + platform)
        mode = (
            getattr(args, 'dispatch_mode', None)
            or ('auto' if platform == 'codex' else 'manual')
        ).lower()
        if mode not in DISPATCH_MODES:
            raise WorkflowError('DISPATCH_MODE_INVALID ' + mode)
        requested_worktree = worktree_map.get(task_id) or common_worktree
        raw_path = path_map.get(task_id) or common_path
        if not raw_path:
            raise WorkflowError('DISPATCH_PATH_REQUIRED ' + task_id)
        try:
            contract = _task_contract(project,state,task_id,document_texts,source_paths)
        except ValueError as exc:
            raise WorkflowError(str(exc)) from exc
        contracts[task_id] = contract
        target = _planned_worktree(
            project, state, task, requested_worktree,
            getattr(args, 'worktree_mode', None)
            or ('bind' if requested_worktree else 'auto'),
            pending=pending_by_task.get(task_id),
        )
        claimed = [_normalize_path(Path(target['worktree']), raw_path)]
        writes = contract.get('writes') or claimed
        for previous in planned:
            if previous['worktree'] != target['worktree']:
                continue
            if any(
                _overlap(left, right)
                for left in writes for right in previous['writes']
            ):
                raise WorkflowError('DISPATCH_PATH_CONFLICT ' + task_id)
            raise WorkflowError('DISPATCH_WORKTREE_CONFLICT ' + task_id)
        for previous in data.get('dispatches', []):
            if (
                previous.get('status') not in ACTIVE_DISPATCH
                or previous.get('worktree') != target['worktree']
            ):
                continue
            if previous.get('task') == task_id and getattr(args, 'retry', False):
                if previous not in supersede:
                    supersede.append(previous)
                continue
            if any(
                _overlap(left, right)
                for left in claimed
                for right in previous.get('claimed_paths') or []
            ):
                raise WorkflowError('DISPATCH_PATH_CONFLICT ' + task_id)
            raise WorkflowError('DISPATCH_WORKTREE_CONFLICT ' + task_id)
        planned.append({
            'task': task_id,
            'platform': platform,
            'dispatch_mode': mode,
            'dependencies': sorted(dependencies),
            'claimed_paths': claimed,
            'writes': list(writes),
            **target,
        })
    requested_uid = _batch_resource_request_uid(state, planned, contracts)
    preparing = [
        row for row in data.get('dispatches', [])
        if row.get('status') == 'preparing'
        and row.get('resource_request_uid') == requested_uid
    ]
    if preparing and {row.get('task') for row in preparing} != set(requested):
        raise WorkflowError('DISPATCH_PREPARATION_PENDING ' + requested_uid)
    if any(
        row.get('status') == 'preparing'
        and row.get('resource_request_uid') != requested_uid
        and row.get('task') in requested
        for row in data.get('dispatches', [])
    ):
        raise WorkflowError('DISPATCH_PREPARATION_PENDING ' + ','.join(requested))
    if not preparing and not getattr(args, 'retry', False):
        human_dir = project.paths(state['id'])[1] / 'sessions'
        for row in planned:
            targets = (
                human_dir / f"{row['task']}-contract.md",
                human_dir / f"{row['task']}-brief.md",
                human_dir / f"{row['task']}-prompt.md",
            )
            if any(target.exists() for target in targets):
                raise WorkflowError('DISPATCH_PROMPT_EXISTS ' + row['task'])

    if preparing:
        candidates = preparing
    else:
        session_hints = _next_session_hints(
            project, state, len(requested), data,
        )
        candidates = []
        for index, row in enumerate(planned):
            dispatch_alias = _next_id(data)
            dispatch_ref = _allocate_dispatch_ref(
                project, state, row['task'], dispatch_alias,
                contracts[row['task']], row,
            )
            status = (
                'adapter-ready'
                if row['dispatch_mode'] in {'auto', 'adapter'}
                and row['platform'] != 'manual'
                else 'manual-required'
            )
            candidates.append({
                'id': dispatch_alias,
                'task': row['task'],
                'session_hint': session_hints[index],
                'platform': row['platform'],
                'dispatch_mode': row['dispatch_mode'],
                'status': 'preparing',
                'claimed_paths': row['claimed_paths'],
                'dependencies': row['dependencies'],
                'created_at': now(),
                'updated_at': now(),
                'thread_id': None,
                'contract': None,
                'brief': None,
                'prompt': None,
                'contract_uid': contracts[row['task']]['contract_uid'],
                'content_digest': contracts[row['task']]['content_digest'],
                'model_alias': 'group/deepseek-2',
                'effort': 'max',
                'worktree': row['worktree'],
                'branch': row['branch'],
                'base': row['base'],
                'managed_worktree': row['managed_worktree'],
                'worktree_mode': row['worktree_mode'],
                'binding_verified': row['binding_verified'],
                'common_dir': row['common_dir'],
                'project_uid': row['project_uid'],
                'build_root': row['build_root'],
                'build_env': row['build_env'],
                'build_mode': row['build_mode'],
                'resource_request_uid': requested_uid,
                'resource_state': 'intent',
                'dispatch_ref': dispatch_ref,
                'execution_slot_request_uid': None,
                'execution_slot_identity': None,
                'execution_slot_owner': None,
                'execution_slot_state': None,
                'execution_slot_released_at': None,
                'execution_slot_released_by': None,
                'execution_slot_release_reason': None,
                'execution_slot_scope': 'project',
                'features': {'multi_agent': False},
            })

    resource_registry = ResourceRegistry(project.root)
    binding = controller.get('context_binding') or {}
    session_ref = binding.get('session') or {}
    owner = ResourceOwner(
        work_uid=state['work_uuid'],
        session_uid=(
            session_ref.get('uid')
            or controller.get('binding_uid')
            or controller['id']
        ),
        generation=int(controller.get('controller_generation') or 1),
    )
    slot_requests = []
    for row in candidates:
        planned_row = next(
            item for item in planned if item['task'] == row['task']
        )
        _ensure_candidate_dispatch_ref(
            project, state, row,
            contracts[row['task']], planned_row,
        )
        slot_request_uid, slot_owner = _candidate_execution_slot(
            row, state,
        )
        row['execution_slot_request_uid'] = slot_request_uid
        row['execution_slot_owner'] = {
            'work_uid': slot_owner.work_uid,
            'session_uid': slot_owner.session_uid,
            'generation': slot_owner.generation,
        }
        row['scheduler_token'] = schedule_token
        row['execution_slot_state'] = 'intent'
        row['updated_at'] = now()
        slot_requests.append((slot_request_uid, slot_owner))
    resources = _batch_resources(resource_registry, planned)
    if not preparing:
        data['dispatches'].extend(candidates)
    # The dispatch intent, including the canonical schedule token, must be
    # durable before the registry can consume a cursor revision or slot.
    _save_dispatch(project, state, data)
    try:
        reserved_batch = resource_registry.reserve_execution_batch(
            requested_uid, owner, resources, slot_requests,
            scheduler=schedule_token,
        )
    except ResourceRegistryError as exc:
        for row in candidates:
            row['status'] = 'preparation-failed'
            row['resource_state'] = 'not-granted'
            row['execution_slot_state'] = 'not-granted'
            row['updated_at'] = now()
        _save_dispatch(project, state, data)
        raise WorkflowError('DISPATCH_RESOURCE_CONFLICT ' + str(exc)) from exc
    reserved = reserved_batch['resources']
    for row in candidates:
        row['resource_state'] = reserved['state']
        slot = reserved_batch['slots'][row['execution_slot_request_uid']]
        row['execution_slot_identity'] = (
            slot['resources'][0]['identity']
        )
        row['execution_slot_state'] = slot['state']
        row['updated_at'] = now()
    _save_dispatch(project, state, data)

    machine, human = _folders(project, state)
    rendered = {}
    for row in candidates:
        task = tasks_by_id[row['task']]
        rendered[row['task']] = render_task_contract(
            contracts[row['task']], state, task, row,
        )
    for row in candidates:
        planned_row = next(item for item in planned if item['task'] == row['task'])
        task = tasks_by_id[row['task']]
        managed_existing = (
            row.get('managed_worktree')
            and Path(row['worktree']).is_dir()
        )
        if managed_existing:
            try:
                inspection = WorktreeAdapter(project.root).inspect(
                    row['worktree']
                )
            except WorktreeAdapterError as exc:
                raise WorkflowError(str(exc)) from exc
            mismatch = next((
                field for field in (
                    'branch', 'base', 'common_dir', 'project_uid',
                )
                if (
                    (field == 'base' and inspection.get('head'))
                    or inspection.get(field)
                ) != row.get(field)
            ), None)
            if mismatch:
                raise WorkflowError(
                    'DISPATCH_WORKTREE_RECOVERY_MISMATCH '
                    + row['task'] + ':' + mismatch
                )
            prepared_row = {
                **row,
                'path': inspection['path'],
                'worktree': inspection['path'],
                'worktree_state': 'created',
                'updated_at': now(),
            }
        elif row.get('worktree_state') == 'bound':
            prepared_row = prepare_worktree(
                project, state, task, row,
                requested_path=row['worktree'],
                mode='bind',
            )
            mismatch = next((
                field for field in (
                    'path', 'worktree', 'branch', 'base',
                    'managed_worktree', 'worktree_mode', 'binding_verified',
                    'binding_uid', 'common_dir', 'project_uid',
                    'cleanup_action', 'deletable', 'host_action',
                )
                if prepared_row.get(field) != row.get(field)
            ), None)
            if mismatch:
                raise WorkflowError(
                    'DISPATCH_WORKTREE_RECOVERY_MISMATCH '
                    + row['task'] + ':' + mismatch
                )
        elif row.get('worktree_state') == 'created':
            inspection = inspect_worktree(project, row['worktree'])
            prepared_row = {
                **row,
                'path': inspection['path'],
                'worktree': inspection['path'],
                'branch': inspection['branch'] or row['branch'],
                'base': inspection['head'],
                'worktree_state': 'bound',
                'updated_at': now(),
            }
        else:
            prepared_row = prepare_worktree(
                project, state, task, row,
                requested_path=planned_row['worktree']
                if not planned_row['managed_worktree'] else None,
                mode=planned_row['worktree_mode'],
            )
        prepared_row['status'] = 'preparing'
        prepared_row['resource_request_uid'] = requested_uid
        prepared_row['resource_state'] = reserved['state']
        schedule_intent = row.get('scheduler_token')
        row.clear()
        row.update(prepared_row)
        if 'scheduler_token' not in row:
            row['scheduler_token'] = schedule_intent
        _save_dispatch(project, state, data)

    for row in candidates:
        contract_target = human / f"{row['task']}-contract.md"
        relative_contract = _relative(project, contract_target)
        row.update({
            'contract': relative_contract,
            'brief': relative_contract,
            'prompt': relative_contract,
            'status': (
                'adapter-ready'
                if row['dispatch_mode'] in {'auto', 'adapter'}
                and row['platform'] != 'manual'
                else 'manual-required'
            ),
            'resource_state': 'committed',
            'updated_at': now(),
        })
        atomic(contract_target, rendered[row['task']])
    resource_registry.commit(requested_uid, owner)
    for row in candidates:
        if row['platform'] == 'codex' and row['dispatch_mode'] == 'auto':
            _launch_native_dispatch(
                project, state, data, row,
                contracts[row['task']], rendered[row['task']],
                int(getattr(args, 'worker_timeout', 900) or 900),
            )
    for previous in supersede:
        previous['status'] = 'superseded'
        previous['updated_at'] = now()
    _save_dispatch(project, state, data)
    for row in candidates:
        registry.setdefault('tasks', {}).setdefault(row['task'], {}).update({
            'dispatch_id': row['id'],
            'dispatch_status': row['status'],
            'contract': row['contract'],
            'brief': row['brief'],
            'platform': row['platform'],
            'dispatch_mode': row['dispatch_mode'],
            'prompt': row['prompt'],
            'contract_uid': row['contract_uid'],
            'content_digest': row['content_digest'],
            'model_alias': row['model_alias'],
            'effort': row['effort'],
            'dispatch_ref': row['dispatch_ref'],
        })
    save_registry(project, state, registry)
    next_action = (
        'Codex native workers completed; submit their session reports.'
        if any(
            row.get('platform') == 'codex' and row.get('status') == 'dispatched'
            for row in candidates
        )
        else 'Codex host adapter creates threads for adapter-ready requests; '
             'user forwards manual-required prompts to ZCode or other manual platforms.'
    )
    return {
        'task': state['id'],
        'batch_size': len(candidates),
        'dispatches': candidates,
        'next_action': next_action,
    }


def _inflight_blocks_slot_release(state, task_id):
    inflight = state.get('inflight')
    if inflight is None:
        return False
    if not isinstance(inflight, dict):
        return True
    inflight_task = inflight.get('task')
    if not isinstance(inflight_task, str) or not inflight_task.strip():
        return True
    return inflight_task.strip() in {'all', task_id}


def release_execution_slot_for_report(
    project, state, task_id, status, session_id,
):
    """Release only a terminal worker's slot after its report is durable."""
    if status not in {'DONE', 'BLOCKED', 'CHANGE_REQUEST'}:
        return {'released': False, 'reason': 'non-terminal-report'}
    if _inflight_blocks_slot_release(state, task_id):
        inflight = state.get('inflight')
        inflight_task = (
            inflight.get('task') if isinstance(inflight, dict) else None
        )
        return {
            'released': False,
            'reason': 'unresolved-inflight',
            'inflight_task': inflight_task,
        }
    registry = load_registry(project, state, create=False)
    mapping = (registry.get('tasks') or {}).get(task_id) or {}
    dispatch_id = mapping.get('dispatch_id')
    if not dispatch_id:
        return {'released': False, 'reason': 'legacy-without-dispatch'}
    data = load_dispatch(project, state)
    row = next(
        (
            item for item in data.get('dispatches', [])
            if item.get('id') == dispatch_id
            and item.get('status') != 'superseded'
        ),
        None,
    )
    if row is None:
        raise WorkflowError(
            'SESSION_REPORT_DISPATCH_NOT_FOUND ' + str(task_id)
        )
    dispatch_ref = row.get('dispatch_ref') or {}
    if (
        row.get('task') != task_id
        or not isinstance(dispatch_ref, dict)
        or not dispatch_ref.get('uid')
    ):
        raise WorkflowError('SESSION_REPORT_SLOT_AUTHORITY_INVALID')
    request_uid = row.get('execution_slot_request_uid')
    owner = row.get('execution_slot_owner')
    if request_uid is None and owner is None:
        return {'released': False, 'reason': 'legacy-without-slot'}
    if not request_uid or not isinstance(owner, dict):
        raise WorkflowError('SESSION_REPORT_SLOT_AUTHORITY_INVALID')
    if (
        owner.get('work_uid') != state.get('work_uuid')
        or owner.get('session_uid') != dispatch_ref.get('uid')
    ):
        raise WorkflowError('SESSION_REPORT_SLOT_AUTHORITY_INVALID')
    try:
        slot = ResourceRegistry(project.root).release_execution_slot(
            request_uid,
            owner,
            row.get('execution_slot_identity'),
        )
    except ResourceRegistryError as exc:
        raise WorkflowError(
            'SESSION_REPORT_SLOT_RELEASE_FAILED ' + str(exc)
        ) from exc
    changed = (
        row.get('execution_slot_state') != slot['state']
        or row.get('execution_slot_released_at') is None
    )
    row['execution_slot_state'] = slot['state']
    if changed:
        row['execution_slot_released_at'] = now()
        row['execution_slot_released_by'] = session_id
        row['execution_slot_release_reason'] = status
        row['updated_at'] = row['execution_slot_released_at']
        _save_dispatch(project, state, data)
    return {
        'released': True,
        'request_uid': request_uid,
        'identity': slot['resources'][0]['identity'],
        'state': slot['state'],
    }



def accept_session_task(project, state, args, caller_binding=None):
    """Accept one completed worker report and trigger owned cleanup."""
    registry = load_registry(project, state)
    controller = next((row for row in registry.get('sessions', [])
                       if row.get('id') == args.session), None)
    if not controller or controller.get('role') != 'controller' or registry.get('controller_session') != args.session:
        raise WorkflowError('CONTROLLER_LEASE_REQUIRED')
    require_row_capability(project, controller, 'accept', state.get('work_uuid'))
    from .sessions import require_actor_binding
    require_actor_binding(caller_binding, controller, 'accept', state.get('work_uuid'))
    task_id = args.task
    data = load_dispatch(project, state)
    rows = [row for row in data.get('dispatches', [])
            if row.get('task') == task_id and row.get('status') != 'superseded']
    if not rows:
        raise WorkflowError('ACCEPTANCE_DISPATCH_NOT_FOUND ' + task_id)
    row = rows[-1]
    mapping = registry.get('tasks', {}).get(task_id) or {}
    if mapping.get('acceptance_status') == 'accepted' or row.get('status') in {'accepted', 'cleaned'}:
        cleanup = next((item for item in reversed(data.get('cleanup', []))
                        if item.get('dispatch_id') == row.get('id')), None)
        if cleanup is None and mapping.get('cleanup_status') == 'deferred':
            cleanup = {'result': 'deferred', 'worktree_state': 'accepted',
                       'dispatch_id': row.get('id')}
        return {'task': state['id'], 'plan_task': task_id, 'acceptance': 'accepted',
                'cleanup': cleanup or {'result': row.get('worktree_state')}, 'idempotent': True}
    if mapping.get('report_status') != 'DONE':
        raise WorkflowError('ACCEPTANCE_REPORT_REQUIRED ' + task_id)
    task = next((item for item in state.get('tasks', []) if item.get('id') == task_id), None)
    if not task or task.get('status') not in {'done', 'accepted', 'integrated', 'cleaned'}:
        raise WorkflowError('ACCEPTANCE_TASK_INCOMPLETE ' + task_id)
    reporter = mapping.get('report_session')
    if not reporter:
        raise WorkflowError('ACCEPTANCE_REPORT_SESSION_REQUIRED ' + task_id)
    worker = next((item for item in registry.get('sessions', []) if item.get('id') == reporter), None)
    if not worker or worker.get('task') not in (None, task_id):
        raise WorkflowError('ACCEPTANCE_SESSION_MISMATCH ' + task_id)
    if worker.get('worktree') and row.get('worktree') and Path(worker['worktree']).resolve() != Path(row['worktree']).resolve():
        raise WorkflowError('ACCEPTANCE_WORKTREE_MISMATCH ' + task_id)

    row['status'] = 'accepted'
    row['worktree_state'] = 'accepted'
    row['accepted_at'] = now()
    row['accepted_by'] = args.session
    row['updated_at'] = now()
    mapping['acceptance_status'] = 'accepted'
    mapping['accepted_at'] = row['accepted_at']
    mapping['accepted_by'] = args.session
    registry.setdefault('tasks', {})[task_id] = mapping
    registry.setdefault('events', []).append({
        'id': uuid.uuid4().hex, 'at': now(), 'event': 'task-accepted',
        'session': args.session, 'detail': redact(task_id),
    })
    save_registry(project, state, registry)

    from .sessions import release_session_lease
    release_session_lease(project, state, reporter, args.reason)
    if _integration_task(state) is not None:
        mapping['cleanup_status'] = 'deferred'
        mapping['cleanup_error'] = None
        row['cleanup_status'] = 'deferred'
        registry = load_registry(project, state)
        registry.setdefault('tasks', {}).setdefault(task_id, {}).update(mapping)
        _save_dispatch(project, state, data)
        save_registry(project, state, registry)
        return {
            'task': state['id'], 'plan_task': task_id, 'acceptance': 'accepted',
            'cleanup': {'result': 'deferred', 'worktree_state': 'accepted',
                        'dispatch_id': row['id']},
            'idempotent': False,
        }
    cleanup_result = cleanup_worktree(project, row, args.reason)
    row.update(cleanup_result)
    cleanup_record = _cleanup_record(row, cleanup_result, args.reason)
    data.setdefault('cleanup', []).append(cleanup_record)
    if cleanup_result.get('result') in {'removed', 'released'}:
        row['status'] = 'cleaned'
        row['worktree_state'] = cleanup_result.get('worktree_state')
        mapping['cleanup_status'] = cleanup_result.get('result')
    else:
        row['status'] = 'accepted'
        mapping['cleanup_status'] = 'cleanup-pending'
    mapping['cleanup_error'] = cleanup_result.get('cleanup_error')
    registry = load_registry(project, state)
    registry.setdefault('tasks', {}).setdefault(task_id, {}).update(mapping)
    _save_dispatch(project, state, data)
    save_registry(project, state, registry)
    return {
        'task': state['id'], 'plan_task': task_id, 'acceptance': 'accepted',
        'cleanup': cleanup_record, 'idempotent': False,
    }

def session_progress(project, state):
    data = load_dispatch(project, state)
    registry = load_registry(project, state)
    tasks = list(state.get('tasks') or [])
    accepted_ids = _accepted_task_ids(state, data)
    reports_done = len({task for task, row in (registry.get('tasks') or {}).items() if row.get('report_status') == 'DONE'})
    blocked_ids = {task.get('id') for task in tasks if task.get('status') == 'blocked'}
    change_requests = 0
    for task, row in (registry.get('tasks') or {}).items():
        if row.get('report_status') == 'BLOCKED':
            blocked_ids.add(task)
        if row.get('report_status') == 'CHANGE_REQUEST':
            change_requests += 1
    cleaned = len([row for row in data.get('cleanup', []) if row.get('result') in CLEANUP_RESULTS])
    manual_required = len([row for row in data.get('dispatches', []) if row.get('status') == 'manual-required'])
    running = len([row for row in data.get('dispatches', []) if row.get('status') == 'running'])
    latest_dispatches = {}
    for row in data.get('dispatches', []):
        if row.get('status') != 'superseded':
            latest_dispatches[row.get('task')] = row
    managed_worktrees = sum(
        1 for row in latest_dispatches.values()
        if row.get('managed_worktree') and row.get('worktree_state') not in {'removed', 'released'}
    )
    bound_worktrees = sum(
        1 for row in latest_dispatches.values()
        if not row.get('managed_worktree') and row.get('worktree_state') not in {'removed', 'released'}
    )
    cleanup_pending_worktrees = sum(
        1 for row in latest_dispatches.values()
        if row.get('worktree_state') == 'cleanup-pending'
    )
    active_tasks = {row.get('task') for row in data.get('dispatches', []) if row.get('status') in ACTIVE_DISPATCH}
    next_tasks = []
    for task in tasks:
        task_id = task.get('id')
        if task_id in accepted_ids or task_id in active_tasks or task_id in blocked_ids:
            continue
        if all(dep in accepted_ids for dep in task.get('depends') or []):
            next_tasks.append(task_id)
    total = len(tasks)
    percent = round((len(accepted_ids) / total * 100) if total else 0.0, 1)
    phases = []
    for task in tasks:
        task_id = task.get('id')
        row = next((item for item in reversed(data.get('dispatches', [])) if item.get('task') == task_id), None)
        status = 'accepted' if task_id in accepted_ids else (row.get('status') if row else ('blocked' if task_id in blocked_ids else 'planned'))
        phases.append({'task': task_id, 'title': task.get('title'), 'status': status, 'depends': task.get('depends') or [], 'next': 'ready' if task_id in next_tasks else ''})
    return {
        'task': state['id'],
        'overall_progress': percent,
        'planned_tasks': total,
        'dispatches_total': len(data.get('dispatches', [])),
        'distinct_tasks': len({row.get('task') for row in data.get('dispatches', [])}),
        'reports_done': reports_done,
        'accepted_tasks': len(accepted_ids),
        'cleaned_worktrees': cleaned,
        'managed_worktrees': managed_worktrees,
        'bound_worktrees': bound_worktrees,
        'cleanup_pending_worktrees': cleanup_pending_worktrees,
        'running_sessions': running,
        'blocked_tasks': len(blocked_ids),
        'change_requests': change_requests,
        'manual_required': manual_required,
        'next_tasks': next_tasks,
        'phases': phases,
        'controller_session': registry.get('controller_session'),
    }


def watch_session_events(project, state, args, caller_binding=None):
    registry = load_registry(project, state)
    controller = next(
        (row for row in registry.get('sessions', [])
         if row.get('id') == registry.get('controller_session')),
        None,
    )
    if controller is None:
        raise WorkflowError('CONTROLLER_LEASE_REQUIRED')
    require_row_capability(project, controller, 'report', state.get('work_uuid'))
    from .sessions import require_actor_binding
    require_actor_binding(
        caller_binding, controller, 'report', state.get('work_uuid'),
    )
    adapter = getattr(args, 'adapter', None) or 'manual'
    limit = int(getattr(args, 'limit', 20) or 20)
    if limit < 1:
        raise WorkflowError('WATCH_LIMIT_INVALID')
    event_id = getattr(args, 'event_id', None)
    pending = [event for event in registry.get('outbox', []) if not event.get('delivered') and (not event_id or event.get('id') == event_id)]
    selected = pending[:limit]
    delivered = 0
    notifications = []
    for event in selected:
        notification = {key: event.get(key) for key in ('id', 'task', 'session', 'status', 'report')}
        if adapter == 'manual':
            event['delivered'] = True
            event['delivered_at'] = now()
            event['delivery'] = 'manual'
            delivered += 1
            notification['delivery'] = 'manual'
        else:
            notification['delivery'] = 'adapter-required'
        notifications.append(notification)
    if delivered:
        save_registry(project, state, registry)
    return {
        'task': state['id'],
        'adapter': adapter,
        'delivered': delivered,
        'pending': len(pending) - delivered,
        'notifications': notifications,
    }


def clean_session_worktree(project, state, args, caller_binding=None):
    registry = load_registry(project, state)
    controller = next(
        (row for row in registry.get('sessions', [])
         if row.get('id') == registry.get('controller_session')),
        None,
    )
    if controller is None:
        raise WorkflowError('CONTROLLER_LEASE_REQUIRED')
    require_row_capability(project, controller, 'accept', state.get('work_uuid'))
    from .sessions import require_actor_binding
    require_actor_binding(
        caller_binding, controller, 'accept', state.get('work_uuid'),
    )
    data = load_dispatch(project, state)
    task_id = args.task
    rows = [row for row in data.get('dispatches', [])
            if row.get('task') == task_id and row.get('status') != 'superseded']
    if not rows:
        raise WorkflowError('CLEANUP_DISPATCH_NOT_FOUND ' + task_id)
    row = rows[-1]
    task = next((item for item in state.get('tasks', []) if item.get('id') == task_id), None)
    if not task or task.get('status') not in {'done', 'accepted', 'integrated', 'cleaned'}:
        if row.get('status') not in {'accepted', 'integrated'}:
            raise WorkflowError('CLEANUP_ACCEPTANCE_REQUIRED ' + task_id)
    retry = bool(getattr(args, 'retry', False))
    if row.get('managed_worktree') and not retry:
        raise WorkflowError('CLEANUP_RETRY_REQUIRED use --retry for a managed worktree')
    if retry:
        existing = next((item for item in reversed(data.get('cleanup', []))
                         if item.get('dispatch_id') == row.get('id')
                         and item.get('result') in {'removed', 'released'}), None)
        if existing:
            return {'task': state['id'], 'cleanup': existing, 'idempotent': True}
        if caller_binding is not None:
            from .resource_operations import ensure_work_evidence
            ensure_work_evidence(project,state,binding=caller_binding)
        cleanup_result = cleanup_worktree(project, row, args.reason, retry=True)
        row.update(cleanup_result)
        cleanup = {
            'id': uuid.uuid4().hex,
            'dispatch_id': row['id'],
            'task': task_id,
            'result': cleanup_result.get('result'),
            'worktree': row.get('worktree'),
            'reason': redact(args.reason),
            'attempt': cleanup_result.get('cleanup_attempts', 1),
            'error': cleanup_result.get('cleanup_error'),
            'cleanup_error': cleanup_result.get('cleanup_error'),
            'at': now(),
        }
        data.setdefault('cleanup', []).append(cleanup)
        if cleanup_result.get('result') in {'removed', 'released'}:
            row['status'] = 'cleaned'
        else:
            row['status'] = 'accepted'
        row['updated_at'] = now()
        _save_dispatch(project, state, data)
        registry = load_registry(project, state)
        mapping = registry.setdefault('tasks', {}).setdefault(task_id, {})
        mapping['cleanup_status'] = cleanup_result.get('result')
        mapping['cleanup_error'] = cleanup_result.get('cleanup_error')
        save_registry(project, state, registry)
        return {'task': state['id'], 'cleanup': cleanup, 'idempotent': cleanup_result.get('idempotent', False)}
    result = args.result
    if result not in CLEANUP_RESULTS:
        raise WorkflowError('CLEANUP_RESULT_INVALID')
    existing = next((item for item in data.get('cleanup', [])
                     if item.get('dispatch_id') == row.get('id')
                     and item.get('result') in CLEANUP_RESULTS), None)
    if existing:
        return {'task': state['id'], 'cleanup': existing, 'idempotent': True}
    cleanup = {
        'id': uuid.uuid4().hex,
        'dispatch_id': row['id'],
        'task': task_id,
        'result': result,
        'worktree': row.get('worktree'),
        'reason': redact(args.reason),
        'at': now(),
    }
    data.setdefault('cleanup', []).append(cleanup)
    row['status'] = 'cleaned'
    row['updated_at'] = now()
    _save_dispatch(project, state, data)
    registry = load_registry(project, state)
    registry.setdefault('tasks', {}).setdefault(task_id, {})['cleanup_status'] = result
    save_registry(project, state, registry)
    return {'task': state['id'], 'cleanup': cleanup}
