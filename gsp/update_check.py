"""Best-effort daily stable-tag checks for user-level GSP installations."""
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

TAG_RE = re.compile(r"refs/tags/(v\d+\.\d+\.\d+)$")
DEFAULT_SOURCE = 'https://github.com/jqz12/gstacksp.git'


def _version(tag):
    return tuple(int(part) for part in tag[1:].split('.'))


def _version_or_none(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith('v'):
        value = value[1:]
    if not re.fullmatch(r'\d+\.\d+\.\d+', value):
        return None
    return tuple(int(part) for part in value.split('.'))


def _decorate(result):
    """Add a comparison result while keeping the existing result fields."""
    if result.get('status') != 'ok':
        result.setdefault('available', None)
        result.setdefault('comparison', 'unknown')
        return result
    current = _version_or_none(result.get('current'))
    latest = _version_or_none(result.get('latest'))
    if current is None or latest is None:
        result['available'] = None
        result['comparison'] = 'unknown'
    elif latest > current:
        result['available'] = True
        result['comparison'] = 'newer'
    elif latest == current:
        result['available'] = False
        result['comparison'] = 'same'
    else:
        result['available'] = False
        result['comparison'] = 'older'
    return result


def source_for_manifest(manifest, env=None, runner=subprocess.run):
    """Resolve the public GitHub source used for update checks."""
    env = os.environ if env is None else env
    override = env.get('GSP_UPDATE_SOURCE')
    if isinstance(override, str) and override.startswith('https://github.com/'):
        return override
    recorded = manifest.get('source') if isinstance(manifest, dict) else None
    if isinstance(recorded, str) and recorded.startswith('https://github.com/'):
        return recorded
    if isinstance(recorded, str):
        checkout = Path(recorded)
        if (checkout / '.git').exists():
            try:
                result = runner(['git', '-C', str(checkout), 'remote', 'get-url', 'origin'],
                                capture_output=True, text=True, timeout=5)
                remote = result.stdout.strip() if result.returncode == 0 else ''
                if remote.startswith('https://github.com/'):
                    return remote
            except (OSError, subprocess.TimeoutExpired):
                pass
    return DEFAULT_SOURCE


def _write_cache(path, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def stable_tags(source, timeout=30, runner=subprocess.run):
    result = runner(
        ['git', 'ls-remote', '--tags', source, 'refs/tags/v*'],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or 'stable tag check failed')
    found = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        match = TAG_RE.search(fields[1])
        if match:
            found[match.group(1)] = fields[0]
    return found


def check(cache_path, source, current_version=None, now=None, force=False, runner=subprocess.run, interval=86400):
    """Return a cached or fresh stable-tag result without raising network errors."""
    path = Path(cache_path)
    now = time.time() if now is None else now
    cached = {}
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            cached = {}
    if not force and cached.get('source') == source and now - float(cached.get('checked_at', 0)) < interval:
        return _decorate(dict(cached, cached=True))
    try:
        tags = stable_tags(source, runner=runner)
        latest = max(tags, key=_version) if tags else None
        result = {'schema': 1, 'source': source, 'checked_at': now, 'status': 'ok',
                  'current': current_version, 'latest': latest,
                  'commit': tags.get(latest) if latest else None}
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        result = {'schema': 1, 'source': source, 'checked_at': now,
                  'status': 'NOT_CHECKED', 'current': current_version,
                  'latest': cached.get('latest'), 'commit': cached.get('commit'),
                  'error': type(exc).__name__}
    result = _decorate(result)
    _write_cache(path, result)
    result['cached'] = False
    return result
