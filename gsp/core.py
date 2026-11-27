"""Neutral errors, atomic files and digests shared by domain services and CLI."""
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

from .validation_context import current_context, invalidate


class WorkflowError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(path):
    path = Path(path)
    context = current_context()
    loader = lambda: sha(path.read_bytes())
    return loader() if context is None else context.get('digest', str(path.resolve()), loader)


def retry_access(operation):
    for attempt in range(4):
        try:
            return operation()
        except OSError as exc:
            if exc.errno != errno.EACCES or attempt == 3:
                raise
            time.sleep(.05)


def read(path):
    try:
        return json.loads(retry_access(lambda: Path(path).read_text(encoding='utf-8-sig')))
    except (OSError, ValueError) as exc:
        raise WorkflowError(f'INVALID_STATE {path}: {exc}') from exc


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        retry_access(lambda: os.replace(temporary, path))
        invalidate('write:' + str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def inside(root, path):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def redact(text):
    text = re.sub(r'(?i)(authorization\s*[:=]\s*bearer\s+)\S+', r'\1<REDACTED>', text)
    text = re.sub(r'''(?i)((?:api[_-]?key|password|secret|token)\s*[=:]\s*["']?)[^\s"']+''', r'\1<REDACTED>', text)
    text = re.sub(r'\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{15,})\b', '<REDACTED>', text)
    return text


def cached_value(namespace, key, loader):
    context = current_context()
    if context is None:
        return loader()
    return context.get(namespace, key, loader)
