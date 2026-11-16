"""Local Git topology discovery and recommendation.

This module is deliberately read-only.  Creating a branch/worktree and binding
it to a task is handled by the runtime layer after an explicit user choice.
"""

from __future__ import annotations

import os
import re
import json
import hashlib
import subprocess
from pathlib import Path
from typing import Iterable

from .runtime import WorkflowError, parallel_required, topology_selected


_KINDS = {'feature', 'fix', 'docs', 'test', 'refactor', 'chore', 'research'}
_INTENT_WORKTREE = {'parallel-multi-task', 'cross-session', 'full-stack'}
_WORKFLOW_PREFIXES = ('.gsp/', 'docs/gsp/', 'docs/work/', 'docs/superpowers/', '.superpowers/')
_WORKFLOW_FILES = {'.gitignore', 'TODOS.md', 'TODO.md'}


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['git', *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )


def _output(root: Path, *args: str) -> str | None:
    result = _run(root, *args)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _changed_paths(raw: str) -> list[str]:
    paths: set[str] = set()
    records = iter(raw.split('\0'))
    for record in records:
        if not record:
            continue
        paths.add(record[3:])
        # In -z porcelain a rename lists destination then source. Both are
        # changed inputs; neither quoting nor an arrow delimiter is involved.
        if 'R' in record[:2] or 'C' in record[:2]:
            source = next(records, '')
            if source:
                paths.add(source)
    return sorted(paths)


def _is_workflow_path(path: str) -> bool:
    normalized = path.replace('\\', '/')
    while normalized.startswith('./'):
        normalized = normalized[2:]
    return normalized in _WORKFLOW_FILES or normalized.startswith(_WORKFLOW_PREFIXES)


def _parse_worktrees(raw: str) -> list[dict]:
    records: list[dict] = []
    current: dict[str, object] = {}
    for line in raw.split('\0') + ['']:
        if not line:
            if current:
                path = str(current.get('path', ''))
                records.append({
                    'path': str(Path(path).resolve()) if path else None,
                    'head': current.get('head'),
                    'branch': current.get('branch'),
                    'detached': bool(current.get('detached')),
                    'exists': Path(path).is_dir() if path else False,
                    'prunable': current.get('prunable'),
                    'locked': current.get('locked'),
                })
                current = {}
            continue
        key, _, value = line.partition(' ')
        if key == 'worktree':
            current['path'] = value
        elif key == 'HEAD':
            current['head'] = value
        elif key == 'branch':
            current['branch'] = value.removeprefix('refs/heads/')
        elif key == 'detached':
            current['detached'] = True
        elif key in {'prunable', 'locked'}:
            current[key] = value or True
    return records


def _active_summaries(root: Path, supplied: Iterable[dict] | None) -> list[dict]:
    if supplied is not None:
        rows = list(supplied)
    else:
        rows = []
        runs = root / '.gsp' / 'runs'
        if runs.is_dir():
            for state_file in sorted(runs.glob('*/state.json')):
                try:
                    state = json.loads(state_file.read_text(encoding='utf-8'))
                except (OSError, ValueError, TypeError):
                    raise WorkflowError('GIT_BINDINGS_UNREADABLE ' + str(state_file))
                rows.append(state)
    result: list[dict] = []
    for state in rows:
        if state.get('status') in {'closed', 'delivered', 'cancelled', 'ship'} or state.get('close_reason'):
            continue
        topology = state.get('git_topology') or {}
        git = state.get('git') or {}
        control_only = parallel_required(state) and topology.get('effective_mode') == 'in-place'
        result.append({
            'id': state.get('id'),
            'branch': topology.get('branch') or git.get('branch'),
            'workspace_root': topology.get('workspace_root') or topology.get('worktree_path') or git.get('root'),
            'writer': topology.get('writer') or topology.get('agent') or state.get('id'),
            'status': state.get('status'),
            'claiming': topology_selected(state) and bool(topology.get('branch') and topology.get('workspace_root')) and not control_only,
        })
        for task in state.get('tasks') or []:
            binding = task.get('topology') or {}
            if not binding.get('workspace_root'):
                continue
            result.append({
                'id': f'{state.get("id")}:{task.get("id")}', 'parent': state.get('id'),
                'branch': binding.get('branch'), 'workspace_root': binding.get('workspace_root'),
                'writer': task.get('agent') or task.get('session') or task.get('id'),
                'status': task.get('status'), 'claiming': task.get('role') != 'observer',
            })
    return result


def bound_task_for_path(root: Path, path: Path) -> str | None:
    """Find the most specific live source workspace without changing active.json."""
    path = Path(path).resolve()
    candidates = []
    for row in _active_summaries(Path(root), None):
        if not row.get('claiming') or not row.get('workspace_root'):
            continue
        workspace = Path(row['workspace_root']).resolve()
        if path.is_relative_to(workspace):
            candidates.append((len(workspace.parts), row.get('parent') or row['id']))
    if not candidates:
        return None
    depth = max(d for d, _ in candidates)
    owners = {owner for d, owner in candidates if d == depth}
    if len(owners) != 1:
        raise WorkflowError('GIT_WORKTREE_CONFLICT multiple tasks claim the current workspace')
    return owners.pop()


def probe(root: Path, active_tasks: Iterable[dict] | None = None) -> dict:
    root = Path(root).resolve()
    try:
        return _probe(root, active_tasks)
    except (OSError, subprocess.TimeoutExpired, WorkflowError) as exc:
        code = 'GIT_UNAVAILABLE' if isinstance(exc, FileNotFoundError) else 'GIT_PROBE_FAILED'
        return {'schema': 1, 'status': 'blocked', 'current': {'root': str(root)},
                'branches': [], 'worktrees': [], 'active_tasks': [],
                'risks': [{'code': code, 'severity': 'blocking', 'detail': str(exc)}]}


def _probe(root: Path, active_tasks: Iterable[dict] | None = None) -> dict:
    """Return a read-only, machine-readable Git topology inventory."""
    root = Path(root).resolve()
    identity = _run(root, 'rev-parse', '--show-toplevel', '--git-common-dir')
    identity_lines = identity.stdout.splitlines() if identity.returncode == 0 else []
    top = identity_lines[0].strip() if identity_lines else None
    if not top:
        git_marker = any((p / '.git').exists() for p in (root, *root.parents))
        if git_marker or 'not a git repository' not in identity.stderr.lower():
            raise WorkflowError('GIT_PROBE_FAILED ' + identity.stderr.strip())
        return {
            'schema': 1,
            'status': 'not-a-repository',
            'current': {'root': str(root), 'branch': None, 'head': None, 'changed_paths': [],
                        'workflow_changed_paths': [], 'source_changed_paths': []},
            'worktrees': [],
            'active_tasks': _active_summaries(root, active_tasks),
            'risks': [{'code': 'GIT_NOT_REPOSITORY', 'severity': 'blocking', 'detail': 'Git repository is not available'}],
        }

    branch_result = _run(root, 'branch', '--show-current')
    if branch_result.returncode:
        raise WorkflowError('GIT_PROBE_FAILED ' + branch_result.stderr.strip())
    branch = branch_result.stdout.strip() or None
    head = _output(root, 'rev-parse', '--verify', 'HEAD')
    status = _run(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all')
    if status.returncode != 0:
        return {
            'schema': 1,
            'status': 'unknown',
            'current': {'root': str(Path(top).resolve()), 'branch': branch, 'head': head, 'changed_paths': [],
                        'workflow_changed_paths': [], 'source_changed_paths': []},
            'worktrees': [],
            'active_tasks': _active_summaries(root, active_tasks),
            'risks': [{'code': 'GIT_PROBE_FAILED', 'severity': 'blocking', 'detail': status.stderr.strip() or 'git status failed'}],
        }

    changed = _changed_paths(status.stdout)
    workflow_changed = sorted(path for path in changed if _is_workflow_path(path))
    source_changed = sorted(path for path in changed if path not in workflow_changed)
    worktree_raw = _run(root, 'worktree', 'list', '--porcelain', '-z')
    if worktree_raw.returncode:
        raise WorkflowError('GIT_PROBE_FAILED ' + worktree_raw.stderr.strip())
    worktrees = _parse_worktrees(worktree_raw.stdout)
    if not worktrees:
        raise WorkflowError('GIT_PROBE_FAILED empty worktree inventory')
    refs = _run(root, 'for-each-ref', '--format=%(refname:short)%09%(objectname)', 'refs/heads/')
    if refs.returncode:
        raise WorkflowError('GIT_PROBE_FAILED ' + refs.stderr.strip())
    branches = [{'name': line.split('\t')[0], 'head': line.split('\t')[1]}
                for line in refs.stdout.splitlines() if '\t' in line]
    active = _active_summaries(root, active_tasks)
    risks: list[dict] = []
    if not head:
        risks.append({'code': 'GIT_HEAD_UNBORN', 'severity': 'blocking', 'detail': 'create an initial commit before branch/worktree isolation'})
    if branch is None:
        risks.append({'code': 'GIT_DETACHED_HEAD', 'severity': 'blocking', 'detail': 'HEAD is detached'})
    if source_changed:
        risks.append({'code': 'GIT_DIRTY', 'severity': 'warning', 'detail': f'{len(source_changed)} source paths require attribution'})
    known_paths = {row['path'] for row in worktrees if row.get('path')}
    claimed_paths: dict[str, str] = {}
    claimed_branches: dict[str, str] = {}
    for task in active:
        if not task.get('claiming'):
            continue
        workspace = task.get('workspace_root')
        workspace = os.path.normcase(str(Path(workspace).resolve())) if workspace else None
        task_branch = task.get('branch')
        if workspace and not Path(workspace).is_dir():
            risks.append({'code': 'GIT_WORKTREE_MISSING', 'severity': 'blocking', 'detail': f'{task.get("id")} workspace is missing'})
        if workspace and workspace in claimed_paths:
            risks.append({'code': 'GIT_WORKTREE_CONFLICT', 'severity': 'blocking', 'detail': f'{workspace} claimed by {claimed_paths[workspace]} and {task.get("id")}'})
        elif workspace:
            claimed_paths[str(workspace)] = str(task.get('id'))
        if task_branch and task_branch in claimed_branches:
            risks.append({'code': 'GIT_BRANCH_CONFLICT', 'severity': 'blocking', 'detail': f'{task_branch} claimed by {claimed_branches[task_branch]} and {task.get("id")}'})
        elif task_branch:
            claimed_branches[str(task_branch)] = str(task.get('id'))
        if workspace and workspace not in {os.path.normcase(path) for path in known_paths}:
            risks.append({'code': 'GIT_WORKTREE_UNREGISTERED', 'severity': 'warning', 'detail': f'{workspace} is not registered by git worktree'})

    status_name = 'clean' if not risks else 'needs-decision'
    if any(r['severity'] == 'blocking' for r in risks):
        status_name = 'blocked'
    return {
        'schema': 1,
        'status': status_name,
        'current': {
            'root': str(Path(top).resolve()),
            'branch': branch,
            'head': head,
            'changed_paths': changed,
            'workflow_changed_paths': workflow_changed,
            'source_changed_paths': source_changed,
        },
        'worktrees': worktrees,
        'branches': branches,
        'common_dir': str((root / identity_lines[1]).resolve()),
        'active_tasks': active,
        'risks': risks,
    }


def recommend(inventory: dict, context: dict) -> dict:
    """Recommend a mode without mutating Git or pretending to have user consent."""
    risks = list(inventory.get('risks') or [])
    if inventory.get('status') == 'not-a-repository':
        return {'mode': None, 'reasons': ['当前目录不是 Git 仓库'], 'blocking': risks}
    intent = str(context.get('intent') or 'single-task')
    changed = inventory.get('current', {}).get('source_changed_paths')
    if changed is None:
        changed = inventory.get('current', {}).get('changed_paths') or []
    branch = inventory.get('current', {}).get('branch')
    reasons: list[str] = []
    if changed:
        mode = 'worktree'
        reasons.append('当前工作区有脏改动，保留现场并使用干净 worktree')
    elif intent in _INTENT_WORKTREE or context.get('size') == 'large':
        mode = 'worktree'
        reasons.append('任务规模较大，或包含跨会话、并行、全栈协作，需要独立 worktree')
    elif branch in (None, 'main', 'master'):
        mode = 'branch'
        reasons.append('当前位于主分支或未命名 HEAD，先建立带 GSP 编号的任务分支')
    else:
        mode = 'branch'
        reasons.append('当前工作区干净，使用独立任务分支即可保持低负担隔离')
    blocking = [risk for risk in risks if risk.get('severity') == 'blocking']
    if blocking:
        reasons.append('存在阻塞风险，选择前必须先处理或明确覆盖')
    return {'mode': mode, 'reasons': reasons, 'blocking': blocking}


def _topic_slug(title: str) -> str:
    topic = re.sub(r'\s+', '-', str(title or '').strip())
    topic = re.sub(r'[\\/\x00-\x1f\x7f]+', '-', topic)
    topic = topic.strip('.-')
    return topic or '任务'


def branch_name(number: str, task_id: str | None, title: str, kind: str = 'feature', ascii_fallback: bool = False) -> str:
    if kind not in _KINDS:
        raise WorkflowError('GIT_BRANCH_KIND_INVALID use a supported branch prefix')
    if not re.fullmatch(r'GSP-\d{6}', str(number or '')):
        raise WorkflowError('GIT_TASK_NUMBER_INVALID expected GSP-000000')
    if task_id and not re.fullmatch(r'T\d+', task_id):
        raise WorkflowError('GIT_PLAN_TASK_INVALID expected T01 format')
    suffix = f'-T{int(task_id[1:]):02d}' if task_id else ''
    topic = _topic_slug(title)
    topic = re.sub(r'[~^:?*\[\]@{}]+', '-', topic).replace('..', '-')
    if ascii_fallback:
        topic = 'task-' + hashlib.sha256(title.encode('utf-8')).hexdigest()[:10]
    topic = topic.encode('utf-8')[:80].decode('utf-8', errors='ignore').strip('.-') or 'task'
    if topic.endswith('.lock'):
        topic += '-task'
    value = f'{kind}/{number}{suffix}-{topic}'
    validate_branch_name(value)
    return value


def validate_branch_name(value: str) -> None:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise WorkflowError('GIT_BRANCH_NAME_INVALID control characters are not allowed')
    if any(char in value for char in (' ', '\\', '@{', '..', '~', '^', ':', '?', '*', '[', '\x7f')) or '//' in value or value.endswith(('.', '/')):
        raise WorkflowError('GIT_BRANCH_NAME_INVALID spaces, backslashes, duplicate slashes or trailing punctuation are not allowed')
    if value.startswith('-') or value.endswith('.lock') or value == '@':
        raise WorkflowError('GIT_BRANCH_NAME_INVALID ref name is not safe')
    prefix, separator, rest = value.partition('/')
    if not separator or prefix not in _KINDS or not re.fullmatch(r'GSP-\d{6}-(?:T\d{2,}-)?[^/]+', rest):
        raise WorkflowError('GIT_BRANCH_NAME_INVALID expected <kind>/GSP-<number>-<topic>')
