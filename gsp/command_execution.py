"""Streaming command execution with bounded logs and observable progress.

The controller keeps ``args.runner`` for existing fake-runner tests, but real
commands use this module.  It drains both pipes continuously, redacts known
secret patterns, caps each stream, and publishes a heartbeat while the child
is quiet.  It never treats a missing parent or an unknown final state as pass.
"""
import codecs
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from .core import WorkflowError, atomic, digest, read, redact


MAX_STREAM_BYTES = 10 * 1024 * 1024
MAX_LINE_BUFFER = 64 * 1024
CARRY_CHARS = 8 * 1024
READ_CHUNK = 64 * 1024
MAX_INPUT_BYTES = 1024 * 1024
HEARTBEAT_SECONDS = 2.0
WAIT_LIMIT_SECONDS = 30.0


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _identity(pid):
    from .hosts import identity
    return identity(pid)


class _RedactingStream:
    def __init__(self, path, limit=None):
        self.path = Path(path)
        self.limit = MAX_STREAM_BYTES if limit is None else limit
        self.file = self.path.open('wb')
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.pending = ''
        self.raw_bytes = 0
        self.written = 0
        self.truncated = False

    def feed(self, chunk):
        self.raw_bytes += len(chunk)
        self.pending += self.decoder.decode(chunk)
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            self._write(line + '\n')
        if len(self.pending) > MAX_LINE_BUFFER:
            cut = len(self.pending) - CARRY_CHARS
            self._write(self.pending[:cut])
            self.pending = self.pending[cut:]

    def _write(self, text):
        if self.truncated:
            return
        data = redact(text).encode('utf-8')
        remaining = self.limit - self.written
        if len(data) > remaining:
            if remaining > 0:
                self.file.write(data[:remaining])
                self.written += remaining
            self.truncated = True
        else:
            self.file.write(data)
            self.written += len(data)
        self.file.flush()

    def close(self):
        self.pending += self.decoder.decode(b'', final=True)
        if self.pending:
            self._write(self.pending)
            self.pending = ''
        self.file.flush()
        try:
            os.fsync(self.file.fileno())
        except OSError:
            pass
        self.file.close()
        return {'path': str(self.path), 'sha256': digest(self.path),
                'bytes': self.written, 'raw_bytes': self.raw_bytes,
                'truncated': self.truncated}


def _drain(pipe, stream):
    try:
        while True:
            chunk = pipe.read(READ_CHUNK)
            if not chunk:
                break
            stream.feed(chunk)
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _input_payload(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = value.encode('utf-8')
        except UnicodeError as exc:
            raise WorkflowError('COMMAND_INPUT_INVALID expected UTF-8 text or bytes') from exc
    elif isinstance(value, (bytearray, memoryview)):
        value = bytes(value)
    elif not isinstance(value, bytes):
        raise WorkflowError('COMMAND_INPUT_INVALID expected text or bytes')
    if len(value) > MAX_INPUT_BYTES:
        raise WorkflowError(
            f'COMMAND_INPUT_TOO_LARGE limit is {MAX_INPUT_BYTES} bytes')
    return value


def _write_input(pipe, payload, state):
    offset = 0
    try:
        fd = pipe.fileno()
        while offset < len(payload):
            chunk = payload[offset:offset + READ_CHUNK]
            written = os.write(fd, chunk)
            if written < 1:
                raise OSError('stdin write made no progress')
            offset += written
            state['written'] = offset
        state['complete'] = True
    except (BrokenPipeError, OSError) as exc:
        state['error'] = type(exc).__name__
    except Exception as exc:
        state['error'] = type(exc).__name__
    finally:
        try:
            pipe.close()
        except OSError:
            pass
        state['closed'] = True


def _heartbeat_document(evidence_id, folder, metadata, process_identity, parent,
                        started, status, stdout, stderr):
    return {
        'schema': 1, 'evidence_id': evidence_id, 'task': metadata.get('task'),
        'phase': 'command', 'status': status, 'started_at': metadata.get('started_at'),
        'heartbeat_at': _stamp(), 'elapsed_s': round(time.monotonic() - started, 3),
        'timeout_s': metadata.get('timeout_s'),
        'argv': [redact(str(value)) for value in (metadata.get('argv') or [])],
        'build_context': dict(metadata.get('build_context') or {}),
        'process_identity': process_identity,
        'parent': parent,
        'stdout': {'path': str(stdout.path), 'bytes': stdout.written,
                    'raw_bytes': stdout.raw_bytes, 'truncated': stdout.truncated},
        'stderr': {'path': str(stderr.path), 'bytes': stderr.written,
                    'raw_bytes': stderr.raw_bytes, 'truncated': stderr.truncated},
        'folder': str(folder),
    }


def _write_progress(folder, document):
    atomic(Path(folder) / 'command-progress.json', document)


def execute_command(argv, cwd, env, timeout, folder, metadata, on_started=None,
                    input_data=None, cancel_check=None):
    """Run one native child process and stream bounded, redacted logs."""
    input_payload = _input_payload(input_data)
    input_state = (None if input_payload is None else {
        'bytes': len(input_payload), 'written': 0,
        'complete': False, 'closed': False, 'error': None,
    })
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    evidence_id = folder.name
    stdout = _RedactingStream(folder / 'stdout.txt', limit=MAX_STREAM_BYTES)
    stderr = _RedactingStream(folder / 'stderr.txt', limit=MAX_STREAM_BYTES)
    started = time.monotonic()
    started_at = _stamp()
    metadata = dict(metadata or {})
    metadata['started_at'] = started_at
    metadata['timeout_s'] = timeout
    from .hosts import parent_identity
    parent = parent_identity()
    process = None
    process_identity = None
    stop = threading.Event()
    readers = []
    heartbeat = None
    input_writer = None
    code = None
    launch_error = None
    interruption = None
    cancel_reason = None
    cancel_error = None
    try:
        from .hosts import child_options
        try:
            process = subprocess.Popen(
                argv, cwd=cwd,
                stdin=(subprocess.DEVNULL if input_payload is None
                       else subprocess.PIPE),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                env=env, **child_options())
        except OSError as exc:
            launch_error = type(exc).__name__
            stderr._write(redact(str(exc)))
            raise _LaunchFailed(str(exc)) from exc
        process_identity = _identity(process.pid)
        readers = [
            threading.Thread(target=_drain, args=(process.stdout, stdout), daemon=True),
            threading.Thread(target=_drain, args=(process.stderr, stderr), daemon=True),
        ]
        for reader in readers:
            reader.start()
        if input_state is not None:
            input_writer = threading.Thread(
                target=_write_input,
                args=(process.stdin, input_payload, input_state),
                daemon=True,
            )
            input_writer.start()
        try:
            if on_started is not None:
                on_started(process)
        except Exception as exc:
            launch_error = type(exc).__name__
            stderr._write(redact(f'startup hook failed: {exc}'))
            from .hosts import terminate_owned
            terminate_owned(process)
            code = process.returncode
        else:
            document = _heartbeat_document(evidence_id, folder, metadata, process_identity, parent,
                                           started, 'running', stdout, stderr)
            _write_progress(folder, document)

            def beat():
                while not stop.wait(HEARTBEAT_SECONDS):
                    if process.poll() is not None:
                        break
                    _write_progress(folder, _heartbeat_document(
                        evidence_id, folder, metadata, process_identity, parent, started,
                        'running', stdout, stderr))

            heartbeat = threading.Thread(target=beat, daemon=True)
            heartbeat.start()
            wait_deadline = time.monotonic() + timeout
            while True:
                code = process.poll()
                if code is not None:
                    break
                if cancel_check is not None:
                    try:
                        requested = cancel_check()
                    except Exception as exc:
                        interruption = 'cancel-check-failed'
                        launch_error = type(exc).__name__
                        cancel_error = str(exc)
                        from .hosts import terminate_owned
                        terminate_owned(process)
                        code = process.returncode
                        break
                    if requested:
                        interruption = 'cancelled'
                        launch_error = 'Cancelled'
                        cancel_reason = (
                            requested if isinstance(requested, str)
                            else 'CANCELLED'
                        )
                        from .hosts import terminate_owned
                        terminate_owned(process)
                        code = process.returncode
                        break
                remaining = wait_deadline - time.monotonic()
                if remaining <= 0:
                    interruption = 'timeout'
                    launch_error = 'TimeoutExpired'
                    from .hosts import terminate_owned
                    terminate_owned(process)
                    code = process.returncode
                    break
                try:
                    code = process.wait(timeout=min(.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
    except KeyboardInterrupt:
        interruption = 'keyboard-interrupt'
        launch_error = 'KeyboardInterrupt'
        if process is not None:
            from .hosts import terminate_owned
            terminate_owned(process)
            code = process.returncode
    except _LaunchFailed:
        pass
    except Exception as exc:
        launch_error = type(exc).__name__
        stderr._write(redact(f'execution failed: {exc}'))
        if process is not None:
            from .hosts import terminate_owned
            terminate_owned(process)
            code = process.returncode
    finally:
        stop.set()
        if heartbeat is not None:
            heartbeat.join(timeout=5)
        if process is not None:
            if input_writer is not None:
                input_writer.join(timeout=10)
            for pipe in (process.stdin,):
                try:
                    if pipe is not None:
                        pipe.close()
                except OSError:
                    pass
            for reader in readers:
                reader.join(timeout=10)
            for pipe in (process.stdout, process.stderr):
                try:
                    if pipe is not None:
                        pipe.close()
                except OSError:
                    pass
        integrity = {'stdout': stdout.close(), 'stderr': stderr.close()}

    input_incomplete = input_state is not None and not input_state['complete']
    terminal = ('timeout' if interruption == 'timeout' else
                'cancelled' if interruption == 'cancelled' else
                'interrupted' if interruption else
                'launch-failed' if launch_error and process is None else
                'failed' if (launch_error or code not in (None, 0)
                             or input_incomplete) else 'completed')
    from .hosts import is_alive
    result = {
        'schema': 1, 'evidence_id': evidence_id, 'task': metadata.get('task'),
        'status': terminal, 'returncode': code, 'launch_error': launch_error,
        'interruption': interruption, 'cancel_reason': cancel_reason,
        'cancel_error': cancel_error, 'started_at': started_at,
        'finished_at': _stamp(), 'duration_s': round(time.monotonic() - started, 3),
        'stdout_path': str(stdout.path), 'stderr_path': str(stderr.path),
        'log_integrity': integrity, 'process_identity': process_identity,
        'process_alive': bool(process_identity and is_alive(process_identity)),
        'parent': parent, 'folder': str(folder), 'input': input_state,
    }
    atomic(folder / 'command-result.json', result)
    _write_progress(folder, _heartbeat_document(
        evidence_id, folder, metadata, process_identity, parent, started, terminal,
        stdout, stderr))
    return result


class _LaunchFailed(Exception):
    pass


def _evidence_owned(state, evidence_id):
    inflight = state.get('inflight') or {}
    if inflight.get('id') == evidence_id:
        return True
    return any(str(path).replace('\\', '/').endswith('/' + evidence_id + '/receipt.json')
               for path in state.get('evidence') or [])


def progress_snapshot(run_dir, evidence_id, wait_seconds=0):
    """Read one evidence execution without taking the project writer lock."""
    run_dir = Path(run_dir)
    if not re.fullmatch(r'[a-f0-9]{32}', str(evidence_id)):
        return {'found': False, 'evidence_id': evidence_id,
                'error': 'EVIDENCE_ID_INVALID', 'terminal': False}
    state_file = run_dir / 'state.json'
    if not state_file.is_file():
        return {'found': False, 'evidence_id': evidence_id,
                'error': 'RUN_STATE_MISSING', 'terminal': False}
    state = read(state_file)
    if not _evidence_owned(state, evidence_id):
        return {'found': False, 'evidence_id': evidence_id,
                'error': 'EVIDENCE_NOT_OWNED', 'terminal': False}
    folder = run_dir / 'evidence' / evidence_id
    if not folder.is_dir():
        return {'found': False, 'evidence_id': evidence_id,
                'error': 'EVIDENCE_FOLDER_MISSING', 'terminal': False}
    try:
        wait = max(0.0, min(float(wait_seconds or 0), WAIT_LIMIT_SECONDS))
    except (TypeError, ValueError):
        wait = 0.0
    deadline = time.monotonic() + wait
    progress = result = receipt = None
    while True:
        progress_file = folder / 'command-progress.json'
        result_file = folder / 'command-result.json'
        receipt_file = folder / 'receipt.json'
        if progress_file.is_file():
            try:
                progress = read(progress_file)
            except WorkflowError:
                progress = None
        if result_file.is_file():
            try:
                result = read(result_file)
            except WorkflowError:
                result = None
        if receipt_file.is_file():
            try:
                receipt = read(receipt_file)
            except WorkflowError:
                receipt = None
        terminal = result is not None or receipt is not None
        if terminal or time.monotonic() >= deadline:
            break
        time.sleep(.2)
    status = (receipt.get('status') if receipt else
              result.get('status') if result else
              progress.get('status') if progress else 'unknown')
    parent_alive = None
    process_alive = None
    if progress:
        from .hosts import is_alive
        parent = progress.get('parent')
        process_identity = progress.get('process_identity')
        parent_alive = is_alive(parent) if parent else None
        process_alive = is_alive(process_identity) if process_identity else None
        if not terminal and parent_alive is False:
            status = 'parent-lost'
    stdout_path = (result or {}).get('stdout_path') or (progress or {}).get('stdout', {}).get('path')
    stderr_path = (result or {}).get('stderr_path') or (progress or {}).get('stderr', {}).get('path')
    integrity = (result or {}).get('log_integrity') or {}
    return {
        'found': True, 'evidence_id': evidence_id, 'terminal': terminal,
        'status': status, 'validation': 'NOT_CHECKED',
        'not_authoritative': True,
        'phase': (progress or {}).get('phase'),
        'heartbeat_at': (progress or {}).get('heartbeat_at'),
        'elapsed_s': (progress or {}).get('elapsed_s'),
        'stdout_path': stdout_path, 'stderr_path': stderr_path,
        'stdout_bytes': (integrity.get('stdout') or {}).get('bytes'),
        'stderr_bytes': (integrity.get('stderr') or {}).get('bytes'),
        'log_truncated': bool((integrity.get('stdout') or {}).get('truncated') or
                              (integrity.get('stderr') or {}).get('truncated')),
        'process_identity': (progress or {}).get('process_identity'),
        'process_alive': process_alive, 'parent': (progress or {}).get('parent'),
        'parent_alive': parent_alive,
        'build_context': (progress or {}).get('build_context'),
        'receipt_path': str(folder / 'receipt.json') if receipt else None,
        'next_action': (None if terminal else
                        'run-status again or inspect the logs; use recover-run after investigation'),
    }
