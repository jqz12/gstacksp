"""Resolve the build root and environment for one real command.

The context is intentionally narrow: it carries only build variables that
can affect artifact reuse, never the full process environment.  The lease is
cooperative and covers GSP writers that honor this protocol.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys

from .build_policy import load_policy
from .hosts import identity, is_alive
from .runtime import WorkflowError, atomic, now, read


BUILD_LEASE_NAME = '.gsp-build-lease.json'
ENV_KEYS = (
    'CARGO_TARGET_DIR', 'BUILD_ROOT', 'CARGO_BUILD_TARGET',
    'CARGO_PROFILE', 'CARGO_PROFILE_DEV', 'CARGO_PROFILE_RELEASE', 'RUSTFLAGS',
)
KEY_ENV = (
    'CARGO_BUILD_TARGET', 'CARGO_PROFILE', 'CARGO_PROFILE_DEV',
    'CARGO_PROFILE_RELEASE', 'RUSTFLAGS', 'RUSTUP_TOOLCHAIN',
)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _file_digest(path):
    try:
        return _sha(Path(path).read_bytes())
    except OSError:
        return None


def key_inputs(project, environ=None):
    """Return the inputs that make a warm build root compatible."""
    environ = os.environ if environ is None else environ
    root = Path(project.root)
    cargo_lock = _file_digest(root / 'Cargo.lock')
    toolchain_file = None
    for name in ('rust-toolchain.toml', 'rust-toolchain'):
        toolchain_file = _file_digest(root / name)
        if toolchain_file:
            break
    flags = {key: environ.get(key) for key in KEY_ENV if environ.get(key)}
    return {
        'platform': sys.platform,
        'cargo_lock': cargo_lock,
        'toolchain_file': toolchain_file,
        'environment': {key: _sha(value.encode('utf-8')) for key, value in flags.items()},
    }


def compatibility_key(project, environ=None):
    return _sha(json.dumps(key_inputs(project, environ), sort_keys=True).encode('utf-8'))


def _dispatch_root(project, state, task_id):
    file = Path(project.meta) / 'runs' / state['id'] / 'sessions' / 'dispatch.json'
    try:
        data = read(file)
    except (OSError, WorkflowError):
        return None
    for row in data.get('dispatches', []):
        if row.get('task') != task_id or row.get('status') in {'superseded', 'cleaned'}:
            continue
        relative = row.get('build_root')
        if not relative:
            continue
        root = (Path(project.root) / relative).resolve()
        if root.is_dir() or not root.exists():
            return root
    return None


def _isolated_root(project, state, task_id):
    number = str(state.get('number') or state.get('id') or 'task')
    slug = state['id']
    task = next((row for row in state.get('tasks', []) if row.get('id') == task_id), None)
    topology = (task or {}).get('topology') or {}
    workspace = topology.get('workspace_root')
    parent = (state.get('git_topology') or {}).get('workspace_root')
    if workspace and parent and Path(workspace).resolve() != Path(parent).resolve():
        from .worktree_lifecycle import worktree_slug
        slug = worktree_slug(number, task)
    return (Path(project.root) / '.gsp' / 'build' / number / slug).resolve()


def _is_cargo(project, source_root, argv):
    for root in (Path(source_root), Path(project.root)):
        if (root / 'Cargo.toml').is_file():
            return True
    if argv:
        executable = Path(str(argv[0])).name.lower()
        if executable in {'cargo', 'cargo.exe'}:
            return True
    return False


def _no_injection(project, state, task_id, source_root, environ, provenance):
    return {
        'schema': 1, 'mode': 'off', 'root': None, 'env': {},
        'compatibility_key': compatibility_key(project, environ),
        'provenance': provenance, 'override_ambient': False,
        'owner': {'run': state.get('id'), 'task': task_id,
                  'workspace': str(Path(source_root).resolve())},
        'lease': None,
    }


def resolve_context(project, state, task_id, argv=None, environ=None, source_root=None):
    """Resolve the mode, root, compatibility inputs and lease path."""
    environ = dict(os.environ if environ is None else environ)
    policy = load_policy(project.root)
    cache = policy.get('build_cache') or {}
    mode = str(cache.get('mode') or 'isolated')
    if policy.get('worktree_build') == 'off' or mode == 'off':
        return {'schema': 1, 'mode': 'off', 'root': None, 'env': {},
                'compatibility_key': compatibility_key(project, environ),
                'provenance': 'policy', 'override_ambient': False,
                'owner': {'run': state.get('id'), 'task': task_id}}
    if mode not in {'isolated', 'reuse-exclusive'}:
        raise WorkflowError('BUILD_CONTEXT_INVALID unknown build_cache.mode ' + mode)
    if source_root is None:
        source_root = project.workspace_root(state, task_id=task_id)
    if mode == 'isolated' and not _is_cargo(project, source_root, argv):
        return _no_injection(project, state, task_id, source_root, environ, 'non-cargo')
    override = False
    if mode == 'reuse-exclusive':
        raw = cache.get('root')
        supplied = cache.get('context')
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            raise WorkflowError('BUILD_CONTEXT_INCOMPLETE reuse-exclusive needs an absolute root')
        if not isinstance(supplied, dict) or not supplied.get('compatibility_key'):
            raise WorkflowError('BUILD_CONTEXT_INCOMPLETE reuse-exclusive needs compatibility context')
        root = Path(raw).resolve()
        key = compatibility_key(project, environ)
        if supplied['compatibility_key'] != key:
            raise WorkflowError('BUILD_CONTEXT_INCOMPATIBLE recorded context differs from current inputs')
        provenance = 'policy'
        override = True
    else:
        root = _dispatch_root(project, state, task_id)
        provenance = 'dispatch' if root else 'isolated'
        root = root or _isolated_root(project, state, task_id)
        key = compatibility_key(project, environ)
    desired = {'CARGO_TARGET_DIR': str(root), 'BUILD_ROOT': str(root)}
    if mode == 'reuse-exclusive':
        extra = (cache.get('context') or {}).get('env') or {}
        if not isinstance(extra, dict):
            raise WorkflowError('BUILD_CONTEXT_INVALID context.env must be an object')
        for key_name, value in extra.items():
            if key_name not in ENV_KEYS or not isinstance(value, str):
                raise WorkflowError('BUILD_CONTEXT_INVALID unsupported environment ' + str(key_name))
            desired[key_name] = value
    conflicts = [key_name for key_name, value in desired.items()
                 if environ.get(key_name) and environ[key_name] != value]
    if conflicts and not override:
        raise WorkflowError('BUILD_CONTEXT_CONFLICT ambient ' + ', '.join(sorted(conflicts)))
    return {
        'schema': 1, 'mode': mode, 'root': str(root), 'env': desired,
        'compatibility_key': key, 'provenance': provenance,
        'override_ambient': override,
        'owner': {'run': state.get('id'), 'task': task_id,
                  'workspace': str(Path(source_root).resolve())},
        'lease': {'path': str(root / BUILD_LEASE_NAME)},
    }


def command_environment(environ, context, temp_env=None):
    """Merge ambient, bound build variables and this run's temporary roots."""
    result = dict(environ)
    if context.get('root'):
        result.update(context.get('env') or {})
    result.update(temp_env or {})
    return result


def _create_lease(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


class BuildLease:
    def __init__(self, path, payload, evidence_id):
        self.path = Path(path)
        self.payload = payload
        self.evidence_id = evidence_id

    def attach(self, process):
        if process is None:
            return
        self.payload['child'] = identity(process.pid)
        self.payload['phase'] = 'running'
        atomic(self.path, self.payload)

    def finish(self, phase='finalized'):
        data = read(self.path)
        if data.get('evidence_id') != self.evidence_id:
            raise WorkflowError('BUILD_LEASE_LOST')
        data['phase'] = phase
        atomic(self.path, data)


@contextmanager
def build_lease(project, context, evidence_id):
    """Hold one cooperative writer lease for a resolved build root."""
    if not context.get('root'):
        yield None
        return
    path = Path(context['root']) / BUILD_LEASE_NAME
    payload = {
        'schema': 1, 'evidence_id': evidence_id, 'phase': 'launching',
        'controller': identity(os.getpid()), 'child': None,
        'owner': context.get('owner') or {},
        'compatibility_key': context.get('compatibility_key'),
        'started_at': now(),
    }
    try:
        _create_lease(path, payload)
    except FileExistsError:
        try:
            existing = read(path)
        except (OSError, WorkflowError) as exc:
            raise WorkflowError('BUILD_LEASE_INVALID ' + str(exc)) from exc
        if is_alive(existing.get('controller')):
            raise WorkflowError('BUILD_LEASE_HELD by ' + str((existing.get('owner') or {}).get('run')))
        raise WorkflowError('BUILD_LEASE_UNKNOWN investigate ' + str(path))
    lease = BuildLease(path, payload, evidence_id)
    try:
        yield lease
    finally:
        try:
            current = read(path)
        except (OSError, WorkflowError):
            current = None
        if current and current.get('evidence_id') == evidence_id:
            child = current.get('child')
            if child and is_alive(child):
                current['phase'] = 'child-still-running'
                atomic(path, current)
            else:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
