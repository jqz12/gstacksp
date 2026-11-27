"""Local Git topology discovery and recommendation.

This module is deliberately read-only.  Creating a branch/worktree and binding
it to a task is handled by the runtime layer after an explicit user choice.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
import re
import json
import hashlib
import subprocess
from pathlib import Path
from typing import Iterable

from .core import (WorkflowError, cached_value)
from .git_operations import (head_branch, visible_git_marker)
from .workflow_support import (parallel_required, topology_selected)
from .orchestration_types import RequestRef, WorktreeRequest, digest_value


_KINDS = {'codex','feature', 'fix', 'docs', 'test', 'refactor', 'chore', 'research'}
_INTENT_WORKTREE = {'parallel-multi-task', 'cross-session', 'full-stack'}
_WORKFLOW_PREFIXES = ('.gsp/', 'docs/gsp/', 'docs/work/', 'docs/superpowers/', '.superpowers/')
_WORKFLOW_FILES = {'.gitignore', 'TODOS.md', 'TODO.md'}


@dataclass(frozen=True)
class WorkspaceExpectation:
    """Expected immutable facts for one existing native worktree."""

    project_uid: str
    work_uid: str | None
    task_uid: str | None
    owner: str
    owner_adapter: str
    base_sha: str
    head_sha: str
    branch: str
    request_uid: str
    request_digest: str
    receipt_digest: str
    control_root: str
    source_root: str
    common_dir: str
    schema: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "WorkspaceExpectation":
        if not isinstance(value, dict):
            raise WorkflowError('WORKSPACE_EXPECTATION_INVALID')
        expected = {
            'schema', 'project_uid', 'work_uid', 'task_uid', 'owner',
            'owner_adapter', 'base_sha', 'head_sha', 'branch',
            'request_uid', 'request_digest', 'receipt_digest',
            'control_root', 'source_root', 'common_dir',
        }
        if set(value) != expected:
            raise WorkflowError('WORKSPACE_EXPECTATION_INVALID fields')
        item = cls(**value)
        item.validate()
        return item

    def validate(self) -> None:
        if self.schema != 1:
            raise WorkflowError('WORKSPACE_EXPECTATION_INVALID schema')
        for name in (
            'project_uid', 'owner', 'owner_adapter',
            'base_sha', 'head_sha', 'branch', 'request_uid',
            'request_digest', 'receipt_digest', 'control_root',
            'source_root', 'common_dir',
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise WorkflowError('WORKSPACE_EXPECTATION_INVALID ' + name)
        if self.work_uid is not None and (
            not isinstance(self.work_uid, str) or not self.work_uid.strip()
        ):
            raise WorkflowError('WORKSPACE_EXPECTATION_INVALID work_uid')
        if self.task_uid is not None and (
            not isinstance(self.task_uid, str) or not self.task_uid.strip()
        ):
            raise WorkflowError('WORKSPACE_EXPECTATION_INVALID task_uid')
        for name in ('request_digest', 'receipt_digest'):
            if re.fullmatch(r'[a-f0-9]{64}', getattr(self, name)) is None:
                raise WorkflowError('WORKSPACE_EXPECTATION_INVALID ' + name)


@dataclass(frozen=True)
class WorkspaceProof:
    """Read-only Git and native attachment facts, never a permission grant."""

    project_uid: str
    work_uid: str | None
    task_uid: str | None
    owner: str
    owner_adapter: str
    control_root: str
    source_root: str
    source_top_level: str
    common_dir: str
    source_common_dir: str
    git_dir: str
    marker_path: str
    marker_digest: str
    backlink_path: str
    backlink_value: str
    registered: bool
    registration_head: str
    registration_branch: str
    base_sha: str
    head: str
    branch: str
    detached: bool
    clean: bool
    native_operation_id: str
    attachment_id: str
    request_uid: str
    request_digest: str
    receipt_digest: str
    verified_at: str
    schema: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


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


def _changed_paths(records) -> list[str]:
    paths: set[str] = set()
    records = iter(records)
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


def _split_branch_header(raw: str) -> tuple[str | None, list[str]]:
    """Separate the `--branch` header from porcelain path records."""
    records = raw.split('\0')
    if records and records[0].startswith('## '):
        text = records[0][3:]
        if text.startswith('No commits yet on '):
            branch = text[len('No commits yet on '):].split('...')[0].strip() or None
        elif text.startswith('HEAD (no branch)'):
            branch = None
        else:
            branch = text.split('...')[0].strip().split(' ')[0] or None
        return branch, records[1:]
    return None, records


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


def probe(root: Path, active_tasks: Iterable[dict] | None = None, include_branches: bool = True,
          include_status: bool = True, include_worktrees: bool = True) -> dict:
    root = Path(root).resolve()
    active = list(active_tasks) if active_tasks is not None else None
    key = (str(root),
           json.dumps(active, sort_keys=True, ensure_ascii=False, default=str) if active is not None else None,
           bool(include_branches), bool(include_status), bool(include_worktrees))

    def load():
        try:
            return _probe(root, active, include_branches, include_status, include_worktrees)
        except (OSError, subprocess.TimeoutExpired, WorkflowError) as exc:
            code = 'GIT_UNAVAILABLE' if isinstance(exc, FileNotFoundError) else 'GIT_PROBE_FAILED'
            return {'schema': 1, 'status': 'blocked', 'current': {'root': str(root)},
                    'branches': [], 'worktrees': [], 'active_tasks': [],
                    'risks': [{'code': code, 'severity': 'blocking', 'detail': str(exc)}]}

    return cached_value('git_probe', key, load)


def workspace_group_binding_projection(state: dict, task_id: str, group, authority: dict) -> dict:
    """Project group authority into topology facts without mutating either source."""
    if not hasattr(group, 'to_dict') or not isinstance(authority, dict):
        raise WorkflowError('WORKSPACE_GROUP_PROJECTION_INPUT_INVALID')
    group_value = group.to_dict()
    topology = state.get('git_topology') or {}
    return {
        'schema': 1,
        'work': state.get('id'),
        'task': task_id,
        'topology': {
            'control_root': group_value.get('control_root'),
            'workspace_root': group_value.get('workspace_root'),
            'common_dir': group_value.get('common_dir'),
            'project_uid': group_value.get('project_uid'),
            'branch': group_value.get('branch'),
            'base_sha': group_value.get('base_sha'),
            'mode': group_value.get('mode'),
            'source': 'workspace_groups.WorkspaceGroup from WorktreeAdapter',
            'state_binding_present': bool(topology),
        },
        'ownership': {
            'group_uid': group_value.get('group_uid'),
            'controller_binding_uid': group_value.get('controller_binding_uid'),
            'source_writer_session_id': authority.get('source_writer_session_id'),
            'source_writer_binding_uid': authority.get('source_writer_binding_uid'),
            'source_writer_status': authority.get('source_writer_status'),
            'source': 'sessions/index.json workspace_leases via SessionWorkspaceLeaseAuthority',
        },
        'cleanup_action': {
            'action': 'release-source-writer',
            'root_lifecycle': 'parent-workspace-reused',
            'remove_root': False,
            'source': 'workspace_groups.release_workspace_group; root/branch resource claim remains',
        },
        'verification': {
            'registered': bool(group_value.get('common_dir')),
            'project_uid': group_value.get('project_uid'),
            'resource_state': authority.get('resource_state'),
            'lease_revision': authority.get('lease_revision'),
            'source_writer_observed': bool(authority.get('source_writer_session_id')),
            'host_observation': authority.get('host_observation', 'unverified'),
            'native_host_receipt': 'NOT_CHECKED',
            'sources': {
                'git': 'git worktree registration/common-dir through WorktreeAdapter',
                'resources': 'ResourceRegistry resource request',
                'lease': 'sessions/index.json workspace_leases',
            },
        },
        'read_only': True,
    }


def _probe(root: Path, active_tasks: Iterable[dict] | None = None, include_branches: bool = True,
           include_status: bool = True, include_worktrees: bool = True) -> dict:
    """Return a read-only, machine-readable Git topology inventory."""
    root = Path(root).resolve()
    identity = _run(root, 'rev-parse', '--show-toplevel', '--git-common-dir', '--git-dir',
                    '--verify', 'HEAD')
    identity_lines = identity.stdout.splitlines()
    top = identity_lines[0].strip() if identity_lines else None
    if not top:
        git_marker = visible_git_marker(root)
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

    gitdir = identity_lines[2].strip() if len(identity_lines) >= 3 else None
    head = identity_lines[3].strip() if identity.returncode == 0 and len(identity_lines) >= 4 else None
    branch = head_branch(top, gitdir)
    status = (_run(root, 'status', '--porcelain=v1', '-z', '--branch', '--untracked-files=all')
              if include_status else None)
    status_records: list[str] = []
    if status is not None and status.returncode != 0:
        return {
            'schema': 1,
            'status': 'unknown',
            'current': {'root': str(Path(top).resolve()), 'branch': branch, 'head': head, 'changed_paths': [],
                        'workflow_changed_paths': [], 'source_changed_paths': []},
            'worktrees': [],
            'active_tasks': _active_summaries(root, active_tasks),
            'risks': [{'code': 'GIT_PROBE_FAILED', 'severity': 'blocking', 'detail': status.stderr.strip() or 'git status failed'}],
        }

    if status is not None:
        _, status_records = _split_branch_header(status.stdout)
    changed = _changed_paths(status_records)
    workflow_changed = sorted(path for path in changed if _is_workflow_path(path))
    source_changed = sorted(path for path in changed if path not in workflow_changed)
    if include_worktrees:
        worktree_raw = _run(root, 'worktree', 'list', '--porcelain', '-z')
        if worktree_raw.returncode:
            raise WorkflowError('GIT_PROBE_FAILED ' + worktree_raw.stderr.strip())
        worktrees = _parse_worktrees(worktree_raw.stdout)
        if not worktrees:
            raise WorkflowError('GIT_PROBE_FAILED empty worktree inventory')
    else:
        worktrees = []
    if include_branches:
        refs = _run(root, 'for-each-ref', '--format=%(refname:short)%09%(objectname)', 'refs/heads/')
        if refs.returncode:
            raise WorkflowError('GIT_PROBE_FAILED ' + refs.stderr.strip())
        branches = [{'name': line.split('\t')[0], 'head': line.split('\t')[1]}
                    for line in refs.stdout.splitlines() if '\t' in line]
    else:
        branches = []
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


def _same_path(left: Path | str, right: Path | str) -> bool:
    raw_left = str(left)
    raw_right = str(right)
    # For facts carried inside a raw host receipt, canonical case is enough:
    # resolving relative to this process cwd would turn a hostile relative path
    # into a real local path and weaken the alias/escape check.
    if not Path(raw_left).is_absolute() or not Path(raw_right).is_absolute():
        return os.path.normcase(raw_left.replace('/', '\\')).rstrip('\\') == (
            os.path.normcase(raw_right.replace('/', '\\')).rstrip('\\')
        )
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(
        str(Path(right).resolve())
    )


def _strict_expected_path(value: str, code: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise WorkflowError(code)
    lexical = os.path.normcase(os.path.abspath(str(path)))
    canonical = os.path.normcase(str(path.resolve()))
    if lexical != canonical:
        raise WorkflowError(code)
    return path.resolve()


def _git_required(root: Path, *args: str, code: str) -> str:
    try:
        result = _run(root, *args)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkflowError(code + ' ' + str(exc)) from exc
    if result.returncode:
        raise WorkflowError(code + ' ' + (result.stderr.strip() or result.stdout.strip()))
    return result.stdout.strip()


def _native_tool_payload(receipt: dict, key: str) -> dict:
    envelope = receipt.get(key)
    value = envelope.get('value') if isinstance(envelope, dict) else None
    if (
        not isinstance(envelope, dict)
        or envelope.get('status') != 'fulfilled'
        or not isinstance(value, dict)
        or value.get('isError') is not False
        or not isinstance(value.get('content'), list)
        or not value['content']
    ):
        raise WorkflowError('NATIVE_RECEIPT_INVALID ' + key)
    for item in value['content']:
        if isinstance(item, dict) and item.get('type') == 'text':
            try:
                payload = json.loads(item.get('text') or '')
            except (TypeError, ValueError) as exc:
                raise WorkflowError('NATIVE_RECEIPT_INVALID ' + key) from exc
            if isinstance(payload, dict):
                return payload
    raise WorkflowError('NATIVE_RECEIPT_INVALID ' + key)


def verify_registered_native_workspace(
    control_root: Path,
    source_root: Path,
    request: WorktreeRequest,
    receipt: dict,
    expected: WorkspaceExpectation,
    *,
    allow_dirty: bool = False,
    allow_detached: bool = False,
) -> WorkspaceProof:
    """Prove one external checkout from Git plus exact native attachment facts.

    This function is intentionally read-only. A receipt is only one input; the
    returned proof records the independently observed Git registration,
    backlink, branch, HEAD, base ancestry and cleanliness facts.
    """
    if type(allow_dirty) is not bool:
        raise WorkflowError('NATIVE_ALLOW_DIRTY_INVALID')
    if not isinstance(expected, WorkspaceExpectation):
        raise WorkflowError('WORKSPACE_EXPECTATION_REQUIRED')
    expected.validate()
    if not isinstance(request, WorktreeRequest) or not isinstance(request.request, RequestRef):
        raise WorkflowError('WORKTREE_REQUEST_REQUIRED')
    if not isinstance(receipt, dict):
        raise WorkflowError('NATIVE_RECEIPT_REQUIRED')

    control = _strict_expected_path(
        str(control_root), 'NATIVE_CONTROL_ROOT_ALIAS_OR_ESCAPE',
    )
    source = _strict_expected_path(
        str(source_root), 'NATIVE_SOURCE_ROOT_ALIAS_OR_ESCAPE',
    )
    expected_control = _strict_expected_path(
        expected.control_root, 'NATIVE_CONTROL_ROOT_ALIAS_OR_ESCAPE',
    )
    expected_source = _strict_expected_path(
        expected.source_root, 'NATIVE_SOURCE_ROOT_ALIAS_OR_ESCAPE',
    )
    common = _strict_expected_path(
        expected.common_dir, 'NATIVE_COMMON_DIR_ALIAS_OR_ESCAPE',
    )
    if not _same_path(control, expected_control):
        raise WorkflowError('NATIVE_CONTROL_ROOT_MISMATCH')
    if not _same_path(source, expected_source):
        raise WorkflowError('NATIVE_SOURCE_ROOT_MISMATCH alias or escape is not accepted')
    if _same_path(control, source):
        raise WorkflowError('NATIVE_WORKSPACE_OUTSIDE_CONTROL_REQUIRED')
    if not control.is_dir() or not source.is_dir():
        raise WorkflowError('NATIVE_WORKSPACE_MISSING')

    marker = control / '.gsp' / 'project.json'
    if not marker.is_file():
        raise WorkflowError('NATIVE_CONTROL_MARKER_MISSING')
    try:
        marker_value = json.loads(marker.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise WorkflowError('NATIVE_CONTROL_MARKER_INVALID') from exc
    if marker_value.get('id') != expected.project_uid:
        raise WorkflowError('NATIVE_CONTROL_PROJECT_MISMATCH')

    source_marker = source / '.gsp' / 'project.json'
    if source_marker.is_file():
        try:
            source_identity = json.loads(
                source_marker.read_text(encoding='utf-8-sig')
            )
        except (OSError, ValueError) as exc:
            raise WorkflowError('NATIVE_SOURCE_MARKER_INVALID') from exc
        if source_identity.get('id') != expected.project_uid:
            raise WorkflowError('NATIVE_SOURCE_PROJECT_MISMATCH')

    backlink_path = source / '.git'
    if not backlink_path.is_file():
        raise WorkflowError(
            'NATIVE_WORKSPACE_UNREGISTERED source is not a linked worktree'
        )
    try:
        backlink_text = backlink_path.read_text(encoding='utf-8').strip()
    except OSError as exc:
        raise WorkflowError('NATIVE_BACKLINK_INVALID') from exc
    if not backlink_text.lower().startswith('gitdir:'):
        raise WorkflowError('NATIVE_BACKLINK_INVALID')
    raw_git_dir = backlink_text.split(':', 1)[1].strip()
    backlink_target = (
        Path(raw_git_dir) if Path(raw_git_dir).is_absolute()
        else (source / raw_git_dir)
    ).resolve()
    if not backlink_target.is_dir():
        raise WorkflowError('NATIVE_BACKLINK_INVALID missing gitdir')

    control_common_raw = _git_required(
        control, 'rev-parse', '--git-common-dir',
        code='NATIVE_CONTROL_GIT_UNVERIFIED',
    )
    source_top = Path(_git_required(
        source, 'rev-parse', '--show-toplevel',
        code='NATIVE_SOURCE_GIT_UNVERIFIED',
    )).resolve()
    source_common_raw = _git_required(
        source, 'rev-parse', '--git-common-dir',
        code='NATIVE_SOURCE_GIT_UNVERIFIED',
    )
    source_git_raw = _git_required(
        source, 'rev-parse', '--git-dir',
        code='NATIVE_SOURCE_GIT_UNVERIFIED',
    )
    source_common = (source / source_common_raw).resolve()
    source_git_dir = (source / source_git_raw).resolve()
    control_common = (control / control_common_raw).resolve()
    if not _same_path(source_top, source):
        raise WorkflowError('NATIVE_SOURCE_TOP_LEVEL_MISMATCH')
    if not _same_path(control_common, common):
        raise WorkflowError('NATIVE_CONTROL_COMMON_DIR_MISMATCH')
    if not _same_path(source_common, common):
        raise WorkflowError('NATIVE_COMMON_DIR_MISMATCH other repository')
    if not _same_path(source_git_dir, backlink_target):
        raise WorkflowError('NATIVE_BACKLINK_MISMATCH')
    if _git_required(
        source, 'rev-parse', '--is-bare-repository',
        code='NATIVE_SOURCE_GIT_UNVERIFIED',
    ) != 'false':
        raise WorkflowError('NATIVE_SOURCE_BARE_UNSUPPORTED')
    if _git_required(
        source, 'rev-parse', '--is-inside-work-tree',
        code='NATIVE_SOURCE_GIT_UNVERIFIED',
    ) != 'true':
        raise WorkflowError('NATIVE_SOURCE_WORKTREE_UNKNOWN')
    superproject = _git_required(
        source, 'rev-parse', '--show-superproject-working-tree',
        code='NATIVE_SOURCE_GIT_UNVERIFIED',
    )
    if superproject:
        raise WorkflowError('NATIVE_SOURCE_SUBMODULE_UNSUPPORTED')

    raw_worktrees = _git_required(
        control, 'worktree', 'list', '--porcelain', '-z',
        code='NATIVE_WORKTREE_LIST_UNVERIFIED',
    )
    worktrees = _parse_worktrees(raw_worktrees)
    registrations = [
        row for row in worktrees
        if row.get('path') and _same_path(row['path'], source)
    ]
    if len(registrations) != 1:
        raise WorkflowError('NATIVE_WORKSPACE_UNREGISTERED')
    registration = registrations[0]
    branch = _run(source, 'branch', '--show-current')
    if branch.returncode:
        raise WorkflowError('NATIVE_SOURCE_GIT_UNVERIFIED ' + branch.stderr.strip())
    branch_name = branch.stdout.strip()
    detached=not branch_name and bool(registration.get('detached'))
    if (not branch_name or registration.get('detached')) and not (allow_detached and detached):
        raise WorkflowError('NATIVE_WORKSPACE_DETACHED')
    head = _git_required(
        source, 'rev-parse', 'HEAD', code='NATIVE_SOURCE_HEAD_UNVERIFIED',
    )
    if registration.get('head') != head:
        raise WorkflowError('NATIVE_REGISTRATION_HEAD_MISMATCH')
    if registration.get('branch') != (branch_name or None):
        raise WorkflowError('NATIVE_REGISTRATION_BRANCH_MISMATCH')
    status = _run(
        source, 'status', '--porcelain=v1', '-z', '--untracked-files=all',
    )
    if status.returncode:
        raise WorkflowError('NATIVE_SOURCE_STATUS_UNVERIFIED ' + status.stderr.strip())
    clean = not bool(status.stdout)
    if not clean and not allow_dirty:
        raise WorkflowError('NATIVE_WORKSPACE_DIRTY')

    request_digest = digest_value(request.to_dict())
    receipt_digest = digest_value(receipt)
    if request.request.project_uid != expected.project_uid:
        raise WorkflowError('NATIVE_REQUEST_PROJECT_MISMATCH')
    if request.request.work_uid != expected.work_uid:
        raise WorkflowError('NATIVE_REQUEST_WORK_MISMATCH')
    if request.request.request_uid != expected.request_uid:
        raise WorkflowError('NATIVE_REQUEST_UID_MISMATCH')
    if request.owner_adapter != expected.owner_adapter:
        raise WorkflowError('NATIVE_REQUEST_OWNER_MISMATCH')
    if not _same_path(request.source_root, control):
        raise WorkflowError('NATIVE_REQUEST_SOURCE_MISMATCH')
    if request.path_hint and not _same_path(request.path_hint, source):
        raise WorkflowError('NATIVE_REQUEST_PATH_MISMATCH')
    if (
        request.base_sha != expected.base_sha
        or request.branch != expected.branch
        or request_digest != expected.request_digest
    ):
        raise WorkflowError('NATIVE_REQUEST_CHANGED')
    if receipt_digest != expected.receipt_digest:
        raise WorkflowError('NATIVE_RECEIPT_DIGEST_MISMATCH')
    if (
        receipt.get('schema') != 1
        or receipt.get('kind') != 'observed-native-tool-receipts'
        or receipt.get('request_uid') != expected.request_uid
    ):
        raise WorkflowError('NATIVE_RECEIPT_REQUEST_MISMATCH')

    creation = _native_tool_payload(receipt, 'creation_status')
    operation_id = str(creation.get('operationId') or '').strip()
    result = creation.get('result') if isinstance(creation, dict) else None
    if (
        not operation_id
        or creation.get('stage') != 'completed'
        or receipt.get('operation_id') != operation_id
        or not isinstance(result, dict)
        or result.get('type') != 'created'
        or result.get('registrationError') not in (None, '')
        or not result.get('worktreeGitRoot')
        or not result.get('worktreeWorkspaceRoot')
        or not _same_path(result['worktreeGitRoot'], source)
        or not _same_path(result['worktreeWorkspaceRoot'], source)
    ):
        raise WorkflowError('NATIVE_CREATION_RECEIPT_MISMATCH')

    attachments_payload = _native_tool_payload(
        receipt, 'attachments_after_creation',
    )
    attachments = attachments_payload.get('attachments')
    if not isinstance(attachments, list):
        raise WorkflowError('NATIVE_ATTACHMENT_RECEIPT_INVALID')
    matches = []
    for item in attachments:
        payload = item.get('payload') if isinstance(item, dict) else None
        if not isinstance(payload, dict):
            continue
        if (
            item.get('attachmentType') == 'worktree'
            and item.get('identityKey')
            and _same_path(item['identityKey'], source)
            and payload.get('root')
            and payload.get('workspaceRoot')
            and _same_path(payload['root'], source)
            and _same_path(payload['workspaceRoot'], source)
            and payload.get('sourceCwd')
            and _same_path(payload['sourceCwd'], control)
        ):
            matches.append(item)
    if len(matches) != 1:
        raise WorkflowError('NATIVE_ATTACHMENT_MISMATCH')
    attachment = matches[0]
    host_owner = str(attachment['payload'].get('hostId') or '')
    if expected.owner != host_owner:
        raise WorkflowError('NATIVE_WORKSPACE_OWNER_MISMATCH')
    if (not detached and expected.branch != branch_name) or expected.branch != request.branch:
        raise WorkflowError('NATIVE_WORKSPACE_BRANCH_MISMATCH')
    if expected.head_sha != head:
        raise WorkflowError('NATIVE_WORKSPACE_HEAD_MISMATCH')
    ancestry = _run(source, 'merge-base', '--is-ancestor', expected.base_sha, head)
    if ancestry.returncode:
        raise WorkflowError('NATIVE_WORKSPACE_BASE_MISMATCH')

    return WorkspaceProof(
        project_uid=expected.project_uid,
        work_uid=expected.work_uid,
        task_uid=expected.task_uid,
        owner=expected.owner,
        owner_adapter=expected.owner_adapter,
        control_root=str(control),
        source_root=str(source),
        source_top_level=str(source_top),
        common_dir=str(common),
        source_common_dir=str(source_common),
        git_dir=str(source_git_dir),
        marker_path=str(marker.resolve()),
        marker_digest=hashlib.sha256(marker.read_bytes()).hexdigest(),
        backlink_path=str(backlink_path.resolve()),
        backlink_value=backlink_text,
        registered=True,
        registration_head=str(registration.get('head')),
        registration_branch=str(registration.get('branch')),
        base_sha=expected.base_sha,
        head=head,
        branch=branch_name or None,
        detached=detached,
        clean=clean,
        native_operation_id=operation_id,
        attachment_id=str(attachment.get('id') or ''),
        request_uid=expected.request_uid,
        request_digest=request_digest,
        receipt_digest=receipt_digest,
        verified_at=_now_iso(),
    )


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec='seconds')


def recommend(inventory: dict, context: dict) -> dict:
    """Recommend a mode without mutating Git or pretending to have user consent."""
    risks = list(inventory.get('risks') or [])
    if inventory.get('status') == 'not-a-repository':
        return {'mode': None, 'reasons': ['当前目录不是 Git 仓库'], 'blocking': risks}
    intent = str(context.get('intent') or 'single-task')
    independent = context.get('independent_verification') is True
    protect = context.get('protect_existing') is True
    changed = inventory.get('current', {}).get('source_changed_paths')
    if changed is None:
        changed = inventory.get('current', {}).get('changed_paths') or []
    branch = inventory.get('current', {}).get('branch')
    number = str(context.get('number') or '')
    owned = context.get('owns_branch')
    if owned is None:
        owned = bool(number and branch and re.search(re.escape(number), str(branch)))
    reasons: list[str] = []
    if changed:
        mode = 'worktree'
        reasons.append('当前工作区有脏改动，保留现场并使用干净 worktree')
    elif independent:
        mode = 'worktree'
        reasons.append('需要独立验证，使用独立 worktree 隔离验证环境')
    elif protect:
        mode = 'worktree'
        reasons.append('需要保护现有改动，使用干净 worktree 避免覆盖现场')
    elif intent in _INTENT_WORKTREE:
        mode = 'worktree'
        reasons.append('任务包含并行、跨会话或全栈协作，需要独立 worktree')
    elif branch in (None, 'main', 'master'):
        mode = 'branch'
        reasons.append('当前位于主分支或未命名 HEAD，先建立带 GSP 编号的任务分支')
    elif owned:
        mode = 'in-place'
        reasons.append('当前工作区干净且分支已归属于本任务，可在原地继续')
    else:
        mode = 'branch'
        reasons.append('当前工作区干净，使用独立任务分支即可保持低负担隔离')
    if context.get('size') == 'large':
        reasons.append('任务规模较大但不单独要求 worktree；需要独立验证时显式声明')
    blocking = [risk for risk in risks if risk.get('severity') == 'blocking']
    if blocking:
        reasons.append('存在阻塞风险，选择前必须先处理或明确覆盖')
    return {'mode': mode, 'reasons': reasons, 'blocking': blocking}


def _binding_index(root):
    """Map bound workspace to (run, status, task id) for residue reporting."""
    from .core import (read)
    index = {}
    for file in (Path(root) / '.gsp' / 'runs').glob('*/state.json'):
        try:
            state = read(file)
        except (WorkflowError, OSError):
            continue
        bindings = [(state.get('id'), state.get('status'), None, state.get('git_topology') or {})]
        bindings += [(state.get('id'), task.get('status'), task.get('id'), task.get('topology') or {})
                     for task in state.get('tasks', []) if task.get('topology')]
        for run_id, status, task_id, binding in bindings:
            workspace = binding.get('workspace_root')
            if not workspace:
                continue
            key = os.path.normcase(str(Path(workspace).resolve()))
            index[key] = {'run': run_id, 'status': status, 'task': task_id}
    return index


def residue(root, states=None):
    """Read-only list of clean, merged worktrees whose run already finished."""
    root = Path(root).resolve()
    try:
        inventory = _probe(root, active_tasks=[], include_branches=False, include_status=False)
    except (OSError, subprocess.TimeoutExpired, WorkflowError):
        return {'count': 0, 'worktrees': [], 'command': None}
    if inventory.get('status') == 'not-a-repository':
        return {'count': 0, 'worktrees': [], 'command': None}
    main_head = inventory.get('current', {}).get('head')
    bindings = states if states is not None else _binding_index(root)
    rows = []
    for worktree in inventory.get('worktrees', []):
        path = Path(worktree.get('path') or '').resolve()
        if not path or path == root or not path.is_dir():
            continue
        binding = bindings.get(os.path.normcase(str(path))) or {}
        branch = worktree.get('branch')
        head = worktree.get('head')
        dirty = _run(path, 'status', '--porcelain', '-uall')
        clean = dirty.returncode == 0 and not dirty.stdout.strip()
        merged = bool(head and main_head and
                      _run(root, 'merge-base', '--is-ancestor', head, main_head).returncode == 0)
        rows.append({
            'run': binding.get('run'),
            'task': binding.get('task'),
            'run_status': binding.get('status'),
            'path': str(path),
            'branch': branch,
            'merged': merged,
            'clean': clean,
        })
    stale = [row for row in rows if row['merged'] and row['clean']
             and row['run_status'] in {'ready', 'closed'}]
    return {'count': len(stale), 'worktrees': rows,
            'command': 'python scripts/gsp.py --project <root> topology-clean <id> --apply' if stale else None}


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
