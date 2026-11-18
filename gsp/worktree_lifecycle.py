"""Safe creation, binding and cleanup of GSP-managed child worktrees."""

from pathlib import Path
import re
import subprocess

from .runtime import WorkflowError, _git_run, git_identity, inside, now


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
    number = str(state.get("number") or state.get("id") or "task")
    slug = worktree_slug(number, task)
    base_dir = project.root / ".gsp" / "worktrees" / "sessions" / number
    return base_dir / slug, f"feature/{number}-{slug}"


def prepare_worktree(project, state, task, dispatch, requested_path=None, mode="auto"):
    """Create or bind one child worktree before publishing a dispatch."""
    if requested_path is not None or mode == "bind":
        if requested_path is None:
            raise WorkflowError("WORKTREE_PATH_REQUIRED")
        path = Path(requested_path).resolve()
        if not inside(project.root, path) or path == project.root:
            raise WorkflowError("WORKTREE_PATH_ESCAPE " + str(path))
        inspection = inspect_worktree(project, path)
        return {
            **dispatch,
            "path": inspection["path"],
            "worktree": inspection["path"],
            "branch": inspection["branch"],
            "base": inspection["head"],
            "managed_worktree": False,
            "worktree_mode": "bound",
            "worktree_state": "bound",
            "cleanup_attempts": 0,
            "cleanup_error": None,
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
    return {
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

    path = Path(dispatch.get("path") or dispatch.get("worktree") or "").resolve()
    if not path:
        return _pending(dispatch, _command_error("WORKTREE_PATH_MISSING"))
    if not path.exists():
        result = {**dispatch, "worktree_state": "removed", "result": "removed", "updated_at": now(), "idempotent": True}
        return result
    status = _git_run(path, "status", "--porcelain", "-uall")
    if status.returncode:
        return _pending(dispatch, _command_error("WORKTREE_STATUS_FAILED", status))
    if status.stdout.strip():
        return _pending(dispatch, _command_error("WORKTREE_DIRTY", status, status.stdout.strip()))
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


