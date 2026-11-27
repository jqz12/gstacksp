"""workflow locks; shared business implementation, independent of CLI."""
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

from .core import (WorkflowError, atomic, inside, now, read, redact)

_WRITER_LOCK_GUARD = threading.RLock()


_WRITER_LOCK_OWNERS = {}


_WRITER_REACQUIRE_SECONDS = 10.0


def process_token(pid=None):
    """Return a best-effort process creation token; never infer liveness from PID alone."""
    pid = int(pid or os.getpid())
    if pid == os.getpid():
        try:
            return f'self:{os.getpid()}'
        except Exception:
            return None
    proc = Path(f'/proc/{pid}/stat')
    try:
        fields = proc.read_text(encoding='utf-8').split()
        return f'proc-start:{fields[21]}'
    except (OSError, IndexError, ValueError):
        pass
    if os.name == 'nt':
        try:
            import ctypes
            from ctypes import wintypes
            handle = ctypes.windll.kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
            if handle:
                created = wintypes.FILETIME(); exited = wintypes.FILETIME(); kernel = wintypes.FILETIME(); user = wintypes.FILETIME()
                ok = ctypes.windll.kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user))
                ctypes.windll.kernel32.CloseHandle(handle)
                if ok:
                    value = (created.dwHighDateTime << 32) | created.dwLowDateTime
                    return f'win-start:{value}'
        except (OSError, AttributeError):
            pass
    return None


def _advisory(fd, blocking):
    if _fcntl is not None:
        flags = _fcntl.LOCK_EX | (0 if blocking else _fcntl.LOCK_NB)
        try:
            _fcntl.flock(fd, flags); return True
        except (BlockingIOError, OSError): return False
    if _msvcrt is not None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            _msvcrt.locking(fd, _msvcrt.LK_LOCK if blocking else _msvcrt.LK_NBLCK, 1); return True
        except OSError: return False
    return True


def _advisory_release(fd):
    if _fcntl is not None:
        with contextlib.suppress(OSError): _fcntl.flock(fd, _fcntl.LOCK_UN)
    elif _msvcrt is not None:
        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET); _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)


def _lock_paths(root):
    resolved = Path(root).resolve()
    folder = resolved / '.gsp'
    return resolved, folder, folder / 'writer.mutex', folder / 'writer.lock'


def _open_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    if os.fstat(fd).st_size == 0:
        os.ftruncate(fd, 1)
        os.fsync(fd)
    return fd


def _metadata_owned_by_current_process(metadata):
    return (
        isinstance(metadata, dict)
        and metadata.get('pid') == os.getpid()
        and metadata.get('process_token') == process_token()
    )


def inspect_lock(root):
    """Read and classify the stable writer mutex without creating project paths."""
    resolved, folder, mutex, lock = _lock_paths(root)
    if not inside(resolved, folder):
        raise WorkflowError('STATE_ESCAPE .gsp resolves outside the project')
    if not folder.is_dir():
        return {'path': str(lock), 'mutex': str(mutex), 'status': 'absent'}
    result = {'path': str(lock), 'status': 'absent'}
    if not lock.exists():
        suspension = folder / 'writer-suspended.json'
        if suspension.is_file():
            try:
                metadata = json.loads(suspension.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                return {
                    **result, 'status': 'suspended',
                    'suspension': str(suspension), 'error': str(exc),
                }
            return {
                **result, 'status': 'suspended',
                'suspension': str(suspension), 'metadata': metadata,
            }
        return result
    metadata = None; read_error = None
    try:
        metadata = json.loads(lock.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        read_error = exc
    busy = False
    try:
        if mutex.exists():
            fd = os.open(mutex, os.O_RDWR)
            try:
                busy = not _advisory(fd, False)
            finally:
                os.close(fd)
        if not busy:
            fd = os.open(lock, os.O_RDWR)
            try:
                legacy_busy = not _advisory(fd, False)
                if not legacy_busy:
                    _advisory_release(fd)
                busy = legacy_busy
            finally:
                os.close(fd)
    except OSError:
        busy = True
    if busy and read_error is not None:
        return {**result, 'status': 'live', 'error': str(read_error)}
    if read_error is not None:
        return {**result, 'status': 'malformed', 'error': str(read_error)}
    result.update({'metadata': metadata})
    if not isinstance(metadata, dict) or not isinstance(metadata.get('pid'), int):
        result['status'] = 'malformed'; return result
    token = process_token(metadata['pid']); result['process_token'] = token
    if busy: result['status'] = 'live'
    elif token is None: result['status'] = 'unknown'
    elif metadata.get('process_token') and metadata.get('process_token') != token:
        result['status'] = 'unknown'; result['reason'] = 'PID_REUSED'
    else: result['status'] = 'dead'
    return result


def recover_lock(root, evidence, reason):
    """Release only a demonstrably dead, well-formed writer metadata file."""
    info = inspect_lock(root)
    if info['status'] != 'dead':
        raise WorkflowError(f"LOCK_RECOVERY_REFUSED status={info['status']} reason=owner_not_proven_dead")
    if not evidence or not Path(evidence).is_file():
        raise WorkflowError('LOCK_RECOVERY_EVIDENCE_REQUIRED provide an investigation report')
    resolved, folder, mutex, lock = _lock_paths(root)
    mutex_fd = _open_lock(mutex)
    if not _advisory(mutex_fd, False):
        os.close(mutex_fd)
        raise WorkflowError(
            f'LOCK_RECOVERY_REFUSED status=live mutex={mutex} '
            'reason=owner_not_proven_dead'
        )
    legacy_fd = None
    try:
        try:
            legacy_fd = os.open(lock, os.O_RDWR)
            if not _advisory(legacy_fd, False):
                raise WorkflowError(
                    'LOCK_RECOVERY_REFUSED status=live '
                    'reason=legacy_owner_not_proven_dead'
                )
            os.lseek(legacy_fd, 0, os.SEEK_SET)
            current = json.loads(os.read(legacy_fd, 1024 * 1024).decode('utf-8'))
            if (
                not isinstance(current, dict)
                or not isinstance(current.get('pid'), int)
            ):
                raise WorkflowError(
                    'LOCK_RECOVERY_REFUSED status=unknown '
                    'reason=owner_changed_during_recovery'
                )
            token = process_token(current.get('pid'))
            if token is None or (
                current.get('process_token')
                and current.get('process_token') != token
            ):
                raise WorkflowError(
                    'LOCK_RECOVERY_REFUSED status=unknown '
                    'reason=owner_changed_during_recovery'
                )
        except (OSError, ValueError, TypeError) as exc:
            raise WorkflowError(
                'LOCK_RECOVERY_REFUSED status=malformed '
                'reason=metadata_changed_during_recovery'
            ) from exc
        history = folder / 'recovery'
        history.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex
        backup = history / f'writer-lock-{stamp}.json'
        atomic(backup, {'lock': info, 'evidence': str(Path(evidence).resolve()), 'reason': redact(reason), 'at': now()})
        _advisory_release(legacy_fd)
        os.close(legacy_fd)
        legacy_fd = None
        lock.unlink(missing_ok=True)
    finally:
        if legacy_fd is not None:
            _advisory_release(legacy_fd)
            os.close(legacy_fd)
        _advisory_release(mutex_fd)
        os.close(mutex_fd)
    return {'status': 'recovered', 'backup': str(backup), 'evidence': str(Path(evidence).resolve())}


@contextmanager
def locked(root):
    """Use one stable, reentrant OS mutex with separate diagnostic metadata."""
    resolved, folder, mutex, lock = _lock_paths(root)
    if not inside(resolved, folder):
        raise WorkflowError('STATE_ESCAPE .gsp resolves outside the project')
    folder.mkdir(parents=True, exist_ok=True)
    thread_id = threading.get_ident()
    with _WRITER_LOCK_GUARD:
        current = _WRITER_LOCK_OWNERS.get(str(resolved))
        if current is not None:
            if current['thread_id'] != thread_id:
                raise WorkflowError(
                    f'WORKFLOW_BUSY {lock}; owned by another thread in this process'
                )
            if current['paused'] and not current.get('nested_holding'):
                if not _advisory(current['fd'], False):
                    raise WorkflowError(
                        f'WORKFLOW_BUSY {lock}; suspended writer cannot '
                        'reacquire its short-transaction mutex'
                    )
                try:
                    atomic(lock, {
                        'schema': 2, 'state': 'held', 'pid': os.getpid(),
                        'process_token': current['process_token'], 'at': now(),
                    })
                except Exception:
                    _advisory_release(current['fd'])
                    raise
                current['nested_holding'] = True
            current['depth'] += 1
        else:
            fd = _open_lock(mutex)
            if not _advisory(fd, False):
                os.close(fd)
                raise WorkflowError(
                    f'WORKFLOW_BUSY {lock}; inspect owner, do not delete a live writer lock'
                )
            legacy_fd = None
            try:
                if lock.exists():
                    legacy_fd = os.open(lock, os.O_RDWR)
                    if not _advisory(legacy_fd, False):
                        raise WorkflowError(
                            f'WORKFLOW_BUSY {lock}; a legacy writer owns the diagnostic lock'
                        )
                metadata = {
                    'schema': 2, 'state': 'held', 'pid': os.getpid(),
                    'process_token': process_token(), 'at': now(),
                }
                atomic(lock, metadata)
            except Exception:
                if legacy_fd is not None:
                    _advisory_release(legacy_fd)
                    os.close(legacy_fd)
                _advisory_release(fd)
                os.close(fd)
                raise
            if legacy_fd is not None:
                _advisory_release(legacy_fd)
                os.close(legacy_fd)
            current = {
                'fd': fd, 'thread_id': thread_id, 'depth': 1,
                'paused': False, 'nested_holding': False,
                'mutex': mutex, 'lock': lock,
                'process_token': metadata['process_token'],
            }
            _WRITER_LOCK_OWNERS[str(resolved)] = current
    try:
        yield
    finally:
        with _WRITER_LOCK_GUARD:
            current = _WRITER_LOCK_OWNERS.get(str(resolved))
            if current is None or current['thread_id'] != thread_id:
                raise WorkflowError('WRITER_LOCK_STATE_LOST')
            current['depth'] -= 1
            if current['depth'] == 0:
                if current.get('nested_holding'):
                    _advisory_release(current['fd'])
                    current['nested_holding'] = False
                if not current['paused']:
                    try:
                        metadata = read(lock)
                    except (OSError, WorkflowError):
                        metadata = None
                    if _metadata_owned_by_current_process(metadata):
                        with contextlib.suppress(OSError):
                            lock.unlink()
                _advisory_release(current['fd'])
                os.close(current['fd'])
                del _WRITER_LOCK_OWNERS[str(resolved)]
            elif (current['paused'] and current.get('nested_holding')
                  and current['depth'] == current.get('suspended_depth', 1)):
                try:
                    metadata = read(lock)
                except (OSError, WorkflowError):
                    metadata = None
                if _metadata_owned_by_current_process(metadata):
                    with contextlib.suppress(OSError):
                        lock.unlink()
                _advisory_release(current['fd'])
                current['nested_holding'] = False


def _reacquire_writer_mutex(fd, lock_path, timeout):
    deadline = time.monotonic() + timeout
    while True:
        if _advisory(fd, False):
            return
        if time.monotonic() >= deadline:
            raise WorkflowError(
                f'WORKFLOW_BUSY resumed lock unavailable {lock_path}; '
                'preserve inflight evidence and inspect the competing writer'
            )
        time.sleep(.05)


@contextmanager
def suspend_writer_lock(root, *, timeout=None):
    """Release this thread's writer mutex only while a long child process runs."""
    resolved, folder, mutex, lock = _lock_paths(root)
    thread_id = threading.get_ident()
    with _WRITER_LOCK_GUARD:
        current = _WRITER_LOCK_OWNERS.get(str(resolved))
        if current is None:
            yield None
            return
        if current['thread_id'] != thread_id:
            raise WorkflowError(
                f'WORKFLOW_BUSY {lock}; another thread owns the writer mutex'
            )
        if current.get('nested_holding'):
            raise WorkflowError('WRITER_LOCK_NESTED_TRANSACTION_ACTIVE')
        if current['paused']:
            raise WorkflowError('WRITER_LOCK_ALREADY_SUSPENDED')
        reacquire_timeout = (
            _WRITER_REACQUIRE_SECONDS if timeout is None else float(timeout)
        )
        suspension = folder / 'writer-suspended.json'
        atomic(suspension, {
            'schema': 1, 'state': 'suspended', 'pid': os.getpid(),
            'process_token': current['process_token'], 'at': now(),
        })
        with contextlib.suppress(OSError):
            lock.unlink()
        _advisory_release(current['fd'])
        current['paused'] = True
        current['suspended_depth'] = current['depth']
    try:
        yield current
    finally:
        with _WRITER_LOCK_GUARD:
            current = _WRITER_LOCK_OWNERS.get(str(resolved))
            failure = None
            if current is None or current['thread_id'] != thread_id:
                failure = WorkflowError('WRITER_LOCK_STATE_LOST during execution')
            elif current.get('nested_holding'):
                failure = WorkflowError(
                    'WRITER_LOCK_NESTED_TRANSACTION_ACTIVE during resume'
                )
            else:
                try:
                    _reacquire_writer_mutex(
                        current['fd'], lock, reacquire_timeout,
                    )
                    current['paused'] = False
                    current.pop('suspended_depth', None)
                    atomic(lock, {
                        'schema': 2, 'state': 'held', 'pid': os.getpid(),
                        'process_token': current['process_token'], 'at': now(),
                    })
                    with contextlib.suppress(OSError):
                        suspension.unlink()
                except Exception as exc:
                    failure = exc
        if failure is not None:
            raise failure
