"""Read-only preview of legacy GSP identities, records and workspaces."""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
from typing import Any, Iterable

from .documents import read_document_index_readonly
from .hosts import is_alive, process_token
from .identity_store import IdentityStore
from .orchestration_types import MigrationManifest
from .core import (WorkflowError, inside)
from .workflow_locks import (_advisory, _advisory_release)
from .todos import Backlog


WORK_NUMBER = re.compile(r'GSP-\d{4,}\Z')
UID = re.compile(r'[a-f0-9]{32}\Z')
TODO_IDENTITY = re.compile(r'(GSP-\d{4,}|TODO-\d{6,})\Z')
TODO_OPEN = re.compile(r'<!--\s*gsp-todo:\s*([^>\r\n]+?)\s*-->')
TODO_CLOSE = re.compile(r'<!--\s*/gsp-todo:\s*([^>\r\n]+?)\s*-->')
TODO_MALFORMED = re.compile(r'<!--\s*gsp-todo\s*-->')
SESSION_TOKEN = re.compile(r'S[0-9]{2,}\Z')
DISPATCH_TOKEN = re.compile(r'D[0-9]{3,}\Z')
DOMAINS = (
    'work_record', 'run_state', 'task_directory', 'todo_marker',
    'explicit_workspace_binding', 'registered_git_worktree',
)
BACKUP_POLICY = {
    'preview_only': True,
    'source_hash_required': True,
    'destructive_actions': [],
    'migration_apply': 'not_performed',
    'unknown_is_fail_closed': True,
}
AUTHORITY_SOURCE = '.gsp/orchestration/registry.sqlite'
DOCUMENT_INDEX_SOURCE = '.gsp/documents.sqlite'
PROJECT_SOURCE = '.gsp/project.json'
SQLITE_SOURCES = (
    AUTHORITY_SOURCE,
    AUTHORITY_SOURCE + '-wal',
    AUTHORITY_SOURCE + '-shm',
    DOCUMENT_INDEX_SOURCE,
    DOCUMENT_INDEX_SOURCE + '-wal',
    DOCUMENT_INDEX_SOURCE + '-shm',
)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _error_name(exc: OSError) -> str:
    if isinstance(exc, PermissionError):
        return 'permission-denied'
    return 'read-error:' + str(exc.errno)


def _read_windows_guarded(path: Path) -> tuple[bytes | None, str | None]:
    """Read while denying writers, without opening the SQLite database."""
    if os.name != 'nt':
        return None, 'platform-guard-unsupported'
    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    handle = create_file(
        str(path), 0x80000000, 0, None, 3, 0x00000080, None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        error = ctypes.get_last_error()
        if error == 2:
            return None, 'missing'
        if error in (5, 32, 33):
            return None, 'writer-live'
        return None, 'guard-error:' + str(error)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except OSError:
        close_handle(handle)
        raise
    with os.fdopen(fd, 'rb', closefd=True) as stream:
        return stream.read(), None


def _capture_source(path: Path) -> dict:
    try:
        exists = path.is_file()
    except OSError as exc:
        return {'exists': True, 'bytes': None, 'digest': None, 'error': _error_name(exc)}
    if not exists:
        return {'exists': False, 'bytes': None, 'digest': None, 'error': None}
    try:
        relative = path.as_posix()
        guarded = relative.endswith(SQLITE_SOURCES)
        if guarded:
            raw, error = _read_windows_guarded(path)
            if error == 'missing':
                return {'exists': False, 'bytes': None, 'digest': None, 'error': None}
        else:
            raw, error = path.read_bytes(), None
    except OSError as exc:
        return {'exists': True, 'bytes': None, 'digest': None, 'error': _error_name(exc)}
    if error:
        return {'exists': True, 'bytes': None, 'digest': None, 'error': error}
    return {'exists': True, 'bytes': raw, 'digest': _sha(raw or b''), 'error': None}


def _capture_item(capture: dict, relative: str) -> dict:
    return capture.get(relative) or {
        'exists': False, 'bytes': None, 'digest': None, 'error': None,
    }


def _read_captured_json(
    root: Path,
    path: Path,
    capture: dict,
) -> tuple[Any, str | None]:
    relative = _rel(root, path)
    item = _capture_item(capture, relative)
    if item['error']:
        return None, item['error']
    if not item['exists'] or item['bytes'] is None:
        return None, 'missing'
    try:
        return json.loads(item['bytes'].decode('utf-8-sig')), None
    except (UnicodeError, ValueError) as exc:
        return None, str(exc)


def _identity_state(value: Any, token_field: str = 'token') -> str:
    """Classify an execution identity without trusting PID or timestamps alone."""
    if not isinstance(value, dict):
        return 'unknown'
    pid = value.get('pid')
    token = value.get(token_field)
    if type(pid) is not int or pid <= 0:
        return 'unknown'
    if not isinstance(token, str) or not token or token == 'unknown':
        return 'unknown'
    current = process_token(pid)
    if current is None:
        return 'dead'
    if current == 'unknown' or current != token:
        return 'unknown'
    return 'live' if is_alive({'pid': pid, 'token': token}) else 'unknown'


def _advisory_busy(path: Path) -> bool | None:
    """Probe an existing lock through a read-only descriptor; never create it."""
    if not path.is_file():
        return False
    try:
        fd = os.open(path, os.O_RDONLY)
    except PermissionError:
        return None
    except OSError:
        return None
    try:
        if os.name == 'nt':
            acquired = _advisory(fd, False)
            if acquired:
                _advisory_release(fd)
            return not acquired
        try:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            except OSError:
                return None
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except ImportError:
            return None
    finally:
        os.close(fd)


def _lock_observation(
    root: Path,
    relative: str,
    *,
    capture: dict,
    token_field: str,
    probe_advisory: bool,
) -> dict:
    path = root / relative
    item = _capture_item(capture, relative)
    result = {'source_path': relative, 'exists': item['exists'], 'state': 'absent',
              'task': None, 'owner_task': None}
    if not result['exists']:
        return result
    metadata, error = _read_captured_json(root, path, capture)
    busy = _advisory_busy(path) if probe_advisory else False
    if error == 'permission-denied':
        result['state'] = 'live' if busy else 'unknown'
        result['reason'] = 'metadata-unreadable'
        return result
    if error:
        result['state'] = 'malformed'
        result['reason'] = error
        return result
    pid = metadata.get('pid')
    token = metadata.get(token_field)
    if type(pid) is not int or pid <= 0:
        result['state'] = 'malformed'
        result['reason'] = 'pid-invalid'
        return result
    if not isinstance(token, str) or not token or token == 'unknown':
        result['state'] = 'unknown'
        result['reason'] = 'trusted-identity-missing'
        result['task'] = metadata.get('task') if isinstance(metadata.get('task'), str) else None
        return result
    result['task'] = metadata.get('task') if isinstance(metadata.get('task'), str) else None
    result['owner_task'] = result['task']
    if busy:
        result['state'] = 'live'
        result['reason'] = 'advisory-lock-busy'
        return result
    result['state'] = _identity_state(metadata, token_field)
    return result


def _driver_state_observation(root: Path, path: Path, capture: dict) -> dict:
    relative = _rel(root, path)
    result = {
        'source_path': relative, 'run': path.parent.parent.name,
        'state': 'invalid', 'task': None, 'status': None,
        'worker_state': None, 'check_state': None, 'pending': False,
        'conflict': None,
    }
    metadata, error = _read_captured_json(root, path, capture)
    if error:
        result['reason'] = error
        return result
    if not isinstance(metadata, dict):
        result['reason'] = 'not-an-object'
        return result
    status = metadata.get('status')
    task = metadata.get('task')
    if not isinstance(status, str) or not isinstance(task, str) or not task:
        result['reason'] = 'status-or-task-invalid'
        return result
    result['status'] = status
    result['task'] = task
    pending = metadata.get('pending')
    result['pending'] = pending is not None
    if pending is not None and not isinstance(pending, dict):
        result['reason'] = 'pending-invalid'
        result['conflict'] = 'pending-invalid'
        return result
    worker = pending.get('worker') if isinstance(pending, dict) else None
    check = metadata.get('pending_check')
    result['worker_state'] = _identity_state(worker) if worker is not None else None
    result['check_state'] = _identity_state(check) if check is not None else None
    states = [state for state in (result['worker_state'], result['check_state'])
              if state is not None]
    if 'live' in states:
        result['state'] = 'live'
    elif 'unknown' in states:
        result['state'] = 'unknown'
    elif states:
        result['state'] = 'stale'
    elif status == 'running':
        result['state'] = 'unknown'
        result['reason'] = 'running-without-process-identity'
    else:
        result['state'] = 'idle'
    if states and status != 'running':
        result['conflict'] = 'process-identity-outside-running'
    return result


def _progress_observation(root: Path, path: Path, capture: dict) -> dict:
    result = {
        'source_path': _rel(root, path), 'run': path.parents[2].name,
        'state': 'invalid', 'evidence_id': None, 'task': None,
        'phase': None, 'status': None, 'process_state': None,
        'parent_state': None,
    }
    metadata, error = _read_captured_json(root, path, capture)
    if error:
        result['reason'] = error
        return result
    if not isinstance(metadata, dict):
        result['reason'] = 'not-an-object'
        return result
    result['evidence_id'] = metadata.get('evidence_id')
    result['task'] = metadata.get('task')
    result['phase'] = metadata.get('phase')
    result['status'] = metadata.get('status')
    result['process_state'] = _identity_state(metadata.get('process_identity'))
    result['parent_state'] = _identity_state(metadata.get('parent'))
    states = (result['process_state'], result['parent_state'])
    if 'live' in states:
        result['state'] = 'live'
    elif 'unknown' in states:
        result['state'] = 'unknown'
    else:
        result['state'] = 'stale'
    return result


def _execution_snapshot(root: Path, capture: dict) -> dict:
    drivers = [
        _driver_state_observation(root, path, capture)
        for path in sorted((root / '.gsp' / 'runs').glob('*/driver/state.json'))
        if path.is_file()
    ]
    progress = [
        _progress_observation(root, path, capture)
        for path in sorted((root / '.gsp' / 'runs').glob('*/evidence/*/command-progress.json'))
        if path.is_file()
    ]
    writer_lock = _lock_observation(
        root, '.gsp/writer.lock', capture=capture,
        token_field='process_token', probe_advisory=True)
    stable_mutex = _advisory_busy(root / '.gsp' / 'writer.mutex')
    if stable_mutex is True:
        writer_lock.update({
            'state': 'live',
            'reason': 'stable-writer-mutex-busy',
        })
    elif stable_mutex is None and writer_lock['state'] != 'live':
        writer_lock.update({
            'state': 'unknown',
            'reason': 'stable-writer-mutex-unknown',
        })
    return {
        'writer_lock': writer_lock,
        'driver_lock': _lock_observation(
            root, '.gsp/driver.lock', capture=capture,
            token_field='token', probe_advisory=False),
        'driver_states': drivers,
        'command_progress': progress,
    }


def _execution_blockers(root: Path, execution: dict, runs: list[dict], blockers: list[dict]) -> None:
    prefix = {'live': 'LIVE', 'unknown': 'UNKNOWN', 'malformed': 'MALFORMED'}
    for key, code in (('writer_lock', 'WRITER_LOCK'), ('driver_lock', 'DRIVER_LOCK')):
        observation = execution[key]
        state = observation['state']
        if state in prefix:
            blockers.append(_blocker(
                code + '_' + prefix[state], observation['source_path'],
                observation.get('reason') or state))
    run_ids = {row['identity'] for row in runs}
    driver_lock = execution['driver_lock']
    if (driver_lock['state'] in {'live', 'unknown'}
            and driver_lock.get('task') and driver_lock['task'] not in run_ids):
        blockers.append(_blocker(
            'DRIVER_LOCK_OWNER_UNKNOWN', driver_lock['source_path'],
            str(driver_lock['task'])))
    for observation in execution['driver_states']:
        state = observation['state']
        if state == 'invalid':
            blockers.append(_blocker(
                'DRIVER_STATE_INVALID', observation['source_path'],
                observation.get('reason') or 'invalid driver state'))
        elif state in {'live', 'unknown', 'stale'}:
            blockers.append(_blocker(
                'DRIVER_STATE_' + state.upper(), observation['source_path'],
                observation.get('reason') or 'execution identity is ' + state,
                observation.get('task')))
        if observation.get('conflict'):
            blockers.append(_blocker(
                'DRIVER_STATE_CONFLICT', observation['source_path'],
                observation['conflict'], observation.get('task')))
        if observation.get('status') == 'running' and driver_lock['state'] != 'live':
            blockers.append(_blocker(
                'DRIVER_STATE_CONFLICT', observation['source_path'],
                'running state has no live driver lease', observation.get('task')))
        if (driver_lock['state'] == 'live' and driver_lock.get('task')
                and observation.get('status') == 'running'
                and driver_lock['task'] != observation.get('task')):
            blockers.append(_blocker(
                'DRIVER_STATE_CONFLICT', observation['source_path'],
                'driver lease belongs to another task', observation.get('task')))

    progress_by_path = {row['source_path']: row for row in execution['command_progress']}
    for run in runs:
        if run.get('inflight') is None:
            continue
        inflight = run['inflight']
        if not isinstance(inflight, dict):
            blockers.append(_blocker(
                'INFLIGHT_MALFORMED', run['source_path'], 'inflight is not an object',
                run['identity']))
            continue
        evidence_id = inflight.get('id')
        if not isinstance(evidence_id, str) or not UID.fullmatch(evidence_id):
            blockers.append(_blocker(
                'INFLIGHT_MALFORMED', run['source_path'], 'inflight id is invalid',
                run['identity']))
            continue
        run_file = root / run['source_path']
        progress_path = run_file.parent / 'evidence' / evidence_id / 'command-progress.json'
        progress_relative = _rel(root, progress_path)
        observation = progress_by_path.get(progress_relative)
        if observation is None:
            blockers.append(_blocker(
                'INFLIGHT_PROGRESS_MISSING', run['source_path'],
                progress_relative, run['identity']))
            continue
        if observation['state'] == 'invalid':
            blockers.append(_blocker(
                'INFLIGHT_PROGRESS_INVALID', progress_relative,
                observation.get('reason') or 'command progress is invalid',
                run['identity']))
            continue
        conflicts = []
        if observation.get('evidence_id') != evidence_id:
            conflicts.append('evidence id mismatch')
        inflight_task = inflight.get('task')
        if (inflight_task is not None and observation.get('task') is not None
                and inflight_task != observation.get('task')):
            conflicts.append('task mismatch')
        inflight_phase = inflight.get('phase')
        if (inflight_phase is not None and observation.get('phase') is not None
                and inflight_phase != observation.get('phase')):
            conflicts.append('phase mismatch')
        if observation.get('status') in {
                'completed', 'failed', 'interrupted', 'timeout', 'launch-failed'}:
            conflicts.append('terminal command progress remains in inflight state')
        if conflicts:
            blockers.append(_blocker(
                'INFLIGHT_CONFLICT', progress_relative, '; '.join(conflicts),
                run['identity']))
        state = observation['state']
        code = {
            'live': 'INFLIGHT_LIVE',
            'unknown': 'INFLIGHT_UNKNOWN',
            'stale': 'INFLIGHT_STALE',
        }.get(state)
        if code:
            blockers.append(_blocker(
                code, progress_relative, 'execution identity is ' + state,
                run['identity']))

def _rel(root: Path, path: Path) -> str:
    return Path(path).relative_to(root).as_posix()


def _path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(Path(path).resolve(strict=False))))


def _source_record(root: Path, path: Path, value: dict) -> dict:
    return {'kind': 'source', 'domain': value.pop('domain'),
            'source_path': _rel(root, path), **value}


def _blocker(code: str, source: Path | str, detail: str, identity: str | None = None) -> dict:
    result = {'code': code, 'source_path': str(source), 'detail': detail}
    if identity is not None:
        result['identity'] = identity
    return result


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['git', '-C', str(cwd), *args], check=False, capture_output=True,
        text=True, encoding='utf-8', errors='replace',
        env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0'},
    )


def _parse_worktrees(output: str) -> list[dict]:
    rows, current = [], None
    for line in output.splitlines():
        if not line:
            if current:
                rows.append(current)
            current = None
            continue
        key, _, value = line.partition(' ')
        if key == 'worktree':
            current = {'path': value, 'detached': False}
        elif current is None:
            continue
        elif key == 'HEAD':
            current['head'] = value
        elif key == 'branch':
            current['branch'] = value.removeprefix('refs/heads/')
        elif key == 'detached':
            current['detached'] = True
        elif key in {'locked', 'prunable'}:
            current[key] = value or True
    if current:
        rows.append(current)
    return rows


def _common_dir(path: Path) -> str | None:
    result = _git(path, 'rev-parse', '--git-common-dir')
    if result.returncode or not result.stdout.strip():
        return None
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = path / common
    return _path_key(common)


def _ignored_git_status(status_text: str, ignored_paths: tuple[str, ...]) -> bool:
    if not ignored_paths:
        return False
    ignored = tuple(path.replace('\\', '/').rstrip('/') + '/' for path in ignored_paths)
    for line in status_text.splitlines():
        value = line[3:] if len(line) >= 3 else line
        if ' -> ' in value:
            value = value.rsplit(' -> ', 1)[-1]
        value = value.strip().strip('"').replace('\\', '/')
        if not any(value == path[:-1] or value.startswith(path) for path in ignored):
            return False
    return True


def _git_inventory(root: Path, ignored_paths: tuple[str, ...] = ()) -> dict:
    identity = _git(root, 'rev-parse', '--show-toplevel', '--verify', 'HEAD')
    if identity.returncode:
        detail = identity.stderr.strip() or identity.stdout.strip() or 'git rev-parse failed'
        return {'status': 'not-a-repository', 'current': {}, 'worktrees': [], 'detail': detail}
    lines = identity.stdout.splitlines()
    top = Path(lines[0]).resolve()
    head = lines[1].strip() if len(lines) > 1 else None
    branch_result = _git(root, 'branch', '--show-current')
    branch = branch_result.stdout.strip() or None if branch_result.returncode == 0 else None
    listing = _git(root, 'worktree', 'list', '--porcelain')
    if listing.returncode:
        return {'status': 'blocked', 'current': {'root': str(top), 'head': head, 'branch': branch},
                'worktrees': [], 'detail': listing.stderr.strip() or 'git worktree list failed'}
    rows = []
    for raw in _parse_worktrees(listing.stdout):
        path = Path(raw['path']).resolve(strict=False)
        row = {
            'path': str(path), 'identity': _path_key(path), 'head': raw.get('head'),
            'branch': raw.get('branch'), 'detached': bool(raw.get('detached')),
            'exists': path.is_dir(), 'common_dir': _common_dir(path) if path.is_dir() else None,
            'dirty': None, 'merged': None,
        }
        if row['exists']:
            status = _git(path, 'status', '--porcelain=v1', '-uall')
            status_text = status.stdout
            if (
                status.returncode == 0
                and _path_key(path) == _path_key(top)
                and _ignored_git_status(status_text, ignored_paths)
            ):
                status_text = ''
            row['dirty'] = bool(status_text.strip()) if status.returncode == 0 else True
            if row['head'] and head:
                row['merged'] = _git(root, 'merge-base', '--is-ancestor', row['head'], head).returncode == 0
        rows.append(row)
    return {
        'status': 'ok', 'current': {'root': str(top), 'head': head, 'branch': branch},
        'common_dir': _common_dir(top), 'worktrees': rows,
    }


def _task_directories(root: Path) -> list[Path]:
    base = root / 'docs' / 'work'
    if not base.is_dir():
        return []
    found = set()
    for pattern in ('GSP-*', '*/GSP-*', '*/*/GSP-*'):
        found.update(path.resolve() for path in base.glob(pattern) if path.is_dir())
    return sorted(found)


def _source_files(root: Path) -> list[Path]:
    files = set()
    files.update((root / 'docs' / 'work').glob('**/record.json'))
    files.update((root / '.gsp' / 'runs').glob('*/state.json'))
    files.update((root / '.gsp' / 'runs').glob('*/driver/state.json'))
    files.update((root / '.gsp' / 'runs').glob('*/evidence/*/command-progress.json'))
    files.update((root / '.gsp' / 'runs').glob('*/sessions/index.json'))
    files.update((root / '.gsp' / 'runs').glob('*/sessions/dispatch.json'))
    for path in (
        root / 'TODOS.md', root / 'TODO.md', root / '.gsp' / 'work-counter.json',
        root / '.gsp' / 'project.json',
        root / '.gsp' / 'documents.sqlite',
        root / '.gsp' / 'documents.sqlite-wal',
        root / '.gsp' / 'documents.sqlite-shm',
        root / '.gsp' / 'backlog' / 'records.json',
        root / '.gsp' / 'backlog' / 'transaction.json',
        root / '.gsp' / 'orchestration' / 'registry.sqlite',
        root / '.gsp' / 'orchestration' / 'registry.sqlite-wal',
        root / '.gsp' / 'orchestration' / 'registry.sqlite-shm',
        root / '.gsp' / 'orchestration' / 'registry.json',
        root / '.gsp' / 'writer.lock', root / '.gsp' / 'driver.lock',
    ):
        if path.is_file():
            files.add(path)
    return sorted(files)


def _capture_source_state(
    root: Path,
    *,
    ignored_source_paths: tuple[str, ...] = (),
    ignored_git_paths: tuple[str, ...] = (),
) -> tuple[dict, dict]:
    ignored = set()
    for path in ignored_source_paths:
        value = Path(path.replace('\\', '/')).as_posix()
        while value.startswith('./'):
            value = value[2:]
        ignored.add(value.lstrip('/'))
    candidates = set(_source_files(root))
    candidates.update(root / relative for relative in SQLITE_SOURCES)
    captures = {
        _rel(root, path): _capture_source(path)
        for path in sorted(candidates)
    }
    files = {
        relative: item['digest'] if item['digest'] is not None else 'unavailable:' + str(item['error'])
        for relative, item in captures.items() if item['exists']
    }
    membership = {relative: bool(item['exists']) for relative, item in captures.items()}
    read_errors = {
        relative: item['error'] for relative, item in captures.items()
        if item['exists'] and item['error']
    }
    directories = [_rel(root, path) for path in _task_directories(root)]
    state = {
        'files': files,
        'membership': membership,
        'read_errors': read_errors,
        'task_directories': directories,
        'git': _git_inventory(root, ignored_git_paths),
        'execution': _execution_snapshot(root, captures),
    }
    for relative in ignored:
        state['files'].pop(relative, None)
        state['read_errors'].pop(relative, None)
        if relative in SQLITE_SOURCES:
            state['membership'][relative] = False
        else:
            state['membership'].pop(relative, None)
    if '.gsp/writer.lock' in ignored:
        state['execution']['writer_lock'] = {
            'source_path': '.gsp/writer.lock',
            'exists': False,
            'state': 'absent',
            'task': None,
            'owner_task': None,
        }
    return state, captures


def _source_digest(
    root: Path,
    *,
    ignored_source_paths: tuple[str, ...] = (),
    ignored_git_paths: tuple[str, ...] = (),
) -> str:
    state, _capture = _capture_source_state(
        root,
        ignored_source_paths=ignored_source_paths,
        ignored_git_paths=ignored_git_paths,
    )
    return _sha(_json(state).encode('utf-8'))


def _capture_blockers(root: Path, capture: dict, blockers: list[dict]) -> None:
    for relative, item in sorted(capture.items()):
        error = item.get('error')
        if not item.get('exists') or not error:
            continue
        if relative in {
            AUTHORITY_SOURCE,
            AUTHORITY_SOURCE + '-wal',
            AUTHORITY_SOURCE + '-shm',
            DOCUMENT_INDEX_SOURCE,
            DOCUMENT_INDEX_SOURCE + '-wal',
            DOCUMENT_INDEX_SOURCE + '-shm',
        }:
            continue
        blockers.append(_blocker('SOURCE_READ_FAILED', relative, error))


def _snapshot_problem(capture: dict, base: str) -> str | None:
    for relative in (base, base + '-wal', base + '-shm'):
        item = _capture_item(capture, relative)
        if item.get('exists') and item.get('error'):
            return str(item['error'])
    wal = _capture_item(capture, base + '-wal')
    shm = _capture_item(capture, base + '-shm')
    if bool(wal.get('exists')) != bool(shm.get('exists')):
        return 'incomplete-wal-sidecar-set'
    return None


def _write_sqlite_snapshot(temp_root: Path, capture: dict, base: str) -> str | None:
    problem = _snapshot_problem(capture, base)
    if problem:
        return problem
    main = _capture_item(capture, base)
    if not main.get('exists'):
        return 'missing'
    if main.get('bytes') is None:
        return 'main-bytes-unavailable'
    for relative in (base, base + '-wal', base + '-shm'):
        item = _capture_item(capture, relative)
        if not item.get('exists'):
            continue
        if item.get('bytes') is None:
            return 'snapshot-bytes-unavailable'
        destination = temp_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(item['bytes'])
    return None


def _write_captured_file(temp_root: Path, capture: dict, relative: str) -> bool:
    item = _capture_item(capture, relative)
    if not item.get('exists') or item.get('bytes') is None:
        return False
    destination = temp_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(item['bytes'])
    return True


def _project_identity(
    root: Path,
    capture: dict,
    blockers: list[dict],
) -> tuple[str | None, str | None]:
    item = _capture_item(capture, PROJECT_SOURCE)
    if not item.get('exists'):
        blockers.append(_blocker('PROJECT_IDENTITY_INVALID', PROJECT_SOURCE, 'missing'))
        return None, None
    if item.get('error') or item.get('bytes') is None:
        blockers.append(_blocker(
            'PROJECT_IDENTITY_INVALID', PROJECT_SOURCE, str(item.get('error') or 'unreadable'),
        ))
        return None, None
    try:
        value = json.loads(item['bytes'].decode('utf-8-sig'))
    except (UnicodeError, ValueError) as exc:
        blockers.append(_blocker('PROJECT_IDENTITY_INVALID', PROJECT_SOURCE, str(exc)))
        return None, None
    if not isinstance(value, dict):
        blockers.append(_blocker('PROJECT_IDENTITY_INVALID', PROJECT_SOURCE, 'not an object'))
        return None, None
    project_uid = value.get('id')
    recorded_root = value.get('root')
    if (
        not isinstance(project_uid, str)
        or not project_uid.strip()
        or project_uid == 'unknown'
        or not isinstance(recorded_root, str)
        or not recorded_root.strip()
    ):
        blockers.append(_blocker(
            'PROJECT_IDENTITY_INVALID', PROJECT_SOURCE,
            'id/root missing or unknown',
        ))
        return None, None
    try:
        identity_root = Path(recorded_root).resolve()
    except OSError as exc:
        blockers.append(_blocker('PROJECT_IDENTITY_INVALID', PROJECT_SOURCE, str(exc)))
        return None, None
    if identity_root != root:
        blockers.append(_blocker(
            'PROJECT_IDENTITY_INVALID', PROJECT_SOURCE,
            f'root={identity_root} expected={root}',
        ))
        return project_uid, None
    return project_uid, str(identity_root)


def _authority(root: Path, blockers: list[dict], capture: dict) -> dict:
    path = root / AUTHORITY_SOURCE
    main = _capture_item(capture, AUTHORITY_SOURCE)
    if not main.get('exists'):
        if any(_capture_item(capture, relative).get('exists') for relative in (
            AUTHORITY_SOURCE + '-wal', AUTHORITY_SOURCE + '-shm',
        )):
            blockers.append(_blocker(
                'AUTHORITY_INVALID', path.relative_to(root), 'orphan WAL sidecar',
            ))
            return {'state': 'invalid', 'project_uid': None, 'incarnation': None}
        return {'state': 'uninitialized', 'project_uid': None, 'incarnation': None}
    if any(_capture_item(capture, relative).get('error') == 'writer-live' for relative in (
        AUTHORITY_SOURCE, AUTHORITY_SOURCE + '-wal', AUTHORITY_SOURCE + '-shm',
    )):
        blockers.append(_blocker(
            'AUTHORITY_WRITER_LIVE',
            AUTHORITY_SOURCE,
            'source database has a retained or active handle',
        ))
        return {'state': 'writer-live', 'project_uid': None, 'incarnation': None}
    project_uid, _project_root = _project_identity(root, capture, blockers)
    metadata = {}
    sequences = []
    try:
        with tempfile.TemporaryDirectory(prefix='gsp-authority-snapshot-') as temporary:
            temp_root = Path(temporary)
            problem = _write_sqlite_snapshot(temp_root, capture, AUTHORITY_SOURCE)
            if problem:
                blockers.append(_blocker('AUTHORITY_SNAPSHOT_UNKNOWN', path.relative_to(root), problem))
                return {'state': 'unknown', 'project_uid': None, 'incarnation': None}
            snapshot = temp_root / AUTHORITY_SOURCE
            with closing(sqlite3.connect(snapshot, timeout=2)) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA query_only=ON')
                schema_problem = IdentityStore._schema_problem(db)
                if schema_problem is not None:
                    blockers.append(_blocker(
                        'AUTHORITY_INVALID', path.relative_to(root), schema_problem,
                    ))
                    return {'state': 'invalid', 'project_uid': None, 'incarnation': None}
                metadata = {
                    row['key']: row['value']
                    for row in db.execute('SELECT key,value FROM metadata').fetchall()
                }
                sequences = [
                    dict(row)
                    for row in db.execute(
                        'SELECT kind,scope_uid,minimum_width,next_value '
                        'FROM sequences ORDER BY kind,scope_uid'
                    )
                ]
    except (OSError, sqlite3.Error, ValueError) as exc:
        blockers.append(_blocker('AUTHORITY_INVALID', path.relative_to(root), str(exc)))
        return {'state': 'invalid', 'project_uid': None, 'incarnation': None}
    authority_uid = metadata.get('project_uid')
    incarnation = metadata.get('incarnation')
    if (
        project_uid is None
        or _project_root is None
        or authority_uid != project_uid
        or not isinstance(incarnation, str)
        or not re.fullmatch(r'[a-f0-9]{32}', incarnation)
    ):
        blockers.append(_blocker(
            'AUTHORITY_INVALID', path.relative_to(root),
            'project UID or incarnation provenance mismatch',
        ))
        return {'state': 'invalid', 'project_uid': authority_uid, 'incarnation': incarnation}
    return {
        'state': 'ready',
        'source_path': AUTHORITY_SOURCE,
        'project_uid': authority_uid,
        'incarnation': incarnation,
        'schema': int(metadata.get('schema_version')),
        'sequences': sequences,
        'wal_present': _capture_item(capture, AUTHORITY_SOURCE + '-wal').get('exists', False),
    }

def _scan_work_records(root: Path, blockers: list[dict], capture: dict) -> list[dict]:
    rows = []
    for path in sorted((root / 'docs' / 'work').glob('**/record.json')):
        relative = _rel(root, path)
        source = _capture_item(capture, relative)
        data, error = _read_captured_json(root, path, capture)
        if error or not isinstance(data, dict):
            rows.append(_source_record(root, path, {
                'domain': 'work_record', 'identity': _rel(root, path),
                'directory': _rel(root, path.parent),
                'classification': 'blocked',
                'hash': source.get('digest') or 'unavailable:' + str(source.get('error') or error)}))
            blockers.append(_blocker('WORK_RECORD_INVALID', path.relative_to(root), error or 'not an object'))
            continue
        identity = data.get('number') if isinstance(data.get('number'), str) else _rel(root, path)
        row = _source_record(root, path, {
            'domain': 'work_record', 'identity': identity, 'uuid': data.get('uuid'),
            'number': data.get('number'), 'run_id': data.get('run_id'),
            'kind': data.get('kind'), 'stage': data.get('stage'),
            'directory': _rel(root, path.parent),
            'hash': source.get('digest') or 'unavailable:' + str(source.get('error') or 'unknown'),
            'classification': 'pending'})
        if data.get('kind') != 'work':
            blockers.append(_blocker('WORK_RECORD_KIND_INVALID', path.relative_to(root), str(data.get('kind'))))
        if not isinstance(data.get('uuid'), str) or not UID.fullmatch(data.get('uuid', '')):
            blockers.append(_blocker('WORK_RECORD_UUID_INVALID', path.relative_to(root), str(data.get('uuid'))))
        if not isinstance(data.get('number'), str) or not WORK_NUMBER.fullmatch(data.get('number', '')):
            blockers.append(_blocker('WORK_RECORD_NUMBER_INVALID', path.relative_to(root), str(data.get('number'))))
        expected = path.parent.relative_to(root).as_posix()
        folder = str(data.get('folder') or '').replace('\\', '/').rstrip('/')
        if folder != expected:
            blockers.append(_blocker('WORK_RECORD_PATH_CONFLICT', path.relative_to(root),
                                     f'folder={folder!r} actual={expected!r}'))
        directory_conflict = (
            isinstance(data.get('number'), str)
            and path.parent.name != data['number']
            and not path.parent.name.startswith(data['number'] + '-')
        )
        if directory_conflict:
            blockers.append(_blocker('WORK_RECORD_DIRECTORY_CONFLICT', path.relative_to(root),
                                     path.parent.name, data.get('number')))
        run_id = data.get('run_id')
        invalid = (data.get('kind') != 'work' or not isinstance(data.get('uuid'), str)
                   or not UID.fullmatch(data.get('uuid', ''))
                   or not isinstance(data.get('number'), str)
                   or not WORK_NUMBER.fullmatch(data.get('number', ''))
                   or folder != expected or directory_conflict
                   or (run_id is not None and (not isinstance(run_id, str) or not run_id.strip())))
        if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
            blockers.append(_blocker('WORK_RECORD_RUN_ID_INVALID', path.relative_to(root), repr(run_id)))
        if invalid:
            row['classification'] = 'blocked'
        rows.append(row)
    return rows


def _scan_runs(root: Path, blockers: list[dict], capture: dict) -> list[dict]:
    rows = []
    for path in sorted((root / '.gsp' / 'runs').glob('*/state.json')):
        relative = _rel(root, path)
        source = _capture_item(capture, relative)
        data, error = _read_captured_json(root, path, capture)
        if error or not isinstance(data, dict):
            rows.append(_source_record(root, path, {
                'domain': 'run_state', 'identity': _rel(root, path),
                'classification': 'blocked',
                'hash': source.get('digest') or 'unavailable:' + str(source.get('error') or error)}))
            blockers.append(_blocker('RUN_STATE_INVALID', path.relative_to(root), error or 'not an object'))
            continue
        identity = data.get('id') if isinstance(data.get('id'), str) and data.get('id').strip() else _rel(root, path)
        tasks = data.get('tasks', [])
        tasks_valid = isinstance(tasks, list)
        task_rows = tasks if tasks_valid else []
        row = _source_record(root, path, {
            'domain': 'run_state', 'identity': identity, 'number': data.get('number'),
            'work_uuid': data.get('work_uuid'), 'project_id': data.get('project_id'),
            'root': data.get('root'), 'status': data.get('status'),
            'inflight': data.get('inflight'),
            'git_topology': data.get('git_topology'),
            'task_topologies': [
                {'id': task.get('id'), 'topology': task.get('topology')}
                for task in task_rows
                if isinstance(task, dict) and task.get('topology') is not None
            ],
            'task_ids': sorted(str(task.get('id')) for task in task_rows
                               if isinstance(task, dict) and isinstance(task.get('id'), str)),
            'tasks_valid': tasks_valid,
            'hash': source.get('digest') or 'unavailable:' + str(source.get('error') or 'unknown'),
            'classification': 'pending'})
        if not isinstance(data.get('id'), str) or not data.get('id', '').strip():
            blockers.append(_blocker('RUN_ID_INVALID', path.relative_to(root), repr(data.get('id'))))
        if data.get('root'):
            try:
                state_root = Path(str(data['root'])).resolve(strict=False)
            except (OSError, ValueError):
                state_root = None
            if state_root != root:
                blockers.append(_blocker('RUN_ROOT_MISMATCH', path.relative_to(root), str(data.get('root'))))
        else:
            blockers.append(_blocker('RUN_ROOT_MISSING', path.relative_to(root), identity))
        directory_conflict = (
            isinstance(data.get('id'), str) and data.get('id', '').strip()
            and path.parent.name != data['id']
        )
        if directory_conflict:
            blockers.append(_blocker('RUN_STATE_PATH_CONFLICT', path.relative_to(root),
                                     path.parent.name, data.get('id')))
        if (not isinstance(data.get('id'), str) or not data.get('id', '').strip()
                or not data.get('root') or state_root != root or directory_conflict):
            row['classification'] = 'blocked'
        rows.append(row)
    return rows


def _duplicates(rows: Iterable[dict], field: str) -> set[str]:
    values = [row.get(field) for row in rows
              if isinstance(row.get(field), str) and row.get(field)]
    return {value for value, count in Counter(values).items() if count > 1}


def _index_validate(
    root: Path,
    works: list[dict],
    blockers: list[dict],
    capture: dict,
) -> tuple[dict, dict]:
    main = _capture_item(capture, DOCUMENT_INDEX_SOURCE)
    if not main.get('exists'):
        blockers.append(_blocker('DOCUMENT_INDEX_MISSING', DOCUMENT_INDEX_SOURCE, 'index file is absent'))
        return {}, {'state': 'missing', 'entries': 0, 'texts': [], 'text_count': 0}
    if any(_capture_item(capture, relative).get('error') for relative in (
        DOCUMENT_INDEX_SOURCE,
        DOCUMENT_INDEX_SOURCE + '-wal',
        DOCUMENT_INDEX_SOURCE + '-shm',
    )):
        errors = [
            _capture_item(capture, relative).get('error')
            for relative in (
                DOCUMENT_INDEX_SOURCE,
                DOCUMENT_INDEX_SOURCE + '-wal',
                DOCUMENT_INDEX_SOURCE + '-shm',
            )
            if _capture_item(capture, relative).get('error')
        ]
        code = (
            'DOCUMENT_INDEX_WRITER_LIVE'
            if 'writer-live' in errors
            else 'DOCUMENT_INDEX_SNAPSHOT_UNKNOWN'
        )
        blockers.append(_blocker(code, DOCUMENT_INDEX_SOURCE, ', '.join(map(str, errors))))
        return {}, {'state': 'unknown', 'entries': 0, 'texts': [], 'text_count': 0}
    try:
        with tempfile.TemporaryDirectory(prefix='gsp-document-index-snapshot-') as temporary:
            temp_root = Path(temporary)
            problem = _write_sqlite_snapshot(temp_root, capture, DOCUMENT_INDEX_SOURCE)
            if problem:
                blockers.append(_blocker(
                    'DOCUMENT_INDEX_UNREADABLE', DOCUMENT_INDEX_SOURCE, problem,
                ))
                return {}, {'state': 'unreadable', 'entries': 0, 'texts': [], 'text_count': 0}
            index = read_document_index_readonly(temp_root)
    except WorkflowError as exc:
        blockers.append(_blocker('DOCUMENT_INDEX_UNREADABLE', DOCUMENT_INDEX_SOURCE, str(exc)))
        return {}, {'state': 'unreadable', 'entries': 0, 'texts': [], 'text_count': 0}
    if index is None:
        blockers.append(_blocker('DOCUMENT_INDEX_UNREADABLE', DOCUMENT_INDEX_SOURCE, 'snapshot missing'))
        return {}, {'state': 'unreadable', 'entries': 0, 'texts': [], 'text_count': 0}
    entries = index['entries']
    by_uuid = defaultdict(list)
    by_number = defaultdict(list)
    by_path = defaultdict(list)
    for entry in entries:
        by_uuid[entry.get('uuid')].append(entry)
        by_number[entry.get('number')].append(entry)
        by_path[entry.get('path')].append(entry)
    for grouped, code in ((by_uuid, 'DOCUMENT_INDEX_DUPLICATE_UUID'),
                          (by_number, 'DOCUMENT_INDEX_DUPLICATE_NUMBER'),
                          (by_path, 'DOCUMENT_INDEX_DUPLICATE_PATH')):
        for value, grouped_rows in grouped.items():
            work_entries = [row for row in grouped_rows if row.get('kind') == 'work']
            if value is not None and len(work_entries) > 1:
                blockers.append(_blocker(code, '.gsp/documents.sqlite', str(value)))
    work_uids = {row['uuid'] for row in works if row.get('uuid')}
    for entry in entries:
        if entry.get('kind') == 'work' and entry.get('uuid') not in work_uids:
            blockers.append(_blocker('DOCUMENT_INDEX_ORPHAN', entry.get('path'), entry.get('uuid')))
    unique = {rows[0]['uuid']: rows[0] for rows in by_uuid.values() if len(rows) == 1}
    for work in works:
        entry = unique.get(work.get('uuid'))
        if entry is None:
            blockers.append(_blocker('DOCUMENT_INDEX_MISSING', work['source_path'], work['identity']))
            work['classification'] = 'blocked'
            continue
        expected = {'path': work['source_path'], 'number': work.get('number'), 'kind': 'work'}
        mismatches = [key for key, value in expected.items() if entry.get(key) != value]
        if mismatches:
            blockers.append(_blocker('DOCUMENT_INDEX_RECORD_CONFLICT', work['source_path'],
                                     ', '.join(mismatches), work['identity']))
            work['classification'] = 'blocked'
        if entry.get('hash') != work['hash']:
            blockers.append(_blocker('DOCUMENT_INDEX_STALE', work['source_path'],
                                     'index hash differs from record bytes', work['identity']))
            work['classification'] = 'blocked'
    return unique, {
        'state': 'ready',
        'entries': len(entries),
        'texts': index.get('texts', []),
        'text_count': index['text_count'],
    }

def _section_for(text: str, offset: int) -> str:
    headings = list(re.finditer(r'^##\s+(.+)$', text[:offset], re.M))
    return headings[-1][1].strip() if headings else ''


def _scan_todos(
    root: Path,
    blockers: list[dict],
    capture: dict,
) -> tuple[list[dict], list[dict], dict]:
    candidate_relatives = [
        relative for relative in ('TODOS.md', 'TODO.md')
        if _capture_item(capture, relative).get('exists')
    ]
    rows, aliases = [], []
    if len(candidate_relatives) > 1:
        blockers.append(_blocker('TODO_FILE_AMBIGUOUS', 'TODOS.md', 'both TODOS.md and TODO.md exist'))
    source_relative = candidate_relatives[0] if candidate_relatives else 'TODOS.md'
    source_item = _capture_item(capture, source_relative)
    if source_item.get('error') or (source_item.get('exists') and source_item.get('bytes') is None):
        blockers.append(_blocker(
            'TODO_FILE_INVALID', source_relative,
            str(source_item.get('error') or 'bytes unavailable'),
        ))
        return rows, aliases, {
            'state': 'invalid',
            'transaction': _capture_item(capture, '.gsp/backlog/transaction.json').get('exists', False),
        }
    try:
        text = (source_item.get('bytes') or b'').decode('utf-8-sig')
    except UnicodeError as exc:
        blockers.append(_blocker('TODO_FILE_INVALID', source_relative, str(exc)))
        return rows, aliases, {
            'state': 'invalid',
            'transaction': _capture_item(capture, '.gsp/backlog/transaction.json').get('exists', False),
        }
    for match in TODO_MALFORMED.finditer(text):
        line = text.count('\n', 0, match.start()) + 1
        blockers.append(_blocker(
            'TODO_MARKER_INVALID', source_relative, f'line {line}: missing identity',
        ))
    pending = _capture_item(capture, '.gsp/backlog/transaction.json').get('exists', False)
    if pending:
        blockers.append(_blocker(
            'TODO_TRANSACTION_PENDING', '.gsp/backlog/transaction.json',
            'recovery required',
        ))

    parsed_items = {}
    record_items = {}
    record_document = None
    parse_error = None
    with tempfile.TemporaryDirectory(prefix='gsp-backlog-snapshot-') as temporary:
        temp_root = Path(temporary)
        for relative in (
            'TODOS.md', 'TODO.md', '.gsp/backlog/records.json',
            '.gsp/backlog/transaction.json',
        ):
            _write_captured_file(temp_root, capture, relative)
        try:
            backlog = Backlog(temp_root)
            _text, loaded = backlog.load()
            parsed_items = {item['id']: item for item in loaded}
            record_items = dict(backlog._records['items'])
            record_document = backlog._records['document']
        except (OSError, ValueError, WorkflowError) as exc:
            parse_error = str(exc)
    records_present = _capture_item(capture, '.gsp/backlog/records.json').get('exists', False)
    if parse_error:
        marker_ids = {match[1].strip() for match in TODO_OPEN.finditer(text)}
        code = (
            'TODO_RECORDS_MISSING'
            if marker_ids and not records_present
            else 'TODO_BACKLOG_INVALID'
        )
        blockers.append(_blocker(
            code, '.gsp/backlog/records.json' if code == 'TODO_RECORDS_MISSING' else source_relative,
            parse_error,
        ))

    occurrences = Counter()
    seen = set()
    marker_ids = set()
    for match in TODO_OPEN.finditer(text):
        raw = match[1].strip()
        occurrences[raw] += 1
        line = text.count('\n', 0, match.start()) + 1
        identity = raw if occurrences[raw] == 1 else f'{raw}#L{line}'
        syntactic = bool(TODO_IDENTITY.fullmatch(raw))
        closing = f'<!-- /gsp-todo:{raw} -->'
        if not syntactic:
            blockers.append(_blocker(
                'TODO_MARKER_INVALID', source_relative,
                f'line {line}: malformed identity {raw!r}',
            ))
        if closing not in text[match.end():]:
            syntactic = False
            blockers.append(_blocker(
                'TODO_MARKER_INVALID', source_relative,
                f'line {line}: missing closing marker',
            ))
        if syntactic and raw in seen:
            blockers.append(_blocker('TODO_IDENTITY_DUPLICATE', source_relative, raw))
        seen.add(raw)
        if syntactic:
            marker_ids.add(raw)
        section = _section_for(text, match.start())
        valid = syntactic and parse_error is None
        row = {
            'kind': 'source', 'domain': 'todo_marker',
            'source_path': source_relative, 'identity': identity,
            'marker_id': raw, 'raw_alias': raw, 'object_kind': 'todo',
            'provenance': 'backlog-todo-marker', 'line': line, 'section': section,
            'classification': 'blocked' if (not valid or pending) else (
                'completed_heading' if section.casefold() == 'completed' else 'open_heading'
            ),
        }
        rows.append(row)
        if syntactic:
            aliases.append({
                'kind': 'alias', 'identity': raw, 'marker_id': raw,
                'number': int(raw.rsplit('-', 1)[1]), 'object_kind': 'todo',
                'provenance': 'backlog-todo-marker',
                'source_path': source_relative, 'line': line,
            })

    missing_records = marker_ids - set(record_items)
    orphan_records = set(record_items) - marker_ids
    for marker_id in sorted(missing_records):
        blockers.append(_blocker(
            'TODO_RECORD_MISSING', '.gsp/backlog/records.json', marker_id, marker_id,
        ))
    for marker_id in sorted(orphan_records):
        blockers.append(_blocker(
            'TODO_RECORD_ORPHAN', '.gsp/backlog/records.json', marker_id, marker_id,
        ))
    document_mismatch = False
    if records_present and (marker_ids or record_items) and parse_error is None:
        document_mismatch = (
            not isinstance(record_document, dict)
            or record_document.get('path') != source_relative
            or record_document.get('sha256') != source_item.get('digest')
        )
        if document_mismatch:
            blockers.append(_blocker(
                'TODO_RECORD_DOCUMENT_MISMATCH', '.gsp/backlog/records.json',
                'document path/hash does not match captured ' + source_relative,
            ))
    blocked_ids = missing_records | orphan_records
    if document_mismatch:
        blocked_ids |= marker_ids
    for row in rows:
        if row['marker_id'] in blocked_ids:
            row['classification'] = 'blocked'

    state = 'absent' if not candidate_relatives and not marker_ids and not record_items else (
        'invalid' if (parse_error or pending or document_mismatch
                      or missing_records or orphan_records) else 'ready'
    )
    return rows, aliases, {
        'state': state,
        'transaction': pending,
        'source_path': source_relative if candidate_relatives else None,
        'source_digest': source_item.get('digest') if candidate_relatives else None,
        'aliases': len(aliases),
        'records': len(record_items),
        'missing_records': sorted(missing_records),
        'orphan_records': sorted(orphan_records),
        'parsed_items': sorted(parsed_items),
    }


def _validate_todo_document(
    rows: list[dict],
    capture: dict,
    index_state: dict,
    blockers: list[dict],
) -> None:
    if not rows:
        return
    source_relative = rows[0]['source_path']
    source_digest = _capture_item(capture, source_relative).get('digest')
    texts = {
        row.get('path'): row.get('hash')
        for row in index_state.get('texts', [])
        if isinstance(row, dict)
    }
    if source_relative not in texts:
        blockers.append(_blocker(
            'TODO_DOCUMENT_MISSING', DOCUMENT_INDEX_SOURCE,
            'raw Markdown path ' + source_relative,
        ))
        for row in rows:
            row['classification'] = 'blocked'
        return
    if texts[source_relative] != source_digest:
        blockers.append(_blocker(
            'TODO_DOCUMENT_STALE', DOCUMENT_INDEX_SOURCE,
            f'indexed={texts[source_relative]} source={source_digest}',
        ))
        for row in rows:
            row['classification'] = 'blocked'


def _counter(
    root: Path,
    works: list[dict],
    runs: list[dict],
    todos: list[dict],
    authority: dict,
    capture: dict,
    blockers: list[dict],
) -> dict:
    path = root / '.gsp' / 'work-counter.json'
    data, error = _read_captured_json(root, path, capture)
    if error or not isinstance(data, dict) or type(data.get('next')) is not int:
        blockers.append(_blocker('WORK_COUNTER_INVALID', path.relative_to(root),
                                 error or 'next is not an integer'))
        return {'state': 'invalid', 'next': None, 'highest': None, 'effective_next': None}
    numbers = []
    sources = {'work_record': [], 'run_number': [], 'authority_work_sequence': []}
    for row in works:
        number = str(row.get('number') or '')
        if WORK_NUMBER.fullmatch(number):
            numbers.append(int(number.split('-')[1]))
            sources['work_record'].append(str(row.get('identity')))
    for row in runs:
        number = str(row.get('number') or '')
        if WORK_NUMBER.fullmatch(number):
            numbers.append(int(number.split('-')[1]))
            sources['run_number'].append(str(row.get('identity')))
    if authority.get('state') == 'ready':
        for sequence in authority.get('sequences', []):
            if sequence.get('kind') == 'work' and sequence.get('scope_uid') == '':
                try:
                    next_value = int(sequence.get('next_value'))
                except (TypeError, ValueError):
                    blockers.append(_blocker(
                        'WORK_HIGHWATER_INVALID', AUTHORITY_SOURCE,
                        'work sequence next_value is invalid',
                    ))
                    continue
                if next_value > 1:
                    numbers.append(next_value - 1)
                    sources['authority_work_sequence'].append('authority:work')
    highest = max(numbers) if numbers else 0
    todo_numbers = []
    todo_sources = {'todo_marker': [], 'authority_todo_sequence': []}
    for row in todos:
        raw = row.get('raw_alias')
        if (row.get('object_kind') == 'todo' and isinstance(raw, str)
                and TODO_IDENTITY.fullmatch(raw)):
            todo_numbers.append(int(raw.rsplit('-', 1)[1]))
            todo_sources['todo_marker'].append(raw)
    if authority.get('state') == 'ready':
        for sequence in authority.get('sequences', []):
            if sequence.get('kind') == 'todo' and sequence.get('scope_uid') == '':
                try:
                    next_value = int(sequence.get('next_value'))
                except (TypeError, ValueError):
                    blockers.append(_blocker(
                        'TODO_HIGHWATER_INVALID', AUTHORITY_SOURCE,
                        'todo sequence next_value is invalid',
                    ))
                    continue
                if next_value > 1:
                    todo_numbers.append(next_value - 1)
                    todo_sources['authority_todo_sequence'].append('authority:todo')
    todo_highest = max(todo_numbers) if todo_numbers else 0
    effective_next = max(data['next'], highest + 1)
    if data['next'] <= highest:
        blockers.append(_blocker('WORK_COUNTER_COLLISION', path.relative_to(root),
                                 f'next={data["next"]} highest={highest}'))
        state = 'collision'
    else:
        state = 'ready'
    return {
        'state': state,
        'next': data['next'],
        'highest': highest,
        'effective_next': effective_next,
        'sources': {key: sorted(set(value)) for key, value in sources.items()},
        'todo_highest': todo_highest,
        'todo_sources': {
            key: sorted(set(value)) for key, value in todo_sources.items()
        },
    }


def _bindings(root: Path, runs: list[dict], blockers: list[dict]) -> list[dict]:
    bindings = []
    for run in runs:
        values = [(run['identity'], run.get('git_topology'))]
        values.extend(
            (f'{run["identity"]}:{task.get("id")}', task.get('topology'))
            for task in run.get('task_topologies') or []
        )
        for identity, topology in values:
            if topology is None:
                continue
            if not isinstance(topology, dict):
                blockers.append(_blocker(
                    'WORKTREE_TOPOLOGY_INVALID', run['source_path'],
                    'topology must be an object', identity,
                ))
                continue
            invalid_fields = [
                field for field in ('branch', 'effective_mode', 'requested_mode')
                if topology.get(field) is not None
                and not isinstance(topology[field], str)
            ]
            if invalid_fields:
                blockers.append(_blocker(
                    'WORKTREE_TOPOLOGY_INVALID', run['source_path'],
                    'expected string fields: ' + ', '.join(invalid_fields), identity,
                ))
                continue
            resolved = {}
            invalid_path = False
            for field in ('workspace_root', 'worktree_path'):
                raw = topology.get(field)
                if raw is None:
                    continue
                if not isinstance(raw, str) or not raw.strip():
                    invalid_path = True
                    blockers.append(_blocker(
                        'WORKTREE_PATH_INVALID', run['source_path'],
                        field + '=' + repr(raw), identity,
                    ))
                    continue
                try:
                    resolved[field] = Path(raw).resolve(strict=False)
                except (OSError, ValueError, RuntimeError) as exc:
                    invalid_path = True
                    blockers.append(_blocker(
                        'WORKTREE_PATH_INVALID', run['source_path'],
                        field + ': ' + str(exc), identity,
                    ))
            if invalid_path or not resolved:
                continue
            path = resolved.get('workspace_root', resolved.get('worktree_path'))
            root_path = resolved.get('workspace_root')
            worktree = resolved.get('worktree_path')
            path_conflict = (
                root_path is not None and worktree is not None
                and _path_key(root_path) != _path_key(worktree)
            )
            if path_conflict:
                blockers.append(_blocker(
                    'WORKTREE_PATH_CONFLICT', run['source_path'],
                    f'workspace_root={root_path} worktree_path={worktree}', identity,
                ))
            bindings.append({
                'identity': identity, 'run': run['identity'], 'source_path': run['source_path'],
                'workspace_path': str(path), 'path_key': _path_key(path),
                'workspace_root': str(root_path) if root_path is not None else None,
                'worktree_path': str(worktree) if worktree is not None else None,
                'path_conflict': path_conflict,
                'branch': topology.get('branch'),
                'mode': topology.get('effective_mode') or topology.get('requested_mode')})
    path_groups = defaultdict(list)
    branch_groups = defaultdict(list)
    for row in bindings:
        managed = row['workspace_path'] == str(root) or inside(root / '.gsp' / 'worktrees',
                                                              Path(row['workspace_path']))
        row['control_only'] = row['workspace_path'] == str(root) and row.get('mode') == 'in-place'
        row['external'] = not managed and not row['control_only']
        if not row['control_only']:
            path_groups[row['path_key']].append(row['identity'])
            if row.get('branch'):
                branch_groups[row['branch']].append(row['identity'])
    for value, identities in path_groups.items():
        if len(identities) > 1:
            blockers.append(_blocker('GIT_WORKTREE_CONFLICT', value, ', '.join(identities)))
    for value, identities in branch_groups.items():
        if len(identities) > 1:
            blockers.append(_blocker('GIT_BRANCH_CONFLICT', value, ', '.join(identities)))
    return bindings

def _workspace_rows(
    root: Path,
    git: dict,
    bindings: list[dict],
    blockers: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    resources = []
    registered = {row['identity']: row for row in git.get('worktrees', [])}
    bindings_by_path = defaultdict(list)
    for binding in bindings:
        bindings_by_path[binding['path_key']].append(binding)
    binding_rows = []
    for binding in bindings:
        path = Path(binding['workspace_path'])
        registered_row = registered.get(binding['path_key'])
        path_common = _common_dir(path) if path.is_dir() else None
        if binding.get('path_conflict'):
            disposition = 'retain_unknown'
        elif not path.exists():
            disposition = 'workspace_missing'
        elif binding['control_only']:
            disposition = 'control_only'
        elif path_common and path_common != git.get('common_dir'):
            disposition = 'external_release_only' if binding['external'] else 'retain_unknown'
            blockers.append(_blocker('GIT_COMMON_DIR_MISMATCH', path,
                                     f'{path_common} != {git.get("common_dir")}',
                                     binding['identity']))
        elif binding['external']:
            disposition = 'external_release_only'
        elif registered_row is None:
            disposition = 'retain_unknown'
            blockers.append(_blocker('GIT_WORKTREE_UNREGISTERED', path,
                                     binding['identity'], binding['identity']))
        elif registered_row.get('dirty'):
            disposition = 'retain_dirty'
        elif registered_row.get('merged') is False:
            disposition = 'retain_unmerged'
        else:
            disposition = 'eligible_after_proof'
        if registered_row and binding.get('branch') and registered_row.get('branch') != binding.get('branch'):
            blockers.append(_blocker('GIT_BRANCH_MISMATCH', path,
                                     f'{binding.get("branch")} != {registered_row.get("branch")}',
                                     binding['identity']))
        binding_rows.append({
            'kind': 'source', 'domain': 'explicit_workspace_binding',
            'source_path': binding['source_path'], 'identity': binding['identity'],
            'workspace_path': binding['workspace_path'],
            'workspace_root': binding.get('workspace_root'),
            'worktree_path': binding.get('worktree_path'),
            'path_conflict': binding.get('path_conflict', False),
            'branch': binding.get('branch'),
            'classification': (
                'workspace_conflict' if binding.get('path_conflict')
                else 'workspace_present' if path.exists()
                else 'workspace_missing'
            ),
            'disposition': disposition})
        if not binding['control_only']:
            resources.append({
                'kind': 'resource', 'identity': binding['identity'],
                'workspace_path': binding['workspace_path'],
                'workspace_root': binding.get('workspace_root'),
                'worktree_path': binding.get('worktree_path'),
                'path_conflict': binding.get('path_conflict', False),
                'branch': binding.get('branch'),
                'registered': registered_row is not None,
                'dirty': registered_row.get('dirty') if registered_row else None,
                'merged': registered_row.get('merged') if registered_row else None,
                'disposition': disposition, 'deletable': False, 'disposed': False})
    registered_rows = []
    for path_key, registered_row in registered.items():
        path = Path(registered_row['path'])
        owners = bindings_by_path.get(path_key, [])
        if path.resolve(strict=False) == root:
            classification = 'control_root'
        elif owners:
            classification = 'bound_worktree'
        else:
            classification = 'unowned_worktree'
            blockers.append(_blocker('GIT_WORKTREE_UNOWNED', path,
                                     'no explicit topology owner', path_key))
            resources.append({
                'kind': 'resource', 'identity': 'unowned:' + path_key,
                'workspace_path': str(path), 'branch': registered_row.get('branch'),
                'registered': True, 'dirty': registered_row.get('dirty'),
                'merged': registered_row.get('merged'), 'disposition': 'retain_unknown',
                'deletable': False, 'disposed': False})
        registered_rows.append({
            'kind': 'source', 'domain': 'registered_git_worktree',
            'source_path': str(path), 'identity': path_key,
            'workspace_path': str(path), 'branch': registered_row.get('branch'),
            'exists': registered_row.get('exists'), 'classification': classification})
    return binding_rows, resources, registered_rows


def _classify_records_and_runs(
    works: list[dict],
    runs: list[dict],
    blockers: list[dict],
) -> list[dict]:
    duplicate_work_uuid = _duplicates(works, 'uuid')
    duplicate_work_number = _duplicates(works, 'number')
    duplicate_run_id = _duplicates(runs, 'identity')
    duplicate_run_number = _duplicates(runs, 'number')
    for value in duplicate_work_uuid:
        blockers.append(_blocker('DUPLICATE_WORK_UUID', 'docs/work', value, value))
    for value in duplicate_work_number:
        blockers.append(_blocker('DUPLICATE_WORK_NUMBER', 'docs/work', value, value))
    for value in duplicate_run_id:
        blockers.append(_blocker('DUPLICATE_RUN_ID', '.gsp/runs', value, value))
    for value in duplicate_run_number:
        blockers.append(_blocker('DUPLICATE_RUN_NUMBER', '.gsp/runs', value, value))
    for row in works:
        if row.get('uuid') in duplicate_work_uuid or row.get('number') in duplicate_work_number:
            row['classification'] = 'blocked'
    for row in runs:
        if row['identity'] in duplicate_run_id or row.get('number') in duplicate_run_number:
            row['classification'] = 'blocked'
    unique_works = {row['uuid']: row for row in works
                    if row.get('uuid') and row['uuid'] not in duplicate_work_uuid}
    unique_runs = {row['identity']: row for row in runs if row['identity'] not in duplicate_run_id}
    refs = defaultdict(list)
    for work in works:
        if isinstance(work.get('run_id'), str) and work['run_id']:
            refs[work['run_id']].append(work)
    links = []
    for work in works:
        if work.get('classification') == 'blocked':
            continue
        run_id = work.get('run_id')
        if run_id is None:
            work['classification'] = 'work_without_run'
            continue
        run = unique_runs.get(run_id)
        if run is None:
            blockers.append(_blocker('WORK_RUN_MISSING', work['source_path'], str(run_id), work['identity']))
            work['classification'] = 'blocked'
            continue
        if len(refs[run_id]) != 1 or run.get('classification') == 'blocked':
            blockers.append(_blocker('WORK_RUN_AMBIGUOUS', work['source_path'], str(run_id), work['identity']))
            work['classification'] = 'blocked'
            continue
        if run.get('work_uuid') and run['work_uuid'] != work.get('uuid'):
            blockers.append(_blocker('WORK_RUN_UUID_CONFLICT', work['source_path'],
                                     f'{work["identity"]} != {run["work_uuid"]}', work['identity']))
            work['classification'] = 'blocked'
            continue
        if run.get('number') and run['number'] != work.get('number'):
            blockers.append(_blocker('WORK_RUN_NUMBER_CONFLICT', work['source_path'],
                                     f'{work.get("number")} != {run["number"]}', work['identity']))
            work['classification'] = 'blocked'
            continue
        if not run.get('work_uuid') and not run.get('number'):
            blockers.append(_blocker('WORK_RUN_IDENTITY_MISSING', work['source_path'],
                                     'run has neither work_uuid nor number', work['identity']))
            work['classification'] = 'blocked'
            continue
        work['classification'] = 'matched_run'
        links.append({'kind': 'link', 'link_type': 'work_run',
                      'from': work['identity'], 'to': run['identity']})
    for run in runs:
        if run.get('classification') == 'blocked':
            continue
        if run.get('work_uuid'):
            work = unique_works.get(run['work_uuid'])
            if work is None or work.get('run_id') != run['identity']:
                blockers.append(_blocker('RUN_WORK_RECORD_MISSING', run['source_path'],
                                         str(run.get('work_uuid')), run['identity']))
                run['classification'] = 'blocked'
            elif work.get('classification') == 'blocked':
                run['classification'] = 'blocked'
            else:
                run['classification'] = 'matched_work'
        elif refs.get(run['identity']):
            work = refs[run['identity']][0] if len(refs[run['identity']]) == 1 else None
            run['classification'] = ('matched_work' if work and work.get('classification') == 'matched_run'
                                     else 'blocked')
        else:
            run['classification'] = 'legacy_only_run'
    return links

def _legacy_number(token: str, pattern: re.Pattern) -> int | None:
    return int(token[1:]) if pattern.fullmatch(token) else None


def _scan_session_dispatch(
    root: Path,
    runs: list[dict],
    works: list[dict],
    blockers: list[dict],
    capture: dict,
) -> tuple[list[dict], dict]:
    """Read legacy S/D registries as scoped aliases and non-reclaimable high-water marks."""
    aliases: list[dict] = []
    session_scopes: list[dict] = []
    dispatch_scopes: list[dict] = []
    works_by_uid = {row.get('uuid'): row for row in works if row.get('uuid')}
    run_by_id = defaultdict(list)
    for run in runs:
        run_by_id[str(run.get('identity'))].append(run)

    registry_paths = {
        root / relative
        for relative in capture
        if re.fullmatch(r'\.gsp/runs/[^/]+/sessions/(?:index|dispatch)\.json', relative)
    }
    known_run_ids = set(run_by_id)
    for path in sorted(registry_paths):
        parent_run_id = path.parents[1].name
        if parent_run_id not in known_run_ids:
            code = ('SESSION_REGISTRY_SCOPE_UNKNOWN'
                    if path.name == 'index.json' else 'DISPATCH_REGISTRY_SCOPE_UNKNOWN')
            blockers.append(_blocker(code, _rel(root, path), parent_run_id))

    for run in runs:
        run_id = str(run.get('identity'))
        index_path = root / '.gsp' / 'runs' / run_id / 'sessions' / 'index.json'
        dispatch_path = root / '.gsp' / 'runs' / run_id / 'sessions' / 'dispatch.json'
        index_item = _capture_item(capture, _rel(root, index_path))
        dispatch_item = _capture_item(capture, _rel(root, dispatch_path))
        if not index_item.get('exists') and not dispatch_item.get('exists'):
            continue
        scope_rows = run_by_id.get(run_id, [])
        if len(scope_rows) != 1:
            blockers.append(_blocker(
                'SESSION_DISPATCH_SCOPE_AMBIGUOUS', '.gsp/runs', run_id, run_id))
            continue
        work = works_by_uid.get(run.get('work_uuid'))
        work_identity = (run.get('number') if isinstance(run.get('number'), str) else
                         work.get('number') if work and isinstance(work.get('number'), str) else
                         work.get('uuid') if work else None)
        if run.get('classification') != 'matched_work' or not work_identity:
            code = ('SESSION_REGISTRY_SCOPE_UNKNOWN'
                    if index_item.get('exists') else 'DISPATCH_REGISTRY_SCOPE_UNKNOWN')
            source = index_path if index_item.get('exists') else dispatch_path
            blockers.append(_blocker(code, _rel(root, source),
                                     'run has no unique work scope', run_id))

        session_aliases: list[dict] = []
        dispatch_aliases: list[dict] = []
        session_hint_aliases: list[dict] = []
        valid_session_ids: set[str] = set()
        session_entries: list[dict] = []
        session_observed: list[int] = []
        session_declared: int | None = None
        session_registry_ok = True
        session_index_present = bool(index_item.get('exists'))
        if session_index_present:
            index_data, index_error = _read_captured_json(root, index_path, capture)
            if index_error or not isinstance(index_data, dict):
                blockers.append(_blocker(
                    'SESSION_REGISTRY_INVALID', _rel(root, index_path),
                    index_error or 'registry is not an object', run_id))
                session_registry_ok = False
            else:
                if index_data.get('schema') != 1:
                    blockers.append(_blocker(
                        'SESSION_REGISTRY_SCHEMA_INVALID', _rel(root, index_path),
                        repr(index_data.get('schema')), run_id))
                    session_registry_ok = False
                if index_data.get('task') != run_id:
                    blockers.append(_blocker(
                        'SESSION_REGISTRY_TASK_MISMATCH', _rel(root, index_path),
                        str(index_data.get('task')), run_id))
                    session_registry_ok = False
                declared_number = index_data.get('number')
                if declared_number is not None and declared_number != work_identity:
                    blockers.append(_blocker(
                        'SESSION_REGISTRY_WORK_MISMATCH', _rel(root, index_path),
                        str(declared_number), run_id))
                    session_registry_ok = False
                next_session = index_data.get('next_session')
                if type(next_session) is not int or next_session < 1:
                    blockers.append(_blocker(
                        'SESSION_NEXT_INVALID', _rel(root, index_path),
                        repr(next_session), run_id))
                    session_registry_ok = False
                else:
                    session_declared = next_session
                entries = index_data.get('sessions')
                if not isinstance(entries, list):
                    blockers.append(_blocker(
                        'SESSION_ENTRIES_INVALID', _rel(root, index_path),
                        'sessions is not a list', run_id))
                    session_registry_ok = False
                    entries = []
                session_entries = [row for row in entries if isinstance(row, dict)]
                if len(session_entries) != len(entries):
                    blockers.append(_blocker(
                        'SESSION_ENTRY_INVALID', _rel(root, index_path),
                        'session entry is not an object', run_id))
                    session_registry_ok = False
                counts = Counter(row.get('id') for row in session_entries
                                 if isinstance(row.get('id'), str))
                for token, count in counts.items():
                    if count > 1:
                        blockers.append(_blocker(
                            'SESSION_ID_DUPLICATE', _rel(root, index_path), token, run_id))
                        session_registry_ok = False
                task_ids = set(run.get('task_ids') or [])
                for row_index, entry in enumerate(session_entries):
                    raw_token = entry.get('id')
                    valid = isinstance(raw_token, str) and bool(SESSION_TOKEN.fullmatch(raw_token))
                    identity = raw_token if isinstance(raw_token, str) and raw_token else f'<invalid-session-{row_index}>'
                    if not valid:
                        blockers.append(_blocker(
                            'SESSION_ID_INVALID', _rel(root, index_path),
                            repr(raw_token), run_id))
                        session_registry_ok = False
                    elif raw_token in valid_session_ids:
                        pass
                    else:
                        valid_session_ids.add(raw_token)
                        session_observed.append(_legacy_number(raw_token, SESSION_TOKEN))
                    plan_task = entry.get('task')
                    if plan_task is not None and isinstance(plan_task, str) and plan_task not in task_ids:
                        blockers.append(_blocker(
                            'SESSION_TASK_UNKNOWN', _rel(root, index_path),
                            plan_task, run_id))
                        session_registry_ok = False
                    session_aliases.append({
                        'kind': 'alias', 'identity': identity, 'legacy_token': raw_token,
                        'object_kind': 'session', 'alias_kind': 'registry_session',
                        'scope_identity': run_id, 'run_identity': run_id,
                        'work_identity': work_identity, 'work_uuid': run.get('work_uuid'),
                        'plan_task': plan_task if isinstance(plan_task, str) else None,
                        'source_path': _rel(root, index_path), 'row_index': row_index,
                        'reservation': False, 'canonical_identity': None,
                        'conversion': 'scope_alias_only',
                        'classification': 'ready' if valid and session_registry_ok else 'blocked',
                    })
                controller = index_data.get('controller_session')
                if controller is not None and controller not in valid_session_ids:
                    blockers.append(_blocker(
                        'SESSION_REFERENCE_UNKNOWN', _rel(root, index_path),
                        'controller_session=' + str(controller), run_id))
                    session_registry_ok = False
                leases = index_data.get('workspace_leases')
                if not isinstance(leases, dict):
                    blockers.append(_blocker(
                        'SESSION_LEASES_INVALID', _rel(root, index_path),
                        'workspace_leases is not an object', run_id))
                    session_registry_ok = False
                else:
                    for owner in leases.values():
                        if owner not in valid_session_ids:
                            blockers.append(_blocker(
                                'SESSION_REFERENCE_UNKNOWN', _rel(root, index_path),
                                'lease owner=' + str(owner), run_id))
                            session_registry_ok = False
                for field in ('tasks', 'events', 'outbox'):
                    value = index_data.get(field)
                    expected = dict if field == 'tasks' else list
                    if not isinstance(value, expected):
                        blockers.append(_blocker(
                            'SESSION_ENTRIES_INVALID', _rel(root, index_path),
                            field + ' has invalid container', run_id))
                        session_registry_ok = False
                if isinstance(index_data.get('events'), list):
                    for event in index_data['events']:
                        if not isinstance(event, dict):
                            blockers.append(_blocker(
                                'SESSION_ENTRY_INVALID', _rel(root, index_path),
                                'event is not an object', run_id))
                            session_registry_ok = False
                            continue
                        token = event.get('session')
                        if token is not None and token not in valid_session_ids:
                            blockers.append(_blocker(
                                'SESSION_REFERENCE_UNKNOWN', _rel(root, index_path),
                                'event session=' + str(token), run_id))
                            session_registry_ok = False
                if isinstance(index_data.get('outbox'), list):
                    for event in index_data['outbox']:
                        if not isinstance(event, dict):
                            blockers.append(_blocker(
                                'SESSION_ENTRY_INVALID', _rel(root, index_path),
                                'outbox entry is not an object', run_id))
                            session_registry_ok = False
                            continue
                        token = event.get('session')
                        if token is not None and token not in valid_session_ids:
                            blockers.append(_blocker(
                                'SESSION_REFERENCE_UNKNOWN', _rel(root, index_path),
                                'outbox session=' + str(token), run_id))
                            session_registry_ok = False
                if isinstance(index_data.get('tasks'), dict):
                    for mapping in index_data['tasks'].values():
                        if not isinstance(mapping, dict):
                            blockers.append(_blocker(
                                'SESSION_ENTRY_INVALID', _rel(root, index_path),
                                'task mapping is not an object', run_id))
                            session_registry_ok = False
                            continue
                        for field in ('owner_session', 'author_session', 'report_session'):
                            token = mapping.get(field)
                            if token is not None and token not in valid_session_ids:
                                blockers.append(_blocker(
                                    'SESSION_REFERENCE_UNKNOWN', _rel(root, index_path),
                                    field + '=' + str(token), run_id))
                                session_registry_ok = False
                if session_declared is not None and session_observed:
                    if session_declared <= max(session_observed):
                        blockers.append(_blocker(
                            'SESSION_NEXT_NOT_ABOVE_OBSERVED', _rel(root, index_path),
                            f'next_session={session_declared} observed={max(session_observed)}',
                            run_id))
                        session_registry_ok = False
        else:
            session_registry_ok = False

        dispatch_entries: list[dict] = []
        dispatch_observed: list[int] = []
        dispatch_declared: int | None = None
        dispatch_registry_ok = True
        dispatch_session_hints: list[int] = []
        dispatch_present = bool(dispatch_item.get('exists'))
        if dispatch_present:
            dispatch_data, dispatch_error = _read_captured_json(root, dispatch_path, capture)
            if dispatch_error or not isinstance(dispatch_data, dict):
                blockers.append(_blocker(
                    'DISPATCH_REGISTRY_INVALID', _rel(root, dispatch_path),
                    dispatch_error or 'registry is not an object', run_id))
                dispatch_registry_ok = False
            else:
                if dispatch_data.get('schema') != 1:
                    blockers.append(_blocker(
                        'DISPATCH_REGISTRY_SCHEMA_INVALID', _rel(root, dispatch_path),
                        repr(dispatch_data.get('schema')), run_id))
                    dispatch_registry_ok = False
                if dispatch_data.get('task') != run_id:
                    blockers.append(_blocker(
                        'DISPATCH_REGISTRY_TASK_MISMATCH', _rel(root, dispatch_path),
                        str(dispatch_data.get('task')), run_id))
                    dispatch_registry_ok = False
                next_dispatch = dispatch_data.get('next_dispatch')
                if type(next_dispatch) is not int or next_dispatch < 1:
                    blockers.append(_blocker(
                        'DISPATCH_NEXT_INVALID', _rel(root, dispatch_path),
                        repr(next_dispatch), run_id))
                    dispatch_registry_ok = False
                else:
                    dispatch_declared = next_dispatch
                entries = dispatch_data.get('dispatches')
                if not isinstance(entries, list):
                    blockers.append(_blocker(
                        'DISPATCH_ENTRIES_INVALID', _rel(root, dispatch_path),
                        'dispatches is not a list', run_id))
                    dispatch_registry_ok = False
                    entries = []
                dispatch_entries = [row for row in entries if isinstance(row, dict)]
                if len(dispatch_entries) != len(entries):
                    blockers.append(_blocker(
                        'DISPATCH_ENTRY_INVALID', _rel(root, dispatch_path),
                        'dispatch entry is not an object', run_id))
                    dispatch_registry_ok = False
                counts = Counter(row.get('id') for row in dispatch_entries
                                 if isinstance(row.get('id'), str))
                for token, count in counts.items():
                    if count > 1:
                        blockers.append(_blocker(
                            'DISPATCH_ID_DUPLICATE', _rel(root, dispatch_path),
                            token, run_id))
                        dispatch_registry_ok = False
                task_ids = set(run.get('task_ids') or [])
                if not run.get('tasks_valid', True) and dispatch_entries:
                    blockers.append(_blocker(
                        'DISPATCH_TASK_ENTRIES_INVALID', _rel(root, dispatch_path),
                        'run state tasks container is invalid', run_id))
                    dispatch_registry_ok = False
                valid_dispatch_ids: set[str] = set()
                for row_index, entry in enumerate(dispatch_entries):
                    raw_token = entry.get('id')
                    valid = isinstance(raw_token, str) and bool(DISPATCH_TOKEN.fullmatch(raw_token))
                    identity = raw_token if isinstance(raw_token, str) and raw_token else f'<invalid-dispatch-{row_index}>'
                    if not valid:
                        blockers.append(_blocker(
                            'DISPATCH_ID_INVALID', _rel(root, dispatch_path),
                            repr(raw_token), run_id))
                        dispatch_registry_ok = False
                    elif raw_token not in valid_dispatch_ids:
                        valid_dispatch_ids.add(raw_token)
                        dispatch_observed.append(_legacy_number(raw_token, DISPATCH_TOKEN))
                    plan_task = entry.get('task')
                    if not isinstance(plan_task, str) or plan_task not in task_ids:
                        blockers.append(_blocker(
                            'DISPATCH_TASK_UNKNOWN', _rel(root, dispatch_path),
                            str(plan_task), run_id))
                        dispatch_registry_ok = False
                    dispatch_aliases.append({
                        'kind': 'alias', 'identity': identity, 'legacy_token': raw_token,
                        'object_kind': 'dispatch', 'alias_kind': 'dispatch',
                        'scope_identity': run_id, 'run_identity': run_id,
                        'work_identity': work_identity, 'work_uuid': run.get('work_uuid'),
                        'plan_task': plan_task if isinstance(plan_task, str) else None,
                        'source_path': _rel(root, dispatch_path), 'row_index': row_index,
                        'canonical_identity': None, 'conversion': 'scope_alias_only',
                        'classification': 'ready' if valid and dispatch_registry_ok else 'blocked',
                    })
                    hint = entry.get('session_hint')
                    if hint is not None and (not isinstance(hint, str)
                                             or not SESSION_TOKEN.fullmatch(hint)):
                        blockers.append(_blocker(
                            'DISPATCH_SESSION_HINT_INVALID', _rel(root, dispatch_path),
                            repr(hint), run_id))
                        dispatch_registry_ok = False
                    hint_identity = hint if isinstance(hint, str) and hint else f'<invalid-hint-{row_index}>'
                    if hint is not None:
                        if isinstance(hint, str) and SESSION_TOKEN.fullmatch(hint):
                            dispatch_session_hints.append(_legacy_number(hint, SESSION_TOKEN))
                        session_hint_aliases.append({
                            'kind': 'alias', 'identity': hint_identity, 'legacy_token': hint,
                            'object_kind': 'session', 'alias_kind': 'dispatch_session_hint',
                            'scope_identity': run_id, 'run_identity': run_id,
                            'work_identity': work_identity, 'work_uuid': run.get('work_uuid'),
                            'plan_task': plan_task if isinstance(plan_task, str) else None,
                            'source_path': _rel(root, dispatch_path), 'row_index': row_index,
                            'reservation': True, 'canonical_identity': None,
                            'conversion': 'scope_alias_only',
                            'classification': ('ready' if isinstance(hint, str)
                                               and SESSION_TOKEN.fullmatch(hint) else 'blocked'),
                        })
                cleanup = dispatch_data.get('cleanup')
                if not isinstance(cleanup, list):
                    blockers.append(_blocker(
                        'DISPATCH_ENTRIES_INVALID', _rel(root, dispatch_path),
                        'cleanup is not a list', run_id))
                    dispatch_registry_ok = False
                else:
                    dispatch_ids = {row.get('id') for row in dispatch_entries
                                    if isinstance(row.get('id'), str)}
                    for row in cleanup:
                        if not isinstance(row, dict):
                            blockers.append(_blocker(
                                'DISPATCH_ENTRY_INVALID', _rel(root, dispatch_path),
                                'cleanup entry is not an object', run_id))
                            dispatch_registry_ok = False
                            continue
                        token = row.get('dispatch_id')
                        if token is not None and token not in dispatch_ids:
                            blockers.append(_blocker(
                                'DISPATCH_REFERENCE_UNKNOWN', _rel(root, dispatch_path),
                                str(token), run_id))
                            dispatch_registry_ok = False
                if dispatch_declared is not None and dispatch_observed:
                    if dispatch_declared <= max(dispatch_observed):
                        blockers.append(_blocker(
                            'DISPATCH_NEXT_NOT_ABOVE_OBSERVED', _rel(root, dispatch_path),
                            f'next_dispatch={dispatch_declared} observed={max(dispatch_observed)}',
                            run_id))
                        dispatch_registry_ok = False
        else:
            dispatch_registry_ok = False

        aliases.extend(session_aliases)
        aliases.extend(dispatch_aliases)
        aliases.extend(session_hint_aliases)
        observed_session_max = max(session_observed) if session_observed else None
        reserved_session_max = max(dispatch_session_hints) if dispatch_session_hints else None
        session_candidates = [value for value in (
            session_declared,
            observed_session_max + 1 if observed_session_max is not None else None,
            reserved_session_max + 1 if reserved_session_max is not None else None,
        ) if value is not None]
        if session_index_present or dispatch_present:
            session_scopes.append({
                'scope_identity': run_id, 'run_identity': run_id,
                'work_identity': work_identity, 'work_uuid': run.get('work_uuid'),
                'source_paths': [rel for rel in (
                    _rel(root, index_path) if index_item.get('exists') else None,
                    _rel(root, dispatch_path) if dispatch_item.get('exists') else None,
                ) if rel],
                'state': 'ready' if session_registry_ok and session_index_present else
                         'derived_from_dispatch' if session_index_present is False and dispatch_present else
                         'invalid',
                'observed_max': observed_session_max,
                'reserved_max': reserved_session_max,
                'declared_next': session_declared,
                'effective_next': max(session_candidates or [1]),
            })
        if dispatch_present:
            observed_dispatch_max = max(dispatch_observed) if dispatch_observed else None
            dispatch_candidates = [value for value in (
                dispatch_declared,
                observed_dispatch_max + 1 if observed_dispatch_max is not None else None,
            ) if value is not None]
            dispatch_scopes.append({
                'scope_identity': run_id, 'run_identity': run_id,
                'work_identity': work_identity, 'work_uuid': run.get('work_uuid'),
                'source_paths': [_rel(root, dispatch_path)],
                'state': 'ready' if dispatch_registry_ok else 'invalid',
                'observed_max': observed_dispatch_max,
                'declared_next': dispatch_declared,
                'effective_next': max(dispatch_candidates or [1]),
            })
    return aliases, {
        'policy': 'preview_only_never_reclaim',
        'sessions': sorted(session_scopes, key=lambda row: row['scope_identity']),
        'dispatches': sorted(dispatch_scopes, key=lambda row: row['scope_identity']),
    }

def _manifest_uid(manifest: MigrationManifest) -> str:
    payload = {
        'schema': manifest.schema, 'source_digest': manifest.source_digest,
        'project_uid': manifest.project_uid,
        'allocator_incarnation': manifest.allocator_incarnation,
        'source_counts': manifest.source_counts,
        'classification_manifest': manifest.classification_manifest,
        'objects': manifest.objects, 'aliases': manifest.aliases,
        'resources': manifest.resources, 'blockers': manifest.blockers,
        'backup_policy': manifest.backup_policy,
    }
    return _sha(_json(payload).encode('utf-8'))[:32]


def preview_migration(root: Path) -> MigrationManifest:
    """Build a content-addressed preview without repairing or migrating source."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise WorkflowError('MIGRATION_ROOT_MISSING ' + str(root))
    before, capture = _capture_source_state(root)
    blockers: list[dict] = []
    _capture_blockers(root, capture, blockers)
    authority = _authority(root, blockers, capture)
    works = _scan_work_records(root, blockers, capture)
    runs = _scan_runs(root, blockers, capture)
    _execution_blockers(root, before['execution'], runs, blockers)
    directories = _task_directories(root)
    record_dirs = {row['directory'] for row in works}
    directory_rows = []
    for directory in directories:
        relative = _rel(root, directory)
        if relative in record_dirs:
            classification = 'record_backed_directory'
        else:
            classification = 'orphan_directory'
            blockers.append(_blocker('TASK_DIRECTORY_ORPHAN', relative,
                                     'no record.json exists'))
        directory_rows.append({
            'kind': 'source', 'domain': 'task_directory', 'source_path': relative,
            'identity': relative, 'classification': classification})
    for relative in sorted(record_dirs - {_rel(root, path) for path in directories}):
        directory_rows.append({
            'kind': 'source', 'domain': 'task_directory', 'source_path': relative,
            'identity': relative, 'classification': 'record_backed_directory'})
    todos, aliases, todo_meta = _scan_todos(root, blockers, capture)
    _index, index_state = _index_validate(root, works, blockers, capture)
    _validate_todo_document(todos, capture, index_state, blockers)
    counter = _counter(root, works, runs, todos, authority, capture, blockers)
    project_ids = {row.get('project_id') for row in runs if row.get('project_id')}
    if len(project_ids) > 1:
        blockers.append(_blocker('PROJECT_UID_MISMATCH', '.gsp/runs',
                                 ', '.join(sorted(project_ids))))
    project_uid = next(iter(project_ids), authority.get('project_uid') or '')
    if authority.get('project_uid') and project_uid and authority['project_uid'] != project_uid:
        blockers.append(_blocker('PROJECT_AUTHORITY_MISMATCH',
                                 '.gsp/orchestration/registry.sqlite',
                                 f'{authority["project_uid"]} != {project_uid}'))
    bindings = _bindings(root, runs, blockers)
    git = before['git']
    if git.get('status') == 'not-a-repository':
        blockers.append(_blocker('GIT_NOT_REPOSITORY', '.', git.get('detail', 'not a repository')))
    elif git.get('status') != 'ok':
        blockers.append(_blocker('GIT_PROBE_FAILED', '.', git.get('detail', 'probe failed')))
    binding_rows, resources, registered_rows = _workspace_rows(root, git, bindings, blockers)
    links = _classify_records_and_runs(works, runs, blockers)
    for work in works:
        links.append({'kind': 'link', 'link_type': 'work_directory',
                      'from': work['identity'], 'to': work['directory']})
    session_dispatch_aliases, session_dispatch_water = _scan_session_dispatch(
        root, runs, works, blockers, capture,
    )
    sources = [*works, *runs, *directory_rows, *todos, *binding_rows, *registered_rows]
    source_counts = {domain: sum(row['domain'] == domain for row in sources)
                     for domain in DOMAINS}
    source_counts['work_matched_run'] = sum(
        row.get('classification') == 'matched_run' for row in works)
    source_counts['work_without_run'] = sum(
        row.get('classification') == 'work_without_run' for row in works)
    source_counts['work_blocked'] = sum(
        row.get('classification') == 'blocked' for row in works)
    source_counts['run_matched_work'] = sum(
        row.get('classification') == 'matched_work' for row in runs)
    source_counts['run_legacy_only'] = sum(
        row.get('classification') == 'legacy_only_run' for row in runs)
    source_counts['run_blocked'] = sum(
        row.get('classification') == 'blocked' for row in runs)
    after, _after_capture = _capture_source_state(root)
    if before != after:
        blockers.append(_blocker('MIGRATION_SOURCE_CHANGED', '.',
                                 'source or Git observation changed during preview'))
    ordered_blockers = sorted(
        blockers,
        key=lambda row: (row['code'], row['source_path'], row.get('identity', ''), row['detail']),
    )
    source_digest = _sha(_json(before).encode('utf-8'))
    backup = dict(BACKUP_POLICY)
    backup['authority'] = authority
    backup['counter'] = counter
    backup['todo'] = todo_meta
    backup['session_dispatch'] = session_dispatch_water
    backup['source_capture'] = {
        'state': 'complete' if not before['read_errors'] else 'unknown',
        'unavailable': [
            {'path': relative, 'error': error}
            for relative, error in sorted(before['read_errors'].items())
        ],
        'membership': before['membership'],
    }
    manifest = MigrationManifest(
        uid='pending', schema=1, source_digest=source_digest,
        project_uid=project_uid,
        allocator_incarnation=authority.get('incarnation'),
        source_counts=source_counts,
        classification_manifest=tuple(sources),
        objects=tuple([*sources, *links]), aliases=tuple([*aliases, *session_dispatch_aliases]),
        resources=tuple(resources), blockers=tuple(ordered_blockers),
        backup_policy=backup,
    )
    return MigrationManifest(**{**manifest.__dict__, 'uid': _manifest_uid(manifest)})


def verify_source(root: Path, manifest: MigrationManifest) -> None:
    """Reject source drift or a modified manifest without attempting recovery."""
    if not isinstance(manifest, MigrationManifest):
        raise WorkflowError('MIGRATION_MANIFEST_CHANGED invalid manifest type')
    if _manifest_uid(manifest) != manifest.uid:
        raise WorkflowError('MIGRATION_MANIFEST_CHANGED content digest mismatch')
    root = Path(root).resolve()
    current = _source_digest(root)
    if current != manifest.source_digest:
        raise WorkflowError(
            'MIGRATION_SOURCE_CHANGED preview is stale; rebuild preview before migration')
    if any(not isinstance(row, dict) or 'code' not in row for row in manifest.blockers):
        raise WorkflowError('MIGRATION_MANIFEST_CHANGED malformed blocker evidence')
    unique = {(row.get('domain'), row.get('source_path'), row.get('identity'))
              for row in manifest.classification_manifest}
    if len(unique) != len(manifest.classification_manifest):
        raise WorkflowError('MIGRATION_MANIFEST_CHANGED duplicate classification keys')


def verify_source_for_apply(
    root: Path,
    manifest: MigrationManifest,
    *,
    ignored_paths: Iterable[str],
) -> None:
    """Verify source while excluding only the caller-attested transient paths."""
    if not isinstance(manifest, MigrationManifest):
        raise WorkflowError('MIGRATION_MANIFEST_CHANGED invalid manifest type')
    if _manifest_uid(manifest) != manifest.uid:
        raise WorkflowError('MIGRATION_MANIFEST_CHANGED content digest mismatch')
    paths = tuple(sorted(set(str(value).replace('\\', '/') for value in ignored_paths)))
    if any(
        not value
        or value.startswith('/')
        or ':' in value
        or '/../' in '/' + value.strip('/') + '/'
        for value in paths
    ):
        raise WorkflowError('MIGRATION_IGNORE_PATH_INVALID')
    root = Path(root).resolve()
    current = _source_digest(
        root,
        ignored_source_paths=paths,
        ignored_git_paths=paths,
    )
    if current != manifest.source_digest:
        raise WorkflowError(
            'MIGRATION_SOURCE_CHANGED preview is stale; rebuild preview before migration')
    if manifest.blockers:
        raise WorkflowError(
            'MIGRATION_BLOCKED '
            + ','.join(sorted({row['code'] for row in manifest.blockers})))
    unique = {
        (row.get('domain'), row.get('source_path'), row.get('identity'))
        for row in manifest.classification_manifest
    }
    if len(unique) != len(manifest.classification_manifest):
        raise WorkflowError('MIGRATION_MANIFEST_CHANGED duplicate classification keys')
