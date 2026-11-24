"""Safe creation, binding and cleanup of GSP-managed child worktrees."""

from pathlib import Path
import re
from .runtime import WorkflowError, _git_run, git_identity, inside, now
from .build_policy import load_policy, remove_tree, tree_size


TERMINAL_STATES = {"removed", "released"}


def worktree_slug(number, task):
    """Return a stable ASCII slug for an automatically managed worktree."""
    task_id = re.sub(r"[^A-Za-z0-9_-]+", "-", str(task.get("id") or "task")).strip("-")
    title = re.sub(r"[^A-Za-z0-9_-]+", "-", str(task.get("title") or "")).strip("-").lower()
    pieces = [task_id, title or "worktree"]
    return re.sub(r"-+", "-", "-".join(pieces))[:48] or "task"


def _command_error(code, result=None, detail=None):
    if result is not None:
        detail = (result.stderr or result.stdout or "").strip()
        return {"code": code, "exit_code": result.returncode, "detail": detail}
    return {"code": code, "exit_code": None, "detail": detail or code}


def _inventory(project):
    result = _git_run(project.root, "worktree", "list", "--porcelain")
    if result.returncode:
        raise WorkflowError("WORKTREE_INVENTORY_FAILED " + (result.stderr or "").strip())
    rows = []
    current = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            if current:
                rows.append(current)
            current = {"path": str(Path(line[9:]).resolve())}
        elif current is not None and line.startswith("HEAD "):
            current["head"] = line[5:].strip()
        elif current is not None and line.startswith("branch "):
            current["branch"] = line[7:].strip().removeprefix("refs/heads/")
        elif current is not None and line == "detached":
            current["detached"] = True
    if current:
        rows.append(current)
    return rows


def inspect_worktree(project, path):
    resolved = Path(path).resolve()
    rows = _inventory(project)
    row = next((item for item in rows if Path(item["path"]).resolve() == resolved), None)
    if not row or not resolved.is_dir():
        raise WorkflowError("WORKTREE_NOT_REGISTERED " + str(resolved))
    identity = git_identity(resolved)
    if not identity.get("root") or Path(identity["root"]).resolve() != resolved:
        raise WorkflowError("WORKTREE_IDENTITY_INVALID " + str(resolved))
    return {
        "path": str(resolved),
        "branch": row.get("branch") or identity.get("branch"),
        "head": row.get("head") or identity.get("head"),
        "detached": bool(row.get("detached")),
        "registered": True,
    }


def _base(project, state):
    topology = state.get("git_topology") or {}
    base = topology.get("base_head") or topology.get("head")
    if base:
        return str(base)
    identity = git_identity(project.root)
    if not identity.get("head"):
        raise WorkflowError("WORKTREE_BASE_UNAVAILABLE")
    return identity["head"]


def _automatic_location(project, state, task):
    from .git_topology import branch_name
    number = str(state.get("number") or state.get("id") or "task")
    slug = worktree_slug(number, task)
    base_dir = project.root / ".gsp" / "worktrees" / "sessions" / number
    branch = branch_name(number, task.get("id"), task.get("title") or "任务")
    return base_dir / slug, branch


def build_root(project, state, slug):
    """Return the GSP-owned build directory for one managed worktree."""
    policy = load_policy(project.root)
    if policy['worktree_build'] == 'off':
        return None
    number = str(state.get('number') or state.get('id') or 'task')
    return (Path(project.root) / '.gsp' / 'build' / number / slug).resolve()


def build_environment(project, state, task):
    """Describe the build root and environment handed to a worker."""
    number = str(state.get('number') or state.get('id') or 'task')
    slug = worktree_slug(number, task)
    root = build_root(project, state, slug)
    if root is None:
        return {'build_root': None, 'build_env': {}, 'build_mode': 'worktree',
                'build_context': {'schema': 1, 'mode': 'off', 'root': None,
                                  'env': {}, 'provenance': 'worktree'}}
    relative = root.relative_to(Path(project.root).resolve()).as_posix()
    from .build_context import compatibility_key
    environment = {'CARGO_TARGET_DIR': str(root), 'BUILD_ROOT': str(root)}
    return {'build_root': relative, 'build_env': environment, 'build_mode': 'managed',
            'build_context': {'schema': 1, 'mode': 'isolated', 'root': str(root),
                              'env': environment, 'provenance': 'worktree',
                              'compatibility_key': compatibility_key(project)}}


def _build_root_referenced(project, relative, exclude_id):
    if not relative:
        return False
    from .runtime import read
    for file in Path(project.meta).glob('runs/*/sessions/dispatch.json'):
        try:
            data = read(file)
        except (WorkflowError, OSError):
            continue
        for row in data.get('dispatches', []):
            if row.get('id') == exclude_id or row.get('status') in {'superseded', 'cleaned'}:
                continue
            if row.get('build_root') == relative:
                return True
    return False


def prepare_worktree(project, state, task, dispatch, requested_path=None, mode="auto"):
    """Create or bind one child worktree before publishing a dispatch."""
    if requested_path is not None or mode == "bind":
        if requested_path is None:
            raise WorkflowError("WORKTREE_PATH_REQUIRED")
        path = Path(requested_path).resolve()
        if not inside(project.root, path):
            raise WorkflowError("WORKTREE_PATH_ESCAPE " + str(path))
        if not path.is_dir():
            raise WorkflowError("WORKTREE_PATH_MISSING " + str(path))
        try:
            inspection = inspect_worktree(project, path)
        except WorkflowError as exc:
            if not str(exc).startswith("WORKTREE_NOT_REGISTERED") and not str(exc).startswith("WORKTREE_IDENTITY_INVALID"):
                raise
            # Legacy callers may bind an already-created source directory that
            # is managed by their host rather than by Git. It is always
            # non-owned and is never removed by the lifecycle adapter.
            inspection = {
                "path": str(path), "branch": None, "head": None,
                "detached": False, "registered": False,
            }
        return {
            **dispatch,
            "path": inspection["path"],
            "worktree": inspection["path"],
            "branch": inspection["branch"],
            "base": inspection["head"],
            "managed_worktree": False,
            "worktree_mode": "bound",
            "binding_verified": inspection["registered"],
            "worktree_state": "bound",
            "cleanup_attempts": 0,
            "cleanup_error": None,
            "build_root": None,
            "build_env": {},
            "build_mode": "bound",
            "created_at": now(),
            "updated_at": now(),
        }

    path, branch = _automatic_location(project, state, task)
    path = path.resolve()
    if not inside(project.root, path) or path == project.root:
        raise WorkflowError("WORKTREE_PATH_ESCAPE " + str(path))
    if path.exists() and any(path.iterdir()):
        raise WorkflowError("WORKTREE_PATH_CONFLICT " + str(path))
    show = _git_run(project.root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    if show.returncode == 0:
        raise WorkflowError("WORKTREE_BRANCH_CONFLICT " + branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    base = _base(project, state)
    result = _git_run(project.root, "worktree", "add", "-b", branch, str(path), base)
    if result.returncode:
        raise WorkflowError("WORKTREE_CREATE_FAILED " + ((result.stderr or result.stdout or "").strip()))
    try:
        inspection = inspect_worktree(project, path)
    except (WorkflowError, OSError) as exc:
        cleanup = _git_run(project.root, "worktree", "remove", str(path))
        if cleanup.returncode:
            raise WorkflowError("WORKTREE_CREATE_UNVERIFIED cleanup-pending " + (cleanup.stderr or "").strip()) from exc
        raise WorkflowError("WORKTREE_CREATE_UNVERIFIED " + str(exc)) from exc
    dispatch_row = {
        **dispatch,
        "path": inspection["path"],
        "worktree": inspection["path"],
        "branch": inspection["branch"] or branch,
        "base": base,
        "managed_worktree": True,
        "worktree_mode": "managed",
        "worktree_state": "created",
        "cleanup_attempts": 0,
        "cleanup_error": None,
        "created_at": now(),
        "updated_at": now(),
    }
    dispatch_row.update(build_environment(project, state, task))
    try:
        project.bind_task_topology(
            state, task.get("id"), "worktree",
            inspection["branch"] or branch, inspection["path"],
            "controller-dispatch",
        )
    except WorkflowError as exc:
        cleaned = cleanup_worktree(project, dispatch_row, "dispatch bind rollback", retry=True)
        if cleaned.get("result") == "cleanup-pending":
            raise WorkflowError("DISPATCH_BIND_ROLLBACK_CLEANUP_PENDING " + str(path)) from exc
        raise WorkflowError("DISPATCH_BIND_FAILED " + str(exc)) from exc
    return dispatch_row


def _pending(dispatch, error):
    result = dict(dispatch)
    result["worktree_state"] = "cleanup-pending"
    result["result"] = "cleanup-pending"
    result["cleanup_attempts"] = int(result.get("cleanup_attempts") or 0) + 1
    result["cleanup_error"] = error
    result["updated_at"] = now()
    result["idempotent"] = False
    return result


def cleanup_worktree(project, dispatch, reason, retry=False):
    """Safely remove a managed worktree or release a bound one."""
    state = dispatch.get("worktree_state")
    if state in TERMINAL_STATES:
        return {**dispatch, "result": state, "idempotent": True}
    if not dispatch.get("managed_worktree", False):
        return {
            **dispatch,
            "worktree_state": "released",
            "result": "released",
            "cleanup_error": None,
            "updated_at": now(),
            "idempotent": False,
        }

    raw_path = dispatch.get("path") or dispatch.get("worktree")
    if not raw_path:
        return _pending(dispatch, _command_error("WORKTREE_PATH_MISSING"))
    path = Path(raw_path).resolve()
    if not path.exists():
        _remove_build_root(project, dispatch)
        result = {**dispatch, "worktree_state": "removed", "result": "removed", "updated_at": now(), "idempotent": True}
        return result
    status = _git_run(path, "status", "--porcelain", "-uall")
    if status.returncode:
        return _pending(dispatch, _command_error("WORKTREE_STATUS_FAILED", status))
    if status.stdout.strip():
        pending = _pending(dispatch, _command_error("WORKTREE_DIRTY", status, status.stdout.strip()))
        build = _build_path(project, dispatch)
        if build is not None and build.exists():
            pending["pending_build_bytes"] = tree_size(build)
        return pending
    build = _build_path(project, dispatch)
    if build is not None and build.exists() and not _build_root_referenced(
            project, dispatch.get("build_root"), dispatch.get("id")):
        try:
            remove_tree(build, project.root)
        except OSError as exc:
            return _pending(dispatch, _command_error("BUILD_ROOT_REMOVE_FAILED", detail=str(exc)))
    removed = _git_run(project.root, "worktree", "remove", str(path))
    if removed.returncode:
        return _pending(dispatch, _command_error("WORKTREE_REMOVE_FAILED", removed))
    prune = _git_run(project.root, "worktree", "prune")
    result = {
        **dispatch,
        "worktree_state": "removed",
        "result": "removed",
        "cleanup_attempts": int(dispatch.get("cleanup_attempts") or 0) + 1,
        "updated_at": now(),
        "idempotent": False,
        "cleanup_error": None,
    }
    if prune.returncode:
        result["cleanup_error"] = _command_error("WORKTREE_PRUNE_FAILED", prune)
    return result


def _build_path(project, dispatch):
    relative = dispatch.get("build_root")
    if not relative:
        return None
    return Path(project.root) / relative


def _remove_build_root(project, dispatch):
    relative = dispatch.get("build_root")
    build = _build_path(project, dispatch)
    if build is None or not build.exists():
        return
    if _build_root_referenced(project, relative, dispatch.get("id")):
        return
    try:
        remove_tree(build, project.root)
    except OSError:
        pass


