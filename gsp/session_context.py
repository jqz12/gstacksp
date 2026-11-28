"""Durable session context bindings and capability checks.

Bindings describe who may act on one work.  They intentionally contain no
credentials or process handles.  The small JSON handle is suitable for a brief
or hook environment because it references the authoritative SQLite registry
without duplicating secrets.
"""
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import subprocess
import uuid

from .identity_store import IdentityStore, IdentityStoreError
from .orchestration_types import RequestRef, SessionBinding, digest_value
from .core import (WorkflowError, atomic, inside, now)
from .git_operations import (visible_git_marker)
from .workflow_support import (slug)


ACTION_CAPABILITIES = {
    'accept': 'accept',
    'brief': 'brief',
    'claim': 'claim',
    'dispatch': 'dispatch',
    'handoff': 'handoff',
    'plan': 'plan',
    'report': 'report',
    'write': 'write',
}
LEGACY_KINDS = {'main', 'child', 'helper', 'integration', 'observer'}
NATIVE_KINDS = {
    'main_chat', 'independent_worker', 'native_executor', 'helper', 'reviewer',
}
KINDS = LEGACY_KINDS | NATIVE_KINDS
CONTROLLER_KINDS = frozenset({'main', 'main_chat'})
KNOWN_HOSTS = {'codex', 'claude', 'zcode'}
HANDLE_SCHEMA = 1
KIND_DEFAULT_CAPABILITIES = {
    'main_chat': frozenset({
        'accept', 'brief', 'claim', 'dispatch', 'handoff', 'plan', 'report',
        'write',
    }),
    'independent_worker': frozenset({'brief', 'claim', 'report', 'write'}),
    'native_executor': frozenset({'brief', 'claim', 'report', 'write'}),
    'helper': frozenset({'claim', 'report', 'write'}),
    'reviewer': frozenset({'report'}),
}
NATIVE_MODEL_STATUSES = frozenset({'actual', 'unsupported', 'unknown'})
EXECUTOR_KINDS = frozenset({'independent_worker', 'native_executor'})


@dataclass(frozen=True)
class NativeModelObservation:
    """Independent facts about the request, host route and provider backend."""

    requested_model: str | None
    requested_status: str
    host_selected_route: str | None
    host_selected_status: str
    provider_backend: str | None
    provider_backend_status: str
    effort: str | None
    native_turn_id: str | None
    observed_at: str

    def __post_init__(self):
        for name in (
            'requested_status', 'host_selected_status',
            'provider_backend_status',
        ):
            if getattr(self, name) not in NATIVE_MODEL_STATUSES:
                _error('NATIVE_MODEL_STATUS_INVALID ' + name)
        for name in (
            'requested_model', 'host_selected_route', 'provider_backend',
            'effort', 'native_turn_id',
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                _error('NATIVE_MODEL_OBSERVATION_INVALID ' + name)
        if not isinstance(self.observed_at, str) or not self.observed_at.strip():
            _error('NATIVE_MODEL_OBSERVATION_INVALID observed_at')

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            _error('NATIVE_MODEL_OBSERVATION_INVALID')
        expected = {
            'requested_model', 'requested_status', 'host_selected_route',
            'host_selected_status', 'provider_backend',
            'provider_backend_status', 'effort', 'native_turn_id',
            'observed_at',
        }
        if set(value) != expected:
            _error('NATIVE_MODEL_OBSERVATION_INVALID fields')
        return cls(**value)


def _error(prefix, detail=''):
    raise WorkflowError((prefix + (' ' + detail if detail else '')).strip())


def canonical_binding_digest(binding):
    """Digest immutable identity, role, host and capability facts."""
    canonical = {
        'binding_uid': binding.binding_uid,
        'session_uid': binding.session.uid,
        'work_uid': binding.work_uid,
        'kind': binding.kind,
        'host': binding.host,
        'host_thread_id': binding.host_thread_id,
        'host_state': binding.host_state,
        'capabilities': sorted(binding.capabilities),
        'project_uid': binding.project_uid,
        'common_dir': binding.common_dir,
        'handle_path': binding.handle_path,
    }
    return digest_value(canonical)


def binding_state(binding):
    return {
        'controller_generation': binding.controller_generation,
        'cancel_generation': binding.cancel_generation,
        'cancelled': binding.cancelled,
    }


def binding_with_state(binding, state):
    value = binding.to_dict()
    value.update({
        'controller_generation': int(state['controller_generation']),
        'cancel_generation': int(state['cancel_generation']),
        'cancelled': bool(state['cancelled']),
    })
    return SessionBinding.from_dict(value)


def record_binding_authoritative(store, binding):
    """Persist immutable binding facts and initial mutable state."""
    store.record_session_binding(binding.binding_uid, canonical_binding_digest(binding))
    store.record_session_state(
        binding.binding_uid,
        binding.controller_generation,
        binding.cancel_generation,
        binding.cancelled,
    )


def _binding_path(store, session, binding_uid):
    root = store.root / '.gsp' / 'runs' / 'session-context' / session.uid
    return root / (binding_uid + '.json')


def _relative(store, path):
    try:
        return Path(path).resolve().relative_to(store.root).as_posix()
    except ValueError as exc:
        raise WorkflowError('SESSION_CONTEXT_PATH_ESCAPE') from exc


def _project_context(store):
    try:
        store.ensure_authority()
        project_uid = store.project_uid()
    except IdentityStoreError as exc:
        _error(str(exc))
    identity = store.root / '.gsp' / 'project.json'
    try:
        value = json.loads(identity.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        _error('PROJECT_IDENTITY_INVALID')
    if value.get('id') != project_uid:
        _error('SESSION_PROJECT_MISMATCH identity store and project file differ')
    try:
        common = subprocess.run(
            ['git', '-C', str(store.root), 'rev-parse', '--git-common-dir'],
            capture_output=True, text=True, encoding='utf-8', timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
    except OSError as exc:
        if visible_git_marker(store.root):
            _error('SESSION_COMMON_DIR_UNVERIFIED ' + str(exc))
        return project_uid, 'non-git:' + project_uid
    if common.returncode:
        if not visible_git_marker(store.root):
            # True non-Git projects retain the existing GSP workflow. A broken
            # Git repository is never downgraded into this mode.
            return project_uid, 'non-git:' + project_uid
        _error('SESSION_COMMON_DIR_UNVERIFIED ' + common.stderr.strip())
    raw = common.stdout.strip()
    if not raw:
        _error('SESSION_COMMON_DIR_UNVERIFIED empty git response')
    common_dir = (store.root / raw).resolve()
    return project_uid, common_dir


def _resolve_work(store, work_uid):
    if not isinstance(work_uid, str) or not work_uid.strip():
        _error('WORK_UID_REQUIRED')
    try:
        return store.resolve('work', work_uid, None, write=True)
    except IdentityStoreError as exc:
        if str(exc).startswith(('ID_NOT_FOUND', 'ID_NOT_CANONICAL')):
            _error('WORK_UID_MISMATCH ' + work_uid)
        _error(str(exc))


def _create_binding(
    store, work_uid, kind, host, host_thread_id, capabilities,
    context_path=None, request=None, binding_uid=None,
):
    project_uid, common_dir = _project_context(store)
    work = _resolve_work(store, work_uid)
    if work.project_uid != project_uid:
        _error('SESSION_PROJECT_MISMATCH work belongs to another project')
    if kind not in KINDS:
        _error('SESSION_BINDING_KIND_INVALID ' + str(kind))
    if not isinstance(host, str) or not host.strip():
        _error('SESSION_HOST_REQUIRED')
    if host_thread_id is not None and (
        not isinstance(host_thread_id, str) or not host_thread_id.strip()
    ):
        _error('SESSION_HOST_THREAD_INVALID')
    if isinstance(capabilities, str):
        _error('SESSION_CAPABILITIES_INVALID')
    try:
        caps = frozenset(capabilities or ())
    except TypeError as exc:
        raise WorkflowError('SESSION_CAPABILITIES_INVALID') from exc
    if any(not isinstance(item, str) or not item.strip() for item in caps):
        _error('SESSION_CAPABILITIES_INVALID')
    caps = frozenset(item.strip() for item in caps)
    if kind == 'helper' and 'accept' in caps:
        _error('SESSION_HELPER_CANNOT_ACCEPT')
    if kind == 'observer' and caps.intersection({'accept', 'claim', 'dispatch', 'handoff', 'report', 'write'}):
        _error('SESSION_OBSERVER_CANNOT_WRITE')
    main_state = _state_for_store_work(store, work.uid)[1] if kind in CONTROLLER_KINDS else None

    binding_uid = str(binding_uid or uuid.uuid4().hex)
    if len(binding_uid) < 16 or any(
        char not in '0123456789abcdef' for char in binding_uid.lower()
    ):
        _error('SESSION_BINDING_UID_INVALID')
    if request is None:
        logical_key = 'session-binding:' + binding_uid
        request = RequestRef(
            request_uid=digest_value({
                'project_uid': project_uid,
                'logical_key': logical_key,
                'operation_kind': 'session-binding',
            }),
            logical_key=logical_key,
            input_digest=digest_value({
                'work_uid': work.uid,
                'kind': kind,
                'host': host.strip(),
                'host_thread_id': host_thread_id,
                'capabilities': sorted(caps),
                'common_dir': str(common_dir),
            }),
            project_uid=project_uid,
            work_uid=work.uid,
            attempt_uid=None,
            operation_kind='session-binding',
        )
    elif not isinstance(request, RequestRef):
        _error('SESSION_BINDING_REQUEST_INVALID')
    elif (
        request.project_uid != project_uid
        or request.work_uid != work.uid
        or request.operation_kind not in {
            'session-binding', 'initial-controller', 'delegate-binding',
        }
    ):
        _error('SESSION_BINDING_REQUEST_MISMATCH')
    try:
        session = store.allocate('session', None, request)
    except IdentityStoreError as exc:
        _error(str(exc))
    if context_path is None:
        handle = _binding_path(store, session, binding_uid)
    else:
        handle = Path(context_path)
        if not handle.is_absolute():
            _error('SESSION_CONTEXT_PATH_REQUIRED use an absolute context path')
        handle = handle.resolve()
        if not inside(store.root, handle):
            _error('SESSION_CONTEXT_PATH_ESCAPE context-out must stay in the project')
    binding = SessionBinding(
        binding_uid=binding_uid,
        session=session,
        work_uid=work.uid,
        kind=kind,
        host=host.strip(),
        host_thread_id=host_thread_id,
        host_state='unverified',
        capabilities=caps,
        controller_generation=1,
        cancel_generation=1,
        cancelled=False,
        created_at=now(),
        project_uid=project_uid,
        common_dir=str(common_dir),
        handle_path=_relative(store, handle),
    )
    if handle.exists():
        existing = load_binding(handle)
        if canonical_binding_digest(existing) != canonical_binding_digest(binding):
            _error('SESSION_BINDING_CONFLICT context-out already belongs to another binding')
        if existing.cancelled:
            _error('SESSION_BINDING_STALE')
        binding = existing
    else:
        payload = {'schema': HANDLE_SCHEMA, 'binding': binding.to_dict()}
        handle.parent.mkdir(parents=True, exist_ok=True)
        # Store immutable facts before publishing the handle. Exact replay keeps
        # its original timestamps and never resets authoritative generations.
        record_binding_authoritative(store, binding)
        atomic(handle, payload)
    if kind in CONTROLLER_KINDS:
        try:
            store.claim_initial_work_controller(work.uid, main_state['id'], binding_uid)
        except IdentityStoreError as exc:
            _error(str(exc))
    return binding


def create_binding(store, work_uid, kind, host, host_thread_id, capabilities):
    return _create_binding(
        store, work_uid, kind, host, host_thread_id, capabilities,
    )


def _state_for_store_work(store, work_uid):
    if not isinstance(work_uid, str) or not work_uid.strip():
        _error('WORK_UID_REQUIRED')
    try:
        controller = store.read_work_controller(work_uid)
    except IdentityStoreError as exc:
        _error(str(exc))
    if controller is not None:
        state_file = store.root / '.gsp' / 'runs' / controller['run_id'] / 'state.json'
        try:
            value = json.loads(state_file.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError) as exc:
            raise WorkflowError('WORK_UID_MISMATCH controller work is unavailable') from exc
        if value.get('work_uuid') != work_uid or value.get('id') != controller['run_id']:
            _error('WORK_UID_MISMATCH controller work differs')
        return state_file, value
    matches = []
    runs = store.root / '.gsp' / 'runs'
    for state_file in sorted(runs.glob('*/state.json')) if runs.is_dir() else ():
        try:
            value = json.loads(state_file.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            continue
        if value.get('work_uuid') == work_uid:
            identifier = value.get('id')
            if not isinstance(identifier, str):
                _error('WORK_UID_MISMATCH invalid work identifier')
            slug(identifier)
            if os.path.normcase(identifier) != os.path.normcase(state_file.parent.name):
                _error('WORK_UID_MISMATCH state identifier and directory disagree')
            matches.append((state_file, value))
    if len(matches) != 1:
        _error('WORK_UID_MISMATCH expected one initialized work')
    return matches[0]


def _read_registry_readonly(store, state_file, state):
    path = state_file.parent / 'sessions' / 'index.json'
    if not path.is_file():
        return {
            'controller_session': None, 'sessions': [],
            'workspace_leases': {}, 'task': state.get('id'),
        }
    try:
        registry = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        _error('SESSION_REGISTRY_INVALID')
    if (
        not isinstance(registry, dict)
        or registry.get('task') != state.get('id')
        or not isinstance(registry.get('sessions', []), list)
        or not isinstance(registry.get('workspace_leases', {}), dict)
    ):
        _error('SESSION_REGISTRY_INVALID')
    return registry


def legacy_read_only_placeholder(registry, row):
    """Recognize only an explicit legacy row with no identity or real lease."""
    return (
        row.get('context_mode') == 'legacy-read-only'
        and not any(row.get(key) for key in (
            'context', 'binding_uid', 'controller_generation', 'cancel_generation',
        ))
        and row.get('id') not in (registry.get('workspace_leases') or {}).values()
    )


def _active_controller(registry):
    controller_id = registry.get('controller_session')
    if not controller_id:
        return None
    active = {'active', 'waiting', 'reported'}
    row = next(
        (item for item in registry.get('sessions', [])
         if item.get('id') == controller_id),
        None,
    )
    if row is None or row.get('role') != 'controller' or row.get('status') not in active:
        return None
    if legacy_read_only_placeholder(registry, row):
        return None
    return row


def _load_authoritative_binding(store, binding):
    if not isinstance(binding, SessionBinding):
        _error('SESSION_BINDING_REQUIRED')
    try:
        immutable = store.read_session_binding(binding.binding_uid)
        state = store.read_session_state(binding.binding_uid)
    except IdentityStoreError as exc:
        _error(str(exc))
    if immutable is None or immutable != canonical_binding_digest(binding):
        _error('SESSION_CONTEXT_TAMPERED')
    if state is None or state != binding_state(binding):
        _error('SESSION_BINDING_STALE')
    if state['cancelled']:
        _error('SESSION_BINDING_STALE')
    return binding_with_state(binding, state)


def establish_initial_controller(
    store, request, work_uid, host, host_thread_id, capabilities,
    expected_controller_generation,
):
    """Establish one idempotent first controller binding for an existing work."""
    if not isinstance(store, IdentityStore):
        _error('IDENTITY_STORE_REQUIRED')
    if not isinstance(request, RequestRef):
        _error('REQUEST_REQUIRED')
    if (
        request.operation_kind != 'initial-controller'
        or request.work_uid != work_uid
        or request.project_uid != store.project_uid()
    ):
        _error('CONTROLLER_REQUEST_MISMATCH')
    if type(expected_controller_generation) is not int or expected_controller_generation != 1:
        _error('CONTROL_VERSION_CHANGED')
    if isinstance(capabilities, str):
        _error('SESSION_CAPABILITIES_INVALID')
    caps = frozenset(capabilities or ())
    if 'write' not in caps:
        _error('SESSION_WRITE_REQUIRED')
    _resolve_work(store, work_uid)
    state_file, state = _state_for_store_work(store, work_uid)
    registry = _read_registry_readonly(store, state_file, state)
    active = _active_controller(registry)
    deterministic_uid = digest_value({
        'request_uid': request.request_uid,
        'work_uid': work_uid,
        'kind': 'initial-controller',
    })[:32]
    try:
        owner = store.read_work_controller(work_uid)
    except IdentityStoreError as exc:
        _error(str(exc))
    if owner is not None:
        if owner['binding_uid'] != deterministic_uid:
            _error('CONTROLLER_LEASE_HELD ' + owner['binding_uid'])
        current_state = store.read_session_state(deterministic_uid)
        if current_state is None or current_state['cancelled']:
            _error('SESSION_BINDING_STALE')
        if current_state['controller_generation'] != expected_controller_generation:
            _error('CONTROL_VERSION_CHANGED')
    if active is not None:
        if (
            active.get('binding_uid') == deterministic_uid
            and int(active.get('controller_generation') or 0)
            == expected_controller_generation
        ):
            return load_binding(store.root / active['context'], _recover_once=True)
        _error('CONTROLLER_LEASE_HELD ' + str(active.get('id')))
    pending = {
        'schema': 1,
        'request': request.to_dict(),
        'work_uid': work_uid,
        'host': str(host or '').strip(),
        'host_thread_id': host_thread_id,
        'capabilities': sorted(caps),
        'expected_controller_generation': expected_controller_generation,
    }
    if not pending['host']:
        _error('SESSION_HOST_REQUIRED')
    try:
        store.record_controller_pending(request.request_uid, pending)
    except IdentityStoreError as exc:
        _error(str(exc))
    return _create_binding(
        store, work_uid, 'main_chat', host, host_thread_id, caps,
        request=request, binding_uid=deterministic_uid,
    )


def issue_delegate_binding(
    store, controller, kind, host, host_thread_id, capabilities,
    expected_controller_generation,
):
    """Issue a capability-limited delegate from the current controller."""
    require_decision_controller(store, controller, controller.work_uid)
    if controller.controller_generation != expected_controller_generation:
        _error('CONTROL_VERSION_CHANGED')
    allowed_kinds = {'independent_worker', 'native_executor', 'helper', 'reviewer'}
    if kind not in allowed_kinds:
        _error('SESSION_DELEGATE_KIND_INVALID ' + str(kind))
    if isinstance(capabilities, str):
        _error('SESSION_CAPABILITIES_INVALID')
    caps = frozenset(capabilities or ())
    if not caps <= controller.capabilities:
        _error('SESSION_CAPABILITY_ESCALATION')
    if not caps <= KIND_DEFAULT_CAPABILITIES[kind]:
        _error('SESSION_CAPABILITY_ESCALATION role ' + kind)
    if kind == 'helper' and 'accept' in caps:
        _error('SESSION_HELPER_CANNOT_ACCEPT')
    project_uid, common_dir = _project_context(store)
    logical_key = 'delegate-binding:' + digest_value({
        'controller_binding': controller.binding_uid,
        'controller_generation': controller.controller_generation,
        'work_uid': controller.work_uid,
        'kind': kind,
        'host': str(host or '').strip(),
        'host_thread_id': host_thread_id,
        'capabilities': sorted(caps),
    })
    request = RequestRef(
        request_uid=digest_value({
            'project_uid': project_uid,
            'logical_key': logical_key,
            'operation_kind': 'delegate-binding',
        }),
        logical_key=logical_key,
        input_digest=digest_value({
            'controller_binding': controller.binding_uid,
            'controller_generation': controller.controller_generation,
            'work_uid': controller.work_uid,
            'kind': kind,
            'host': str(host or '').strip(),
            'host_thread_id': host_thread_id,
            'capabilities': sorted(caps),
            'common_dir': str(common_dir),
        }),
        project_uid=project_uid,
        work_uid=controller.work_uid,
        attempt_uid=None,
        operation_kind='delegate-binding',
        controller_generation=controller.controller_generation,
        cancel_generation=controller.cancel_generation,
    )
    binding_uid = request.request_uid[:32]
    return _create_binding(
        store, controller.work_uid, kind, host, host_thread_id, caps,
        request=request, binding_uid=binding_uid,
    )


def require_decision_controller(store, binding, work_uid):
    """Require the actual active controller lease and current generations."""
    if not isinstance(store, IdentityStore):
        _error('IDENTITY_STORE_REQUIRED')
    current = _load_authoritative_binding(store, binding)
    if current.work_uid != work_uid:
        _error('WORK_UID_MISMATCH expected ' + str(work_uid))
    if current.kind not in CONTROLLER_KINDS:
        _error('SESSION_CONTROLLER_REQUIRED controller binding required')
    require_capability(current, 'plan', work_uid)
    try:
        owner = store.read_work_controller(work_uid)
    except IdentityStoreError as exc:
        _error(str(exc))
    if owner is not None:
        if owner['binding_uid'] != current.binding_uid:
            _error('SESSION_CONTROLLER_REQUIRED control lease is held by another binding')
        # Planning ownership is independent of an optional Snn source writer.
        # The current immutable identity and mutable generations were checked
        # above; the registry is a projection, not another grant of authority.
        return None
    state_file, state = _state_for_store_work(store, work_uid)
    registry = _read_registry_readonly(store, state_file, state)
    controller = _active_controller(registry)
    if controller is None:
        _error('CONTROLLER_LEASE_REQUIRED')
    if (
        controller.get('binding_uid') != current.binding_uid
        or controller.get('context') != current.handle_path
        or controller.get('work_uid') != work_uid
    ):
        _error('SESSION_CONTROLLER_REQUIRED active controller lease is held by another session')
    if (
        int(controller.get('controller_generation') or 0)
        != current.controller_generation
        or int(controller.get('cancel_generation') or 0)
        != current.cancel_generation
    ):
        _error('CONTROL_VERSION_CHANGED')
    workspace = controller.get('worktree')
    lease_owner = (registry.get('workspace_leases') or {}).get(
        os.path.normcase(str(Path(workspace).resolve()))
    ) if workspace else None
    if lease_owner != controller.get('id'):
        _error('SESSION_WRITE_LEASE_REQUIRED active controller does not hold the workspace lease')
    return None


def resume_pending_controller(store, request_uid, observed_host_identity):
    """Recover the exact first-controller intent after response loss."""
    if not isinstance(store, IdentityStore):
        _error('IDENTITY_STORE_REQUIRED')
    if not isinstance(observed_host_identity, dict):
        _error('NATIVE_HOST_IDENTITY_REQUIRED')
    try:
        pending = store.read_controller_pending(request_uid)
    except IdentityStoreError as exc:
        _error(str(exc))
    if pending is None:
        _error('CONTROLLER_PENDING_NOT_FOUND')
    host = str(observed_host_identity.get('host') or '').strip()
    host_thread_id = (
        observed_host_identity.get('host_thread_id')
        or observed_host_identity.get('thread_id')
    )
    if (
        pending.get('schema') != 1
        or host != pending.get('host')
        or host_thread_id != pending.get('host_thread_id')
    ):
        _error('CONTROLLER_PENDING_HOST_MISMATCH')
    try:
        request = RequestRef.from_dict(pending['request'])
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowError('CONTROLLER_PENDING_INVALID') from exc
    return establish_initial_controller(
        store,
        request,
        pending['work_uid'],
        pending['host'],
        pending.get('host_thread_id'),
        frozenset(pending.get('capabilities') or ()),
        pending.get('expected_controller_generation'),
    )


def _state_for_work(project, work_uid):
    return _state_for_store_work(IdentityStore(project.root), work_uid)[1]


def _legacy_controller_recovery(registry, owner, kind, reason):
    if kind != 'main_chat' or not str(reason or '').strip():
        _error('LEGACY_CONTROLLER_RECOVERY_REASON_REQUIRED')
    if owner is not None:
        _error('LEGACY_CONTROLLER_RECOVERY_BOUND')
    controller_id = registry.get('controller_session')
    projected = next((row for row in registry['sessions']
                      if row.get('id') == controller_id), None)
    if projected is None or projected.get('role') != 'controller':
        _error('LEGACY_CONTROLLER_RECOVERY_NOT_LEGACY')
    for row in registry['sessions']:
        if any(row.get(key) for key in ('binding_uid', 'context', 'context_binding',
                                        'controller_generation', 'cancel_generation')):
            _error('LEGACY_CONTROLLER_RECOVERY_BOUND')
        if row is not projected and row.get('status') in {'active', 'waiting', 'reported'}:
            _error('LEGACY_CONTROLLER_RECOVERY_OTHER_SESSION')
    if any(session != controller_id for session in
           (registry.get('workspace_leases') or {}).values()):
        _error('LEGACY_CONTROLLER_RECOVERY_OTHER_SESSION')
    return projected


def issue_cli_binding(project, args):
    """Issue one real CLI binding without consulting ``active.json``."""
    kind = str(args.kind)
    if kind not in KINDS:
        _error('SESSION_BINDING_KIND_INVALID ' + kind)
    state = _state_for_work(project, args.work_uid)
    capabilities = KIND_DEFAULT_CAPABILITIES.get(
        kind, KIND_DEFAULT_CAPABILITIES.get(
            {'main': 'main_chat', 'child': 'independent_worker',
             'integration': 'native_executor',
             'observer': 'reviewer'}.get(kind, ''),
            frozenset(),
        ),
    )
    if kind == 'helper':
        capabilities = capabilities - {'accept'}
    from .sessions import load_registry, require_controller_binding
    registry = load_registry(project, state, create=False)
    controller_id = registry.get('controller_session')
    projected = next((row for row in registry['sessions'] if row.get('id') == controller_id), None)
    legacy_placeholder = projected is not None and legacy_read_only_placeholder(registry, projected)
    caller = None
    caller_context = str(os.environ.get('GSP_SESSION_CONTEXT') or '').strip()
    owner = IdentityStore(project.root).read_work_controller(state['work_uuid'])
    recovery = None
    if getattr(args, 'recover_legacy_controller', False):
        recovery = _legacy_controller_recovery(
            registry, owner, kind, getattr(args, 'reason', None),
        )
        legacy_placeholder = True
    if owner is not None or (controller_id and not legacy_placeholder) or kind != 'main_chat':
        if not caller_context:
            raise WorkflowError(
                'SESSION_CONTROLLER_REQUIRED delegate issuance requires the '
                'active controller context'
            )
        def authorize_caller(binding, store):
            if store.root.resolve() != project.root.resolve():
                _error('SESSION_PROJECT_MISMATCH')
            require_capability(binding, 'plan', state['work_uuid'])
            require_controller_binding(project, state, binding)

        caller = load_binding(caller_context, before_recovery=authorize_caller)
        require_controller_binding(project, state, caller)
        require_capability(caller, 'plan', state['work_uuid'])
    context_out = Path(args.context_out)
    if not context_out.is_absolute():
        _error('SESSION_CONTEXT_PATH_REQUIRED use an absolute context path')
    if not inside(project.root, context_out.resolve()):
        _error('SESSION_CONTEXT_PATH_ESCAPE context-out must stay in the project')
    if recovery is not None:
        from .sessions import _event, _save
        if 'legacy_recovery_snapshot' not in recovery:
            recovery['legacy_recovery_snapshot'] = dict(recovery)
        recovery['context_mode'] = 'legacy-read-only'
        recovery['write_mode'] = 'read-only'
        recovery['status'] = 'superseded'
        recovery['updated_at'] = now()
        registry['workspace_leases'] = {}
        _event(registry, 'legacy-controller-recovery', recovery['id'],
               str(args.reason))
        _save(project, state, registry)
    binding = _create_binding(
        IdentityStore(project.root),
        state['work_uuid'],
        kind,
        args.host,
        args.thread,
        capabilities,
        context_path=context_out,
    )
    return {
        'task': state['id'],
        'work_uid': state['work_uuid'],
        'kind': kind,
        'binding_uid': binding.binding_uid,
        'session_uid': binding.session.uid,
        'handle_path': binding.handle_path,
        'context_out': str(context_out.resolve()),
        'requested_by': caller.binding_uid if caller else None,
    }


def load_binding(context_path, _recover_once=False, *, before_recovery=None):
    path = Path(context_path)
    if not path.is_absolute():
        _error('SESSION_CONTEXT_PATH_REQUIRED use an absolute context path')
    if not path.is_file():
        _error('SESSION_CONTEXT_REQUIRED ' + str(path))
    try:
        payload = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise WorkflowError('SESSION_CONTEXT_INVALID') from exc
    if not isinstance(payload, dict) or payload.get('schema') != HANDLE_SCHEMA:
        _error('SESSION_CONTEXT_INVALID')
    raw = payload.get('binding')
    if not isinstance(raw, dict):
        _error('SESSION_CONTEXT_INVALID')
    try:
        binding = SessionBinding.from_dict(raw)
    except (TypeError, ValueError, KeyError) as exc:
        raise WorkflowError('SESSION_CONTEXT_INVALID') from exc
    handle = Path(binding.handle_path)
    if handle.is_absolute() or '..' in handle.parts:
        _error('SESSION_CONTEXT_PATH_ESCAPE')
    expected = path.resolve()
    candidates = [parent for parent in expected.parents
                  if (parent / '.gsp/project.json').is_file()]
    root = next((parent for parent in candidates
                 if (parent / handle).resolve() == expected), None)
    if root is None:
        _error('SESSION_CONTEXT_PROJECT_MISSING')
    if not inside(root, expected):
        _error('SESSION_CONTEXT_PATH_ESCAPE')
    store = IdentityStore(root)
    project_uid, common_dir = _project_context(store)
    if binding.project_uid != project_uid:
        _error('SESSION_PROJECT_MISMATCH')
    binding_common = (Path(binding.common_dir).resolve()
                       if not str(binding.common_dir).startswith('non-git:') else binding.common_dir)
    if binding_common != common_dir:
        _error('SESSION_COMMON_DIR_MISMATCH')
    work = _resolve_work(store, binding.work_uid)
    if work.project_uid != project_uid:
        _error('SESSION_PROJECT_MISMATCH')
    try:
        session = store.resolve(
            'session', binding.session.uid, None, write=True,
        )
    except IdentityStoreError as exc:
        _error(str(exc))
    if session.uid != binding.session.uid:
        _error('SESSION_BINDING_STALE session identity changed')
    # Compare the editable handle against the authoritative canonical record.
    # Any tampering with kind, capabilities, host-state, generations or
    # cancellation is rejected here, before require_capability trusts it.
    try:
        authoritative = store.read_session_binding(binding.binding_uid)
    except IdentityStoreError as exc:
        _error(str(exc))
    if not authoritative:
        _error('SESSION_CONTEXT_INVALID missing authoritative binding record')
    if canonical_binding_digest(binding) != authoritative:
        _error('SESSION_CONTEXT_TAMPERED handle does not match authoritative binding')
    try:
        authoritative_state = store.read_session_state(binding.binding_uid)
    except IdentityStoreError as exc:
        _error(str(exc))
    if authoritative_state is None:
        _error('SESSION_CONTEXT_INVALID missing authoritative binding state')
    if before_recovery is not None:
        # Callers that know the requested work and actor must authorize against
        # current authority before an interrupted transition repairs any file.
        before_recovery(binding_with_state(binding, authoritative_state), store)
    if binding_state(binding) != authoritative_state:
        if _recover_once:
            _error('SESSION_CONTEXT_TAMPERED handle state does not match authoritative binding')
        from .sessions import recover_binding_handle
        binding = recover_binding_handle(root, binding)
        try:
            authoritative_state = store.read_session_state(binding.binding_uid)
        except IdentityStoreError as exc:
            _error(str(exc))
        if authoritative_state is None or binding_state(binding) != authoritative_state:
            _error('SESSION_CONTEXT_TAMPERED handle state does not match authoritative binding')
    return binding


def require_capability(binding, action, expected_work_uid):
    if not isinstance(binding, SessionBinding):
        _error('SESSION_BINDING_REQUIRED')
    if not isinstance(action, str) or action not in ACTION_CAPABILITIES:
        _error('SESSION_ACTION_INVALID ' + str(action))
    if not isinstance(expected_work_uid, str) or binding.work_uid != expected_work_uid:
        _error('WORK_UID_MISMATCH expected ' + str(expected_work_uid))
    if binding.cancelled:
        _error('SESSION_BINDING_STALE cancelled binding')
    if action not in binding.capabilities:
        _error('CAPABILITY_REQUIRED ' + action)
    if action == 'accept' and binding.kind not in CONTROLLER_KINDS | {'integration'}:
        _error('CAPABILITY_REQUIRED accept')


def require_native_model_role(
    binding, observation, role, expected_requested_model, expected_effort,
):
    """Require observed model facts for one native business role."""
    if not isinstance(binding, SessionBinding):
        _error('SESSION_BINDING_REQUIRED')
    if not isinstance(observation, NativeModelObservation):
        _error('NATIVE_MODEL_OBSERVATION_REQUIRED')
    role = str(role or '').strip()
    allowed_kinds = {
        'main_chat': {'main', 'main_chat'},
        'executor': EXECUTOR_KINDS,
        'helper': {'helper'},
        'reviewer': {'reviewer', 'observer'},
    }
    if role not in allowed_kinds:
        _error('NATIVE_MODEL_ROLE_INVALID ' + role)
    if binding.kind not in allowed_kinds[role]:
        _error('NATIVE_MODEL_ROLE_MISMATCH ' + binding.kind)
    if (
        observation.requested_status != 'actual'
        or observation.requested_model != expected_requested_model
    ):
        _error(
            'NATIVE_MODEL_REQUEST_MISMATCH expected '
            + str(expected_requested_model)
        )
    if (
        not isinstance(observation.effort, str)
        or observation.effort != expected_effort
    ):
        _error('NATIVE_MODEL_EFFORT_MISMATCH expected ' + str(expected_effort))
    if role == 'main_chat':
        return
    route = str(observation.host_selected_route or '').strip()
    if observation.host_selected_status != 'actual' or not route:
        _error('NATIVE_MODEL_ROUTE_UNVERIFIED')
    normalized_route = route.lower()
    if (
        normalized_route in {'gpt-6.1-sol', 'sol', 'gpt-6-sol'}
        or 'gpt-6.1-sol' in normalized_route
    ):
        _error('NATIVE_MODEL_ROUTE_INHERITED_SOL')
    # A native hook may expose that a provider field is unsupported or still
    # unknown. Claiming an exact backend requires a separately verified adapter;
    # the T04 observation surface cannot manufacture that proof.
    if observation.provider_backend_status == 'actual':
        _error('NATIVE_PROVIDER_BACKEND_UNSUPPORTED')


def record_native_model_observation(
    store, binding, request, observation, expected_controller_generation,
):
    """Persist one immutable native model observation under its request UID."""
    if not isinstance(store, IdentityStore):
        _error('IDENTITY_STORE_REQUIRED')
    if not isinstance(binding, SessionBinding):
        _error('SESSION_BINDING_REQUIRED')
    if not isinstance(request, RequestRef):
        _error('REQUEST_REQUIRED')
    if not isinstance(observation, NativeModelObservation):
        _error('NATIVE_MODEL_OBSERVATION_REQUIRED')
    if (
        not isinstance(expected_controller_generation, int)
        or expected_controller_generation < 1
        or binding.controller_generation != expected_controller_generation
    ):
        _error('CONTROL_VERSION_CHANGED')
    if (
        request.project_uid != store.project_uid()
        or request.work_uid != binding.work_uid
        or request.work_uid != _resolve_work(store, binding.work_uid).uid
    ):
        _error('REQUEST_SCOPE_MISMATCH')
    try:
        authoritative = store.read_session_binding(binding.binding_uid)
        state = store.read_session_state(binding.binding_uid)
    except IdentityStoreError as exc:
        _error(str(exc))
    if authoritative is None or authoritative != canonical_binding_digest(binding):
        _error('SESSION_CONTEXT_TAMPERED')
    if state is None or state != binding_state(binding) or state['cancelled']:
        _error('SESSION_BINDING_STALE')
    payload = observation.to_dict()
    try:
        stored = store.record_native_model_observation(
            request.request_uid, payload,
        )
    except IdentityStoreError as exc:
        _error(str(exc))
    return {
        'schema': 1,
        'request_uid': request.request_uid,
        'binding_uid': binding.binding_uid,
        'work_uid': binding.work_uid,
        'observation': stored,
        'digest': digest_value(stored),
    }


def binding_from_row(row):
    raw = row.get('context_binding')
    if not isinstance(raw, dict):
        _error('SESSION_CONTEXT_REQUIRED')
    try:
        return SessionBinding.from_dict(raw)
    except (TypeError, ValueError, KeyError) as exc:
        raise WorkflowError('SESSION_CONTEXT_INVALID') from exc


def row_context(project, row):
    value = row.get('context')
    if not value:
        _error('SESSION_CONTEXT_REQUIRED')
    path = Path(value)
    if not path.is_absolute():
        path = project.root / path
    return load_binding(path)


def require_row_capability(project, row, action, expected_work_uid):
    """Validate the row's current authoritative context and one action."""
    binding = row_context(project, row)
    require_capability(binding, action, expected_work_uid)
    if row.get('binding_uid') and row.get('binding_uid') != binding.binding_uid:
        _error('SESSION_BINDING_STALE registry binding changed')
    return binding
