"""Build and temporary-artifact policy shared by run, plan and health.

The module only inspects and reclaims entries that a single execution created.
It never deletes pre-existing files, never follows links and never crosses the
project root.
"""
from pathlib import Path
import json
import os
import re
import stat
import subprocess

from .runtime import WorkflowError, inside

DEFAULT_POLICY = {
    'version': 1,
    'temp_isolation': True,
    'temp_roots': [],
    'retain_on_failure': {'max_entries': 1, 'max_bytes': 50 * 1024 * 1024},
    'worktree_build': 'managed',
    'build_cache': {'mode': 'isolated', 'root': None, 'context': {}},
    'limits': {'max_build_bytes': 0, 'max_temp_entries': 0},
    'profile_expectations': {'debug': None, 'incremental_for_tests': None},
}
MAX_ENUMERATED_ENTRIES = 20_000
MAX_TREE_ENTRIES = 50_000


def _policy_path(root):
    return Path(root) / '.gsp' / 'build-policy.json'


def _defaults(root):
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    policy['temp_roots'] = ['target/tmp'] if (Path(root) / 'Cargo.toml').is_file() else []
    return policy


def _merge(base, value):
    merged = dict(base)
    for key, item in value.items():
        if isinstance(item, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **item}
        else:
            merged[key] = item
    return merged


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _invalid(detail):
    raise WorkflowError('BUILD_POLICY_INVALID ' + detail)


def _validate(policy):
    if policy.get('version') != 1:
        _invalid('version must be 1')
    if not isinstance(policy.get('temp_isolation'), bool):
        _invalid('temp_isolation must be true or false')
    roots = policy.get('temp_roots')
    if not isinstance(roots, list):
        _invalid('temp_roots must be a list of project-relative paths')
    normalized = []
    for raw in roots:
        if not isinstance(raw, str) or not raw.strip():
            _invalid('temp_roots entries must be nonempty strings')
        value = raw.replace('\\', '/').strip()
        path = Path(value)
        if path.is_absolute() or value.startswith('/') or re.match(r'^[A-Za-z]:', value):
            _invalid('temp root must be relative: ' + raw)
        if '..' in path.parts:
            _invalid('temp root must not escape with ..: ' + raw)
        normalized.append(path.as_posix())
    policy['temp_roots'] = normalized
    retain = policy.get('retain_on_failure')
    if not isinstance(retain, dict):
        _invalid('retain_on_failure must be an object')
    if not _integer(retain.get('max_entries')):
        _invalid('retain_on_failure.max_entries must be an integer >= 0')
    if not _integer(retain.get('max_bytes')):
        _invalid('retain_on_failure.max_bytes must be an integer >= 0')
    if policy.get('worktree_build') not in {'managed', 'off'}:
        _invalid('worktree_build must be "managed" or "off"')
    cache = policy.get('build_cache')
    if not isinstance(cache, dict):
        _invalid('build_cache must be an object')
    if cache.get('mode') not in {'isolated', 'reuse-exclusive', 'off'}:
        _invalid('build_cache.mode must be isolated, reuse-exclusive or off')
    if cache.get('root') is not None and not isinstance(cache.get('root'), str):
        _invalid('build_cache.root must be an absolute path string or null')
    if not isinstance(cache.get('context'), dict):
        _invalid('build_cache.context must be an object')
    limits = policy.get('limits')
    if not isinstance(limits, dict):
        _invalid('limits must be an object')
    for key in ('max_build_bytes', 'max_temp_entries'):
        if not _integer(limits.get(key)):
            _invalid('limits.' + key + ' must be an integer >= 0')
    expectations = policy.get('profile_expectations')
    if not isinstance(expectations, dict):
        _invalid('profile_expectations must be an object')
    for key in ('debug', 'incremental_for_tests'):
        value = expectations.get(key)
        if value is not None and not isinstance(value, bool):
            _invalid('profile_expectations.' + key + ' must be true, false or null')


def load_policy(root):
    """Return validated policy merged over built-in defaults."""
    root = Path(root)
    policy = _defaults(root)
    file = _policy_path(root)
    if file.exists():
        try:
            data = json.loads(file.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            _invalid('cannot read ' + str(file) + ': ' + str(exc))
        if not isinstance(data, dict):
            _invalid('top level must be an object')
        policy = _merge(policy, data)
    _validate(policy)
    return policy


def temp_env(run_dir):
    """Point every temporary-directory variable at one isolated run folder."""
    value = str(Path(run_dir))
    return {'TMP': value, 'TEMP': value, 'TMPDIR': value}


def isolation_env(run_dir):
    """Keep nested fixtures from inheriting the control repository's Git."""
    env = temp_env(run_dir)
    value = str(Path(run_dir))
    env['GIT_CEILING_DIRECTORIES'] = value
    env['GSP_CEILING_DIRECTORIES'] = value
    return env


def snapshot_names(root, rel):
    """Record first-level entry names and mtimes for a project-relative root."""
    target = Path(root) / rel
    result = {}
    try:
        with os.scandir(target) as entries:
            for entry in entries:
                try:
                    result[entry.name] = entry.stat(follow_symlinks=False).st_mtime
                except OSError:
                    continue
    except (FileNotFoundError, NotADirectoryError):
        return {}
    except OSError:
        return {}
    return result


def _is_link(path):
    path = Path(path)
    isjunction = getattr(os.path, 'isjunction', None)
    return path.is_symlink() or bool(isjunction and isjunction(path))


def _tracked_paths(root, rel):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('GIT_CONFIG_')}
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--', rel],
                            capture_output=True, text=True, encoding='utf-8',
                            errors='replace', env=env)
    if result.returncode:
        return set()
    return {item for item in result.stdout.split('\0') if item}


def _tree_size(path, limit=MAX_TREE_ENTRIES):
    total = 0
    seen = 0
    stack = [Path(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    seen += 1
                    if seen > limit:
                        return None
                    if _is_link(entry.path):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    else:
                        total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            return None
    return total


def tree_size(path):
    path = Path(path)
    if not path.exists() or _is_link(path):
        return 0
    if path.is_file():
        return path.stat().st_size
    return _tree_size(path)


def _remove(path, root):
    path = Path(path)
    if _is_link(path):
        path.unlink()
        return
    if not inside(root, path):
        raise OSError('entry resolves outside project root: ' + str(path))
    if path.is_dir():
        with os.scandir(path) as entries:
            children = [Path(entry.path) for entry in entries]
        for child in children:
            _remove(child, root)
        try:
            path.rmdir()
        except PermissionError:
            os.chmod(path, stat.S_IWRITE)
            path.rmdir()
    else:
        try:
            path.unlink()
        except PermissionError:
            os.chmod(path, stat.S_IWRITE)
            path.unlink()


def remove_tree(path, root):
    """Delete one owned tree without following links or leaving the root."""
    _remove(path, root)


def reclaim_new_entries(root, rel, before, retain=None, success=True,
                        max_entries=MAX_ENUMERATED_ENTRIES):
    """Delete entries created after ``before``; retain bounded evidence on failure."""
    root = Path(root)
    target = root / rel
    result = {'created': 0, 'removed': 0, 'retained': 0, 'retained_bytes': 0,
              'truncated': False, 'errors': []}
    try:
        entries = []
        with os.scandir(target) as scanned:
            for entry in scanned:
                entries.append(entry)
                if len(entries) > max_entries:
                    break
    except (FileNotFoundError, NotADirectoryError):
        return result
    except OSError as exc:
        result['truncated'] = True
        result['errors'].append({'name': rel, 'code': 'ENUMERATION_FAILED',
                                 'detail': str(exc)})
        return result
    if len(entries) > max_entries:
        result['truncated'] = True
        return result
    new = [entry for entry in entries if entry.name not in before]
    result['created'] = len(new)
    tracked = _tracked_paths(root, rel)
    keep = []
    if not success and retain:
        limit_entries = int(retain.get('max_entries', 1) or 0)
        limit_bytes = int(retain.get('max_bytes', 0) or 0)
        ordered = sorted(new, key=lambda entry: entry.stat(follow_symlinks=False).st_mtime,
                         reverse=True)
        used = 0
        for entry in ordered:
            if len(keep) >= limit_entries:
                break
            size = _tree_size(entry.path) if entry.is_dir(follow_symlinks=False) else entry.stat(follow_symlinks=False).st_size
            if limit_bytes and keep and used + (size or 0) > limit_bytes:
                break
            keep.append(entry)
            used += size or 0
    keep_names = {entry.name for entry in keep}
    for entry in new:
        path = Path(entry.path)
        if entry.name in keep_names:
            size = tree_size(path) if entry.is_dir(follow_symlinks=False) else path.stat().st_size
            result['retained'] += 1
            result['retained_bytes'] += size or 0
            continue
        if _is_link(path):
            result['errors'].append({'name': entry.name, 'code': 'SYMLINK',
                                     'detail': 'links are never followed or deleted'})
            result['retained'] += 1
            continue
        relative = Path(rel) / entry.name
        if relative.as_posix() in tracked:
            result['errors'].append({'name': entry.name, 'code': 'TRACKED',
                                     'detail': 'Git-tracked entries are never deleted'})
            result['retained'] += 1
            continue
        if not inside(root, path):
            result['errors'].append({'name': entry.name, 'code': 'OUTSIDE',
                                     'detail': 'entry resolves outside project root'})
            result['retained'] += 1
            continue
        try:
            _remove(path, root)
            result['removed'] += 1
        except OSError as exc:
            result['errors'].append({'name': entry.name, 'code': 'REMOVE_FAILED',
                                     'detail': str(exc)})
            result['retained'] += 1
    return result


def _profile_values(root):
    values = {}
    for relative in ('Cargo.toml', '.cargo/config.toml'):
        path = Path(root) / relative
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except OSError:
            continue
        match = re.search(r'(?m)^\s*debug\s*=\s*(true|false)\s*(?:#.*)?$', text)
        if match and 'debug' not in values:
            values['debug'] = match.group(1) == 'true'
    return values


def build_policy_report(root):
    """Read-only health view; never edits the policy or Cargo configuration."""
    root = Path(root)
    file = _policy_path(root)
    try:
        policy = load_policy(root)
    except WorkflowError as exc:
        return {'status': 'invalid',
                'findings': [{'code': 'BUILD_POLICY_INVALID', 'severity': 'blocking',
                              'detail': str(exc)}]}
    findings = []
    status = 'ok'
    if (root / 'Cargo.toml').is_file() and not file.exists():
        status = 'missing'
        findings.append({'code': 'BUILD_POLICY_MISSING', 'severity': 'advisory',
                         'detail': 'Cargo project uses built-in defaults; add .gsp/build-policy.json to declare limits'})
    observed = _profile_values(root)
    for key, expected in (policy.get('profile_expectations') or {}).items():
        if expected is None:
            continue
        actual = observed.get('debug' if key == 'debug' else 'debug')
        if actual is None:
            findings.append({'code': 'PROFILE_EXPECTATION_UNKNOWN', 'severity': 'advisory',
                             'detail': key + ' expected ' + str(expected).lower() + ' but no profile value was found'})
        elif actual != expected:
            findings.append({'code': 'PROFILE_EXPECTATION_MISMATCH', 'severity': 'advisory',
                             'detail': key + ' expected ' + str(expected).lower() + ' but observed ' + str(actual).lower()})
    return {'status': status, 'findings': findings}
