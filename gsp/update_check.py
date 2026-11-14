"""Best-effort daily stable-tag checks for user-level GSP installations."""
import json
import re
import subprocess
import time
from pathlib import Path

TAG_RE = re.compile(r"refs/tags/(v\d+\.\d+\.\d+)$")


def _version(tag):
    return tuple(int(part) for part in tag[1:].split('.'))


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
        return dict(cached, cached=True)
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    result['cached'] = False
    return result
