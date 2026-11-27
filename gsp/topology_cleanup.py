"""Recoverable cleanup for merged GSP-managed worktrees and branches."""
from __future__ import annotations

import os
from pathlib import Path

from .core import WorkflowError, atomic, inside, read
from .resource_registry import ResourceRegistry, ResourceRegistryError


def _active_paths(active_worktrees, project, state):
    values = active_worktrees(project, state) if callable(active_worktrees) else active_worktrees
    return {
        os.path.normcase(str(Path(value).resolve()))
        for value in (values or ())
        if value
    }


def _run_dir(project, state):
    return Path(project.meta) / "runs" / str(state["id"])


def _previous(project, state):
    path = _run_dir(project, state) / "topology-clean.json"
    if not path.is_file():
        return {}
    try:
        document = read(path)
    except WorkflowError:
        return {}
    if document.get("run") != state.get("id") or document.get("apply") is not True:
        return {}
    return document


def _previous_row(previous, path):
    key = os.path.normcase(str(Path(path).resolve()))
    for row in list(previous.get("rows") or []) + list(previous.get("applied") or []):
        if row.get("path") and os.path.normcase(
            str(Path(row["path"]).resolve())
        ) == key:
            return row
    return {}


def _verified_removal(previous, path):
    row = _previous_row(previous, path)
    proof = row.get("worktree_disposition") or {}
    return bool(
        row.get("verified_absent") is True
        or (
            proof.get("action") == "remove"
            and proof.get("state") == "completed"
            and proof.get("verified_absent") is True
            and proof.get("path")
            and Path(proof["path"]).resolve() == Path(path).resolve()
        )
    )


def _branch_exists(git_run, root, branch):
    result = git_run(
        root, "show-ref", "--verify", "--quiet", "refs/heads/" + str(branch)
    )
    if result.returncode == 0:
        return True, None
    if result.returncode == 1:
        return False, None
    return None, (result.stderr or result.stdout or "git show-ref failed").strip()


def _step(row, name):
    for item in row["steps"]:
        if item["name"] == name:
            return item
    item = {"name": name, "state": "pending", "attempts": 0, "error": None}
    row["steps"].append(item)
    return item


def _complete(row, name):
    item = _step(row, name)
    item.update({"state": "completed", "error": None})
    row["pending_step"] = None


def _fail(row, name, error, reason=None):
    item = _step(row, name)
    item["state"] = "failed"
    item["attempts"] = int(item.get("attempts") or 0) + 1
    item["error"] = error
    row["pending_step"] = name
    row["action"] = "keep"
    row["reason"] = reason or {
        "worktree_remove": "REMOVE_FAILED",
        "branch_delete": "BRANCH_DELETE_FAILED",
        "prune": "PRUNE_FAILED",
    }[name]
    row["error"] = error


def _steps(row):
    return {
        "worktree_remove": _step(row, "worktree_remove"),
        "branch_delete": _step(row, "branch_delete"),
        "prune": _step(row, "prune"),
    }


def _reject_unresolved_authority(project, state):
    if state.get("inflight"):
        raise WorkflowError(
            "EXECUTION_UNKNOWN recover the recorded child before topology cleanup"
        )
    work_uid = state.get("work_uuid")
    if not work_uid:
        return
    try:
        records = ResourceRegistry(project.root).list_for_work(work_uid)
    except ResourceRegistryError as exc:
        if "RESOURCE_AUTHORITY_MISSING" in str(exc):
            return
        raise WorkflowError("TOPOLOGY_RESOURCE_CHECK_FAILED " + str(exc)) from exc
    if any(row["kind"] == "slot" for row in records):
        raise WorkflowError("EXECUTION_UNKNOWN an execution slot is still reserved")


def _bindings(state, task_id):
    rows = []
    parent = state.get("git_topology") or {}
    if parent.get("workspace_root") and parent.get("effective_mode") == "worktree":
        rows.append({"task": None, "status": state.get("status"), "binding": parent})
    for task in state.get("tasks") or []:
        if task_id and task.get("id") != task_id:
            continue
        topology = task.get("topology") or {}
        if topology.get("workspace_root"):
            rows.append({
                "task": task.get("id"),
                "status": task.get("status"),
                "binding": topology,
            })
    return rows


def _pending_step_from_previous(row, previous):
    old = _previous_row(previous, row["path"])
    if not old:
        return
    previous_steps = {
        item.get("name"): item for item in old.get("steps") or []
        if isinstance(item, dict)
    }
    for current in row["steps"]:
        prior = previous_steps.get(current["name"])
        if prior:
            current.update(prior)
    if old.get("pending_step"):
        _step(row, old["pending_step"])
        row["pending_step"] = old["pending_step"]


def _inspect_rows(project, state, task_id, previous, git_run, git_identity, active):
    rows = []
    seen = set()
    managed_root = Path(project.root) / ".gsp" / "worktrees"
    base = (state.get("git_topology") or {}).get("base_branch") or "main"
    for item in _bindings(state, task_id):
        binding = item["binding"]
        path = Path(binding["workspace_root"]).resolve()
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        identity = git_identity(path) if path.is_dir() else {}
        actual_head = identity.get("head")
        prior = _previous_row(previous, path)
        # A bound workspace normally advances while its task is implemented.
        # Freeze the observed removal candidate, then keep that head on replay.
        expected_head = (actual_head if path.is_dir() else prior.get("head"))
        expected_head = expected_head or binding.get("head") or binding.get("base_head")
        branch = identity.get("branch") or binding.get("branch")
        row = {
            "run": state.get("id"),
            "task": item["task"],
            "path": str(path),
            "branch": branch,
            "head": expected_head or actual_head,
            "actual_head": actual_head,
            "dirty": None,
            "merged": False,
            "active": False,
            "action": "keep",
            "reason": None,
            "verified_absent": False,
            "pending_step": None,
            "steps": [],
        }
        _steps(row)
        _pending_step_from_previous(row, previous)

        if (
            binding.get("managed_worktree") is False
            or binding.get("cleanup_action") in {"release", "archive"}
            or binding.get("worktree_mode") in {"bound", "native"}
        ):
            row["reason"] = "EXTERNAL_OWNER"
            rows.append(row)
            continue
        if path == Path(project.root).resolve() or not inside(managed_root, path):
            row["reason"] = "OUTSIDE_MANAGED_ROOT"
            rows.append(row)
            continue
        if not path.is_dir():
            if _verified_removal(previous, path):
                row["verified_absent"] = True
                _complete(row, "worktree_remove")
                branch_present, error = _branch_exists(git_run, project.root, branch)
                if error:
                    row["reason"] = "BRANCH_STATUS_UNKNOWN"
                    row["pending_step"] = "branch_delete"
                    _fail(row, "branch_delete", error)
                elif branch_present is False:
                    _complete(row, "branch_delete")
                    row["reason"] = "ALREADY_DISPOSED"
                else:
                    row["action"] = "remove"
                    row["reason"] = "RESUME_BRANCH_DELETE"
            else:
                row["reason"] = "WORKSPACE_MISSING_NO_PROOF"
                row["pending_step"] = "worktree_remove"
                _fail(
                    row,
                    "worktree_remove",
                    "missing workspace has no verified removal proof",
                    reason="WORKSPACE_MISSING_NO_PROOF",
                )
            rows.append(row)
            continue

        status = git_run(path, "status", "--porcelain", "-uall")
        files = [
            line[3:].strip() for line in status.stdout.splitlines() if line.strip()
        ]
        row["dirty"] = bool(files) if status.returncode == 0 else True
        row["dirty_files"] = files[:20]
        row["active"] = key in active or item["status"] == "running"
        ancestor = git_run(
            project.root,
            "merge-base",
            "--is-ancestor",
            str(expected_head or actual_head or ""),
            base,
        )
        row["merged"] = bool(expected_head or actual_head) and ancestor.returncode == 0
        if expected_head and actual_head and expected_head != actual_head:
            row["reason"] = "HEAD_DRIFT"
        elif status.returncode:
            row["reason"] = "STATUS_UNKNOWN"
        elif row["active"]:
            row["reason"] = "ACTIVE"
        elif row["dirty"]:
            row["reason"] = "DIRTY"
        elif binding.get("branch") and branch != binding.get("branch"):
            row["reason"] = "BRANCH_DRIFT"
        elif not row["merged"]:
            row["reason"] = "UNMERGED"
        else:
            row["action"] = "remove"
            row["reason"] = "MERGED_CLEAN"
        rows.append(row)
    return rows


def _summary(document):
    rows = document["rows"]
    pending_rows = [
        row for row in rows
        if row.get("pending_step")
        or row.get("reason") in {
            "WORKSPACE_MISSING_NO_PROOF",
            "BRANCH_STATUS_UNKNOWN",
            "STATUS_UNKNOWN",
        }
    ]
    document["pending"] = bool(pending_rows or document.get("prune_error"))
    document["steps"] = [
        {
            "task": row.get("task"),
            "path": row.get("path"),
            "pending_step": row.get("pending_step"),
            "steps": row.get("steps") or [],
        }
        for row in rows
    ]
    completed_paths = {
        row.get("path") for row in document.get("applied") or []
        if row.get("path")
    }
    document["refused"] = [
        row for row in rows
        if row.get("path") not in completed_paths
        and (
            row.get("action") != "remove"
            or row.get("reason") in {
                "BRANCH_DELETE_FAILED",
                "PRUNE_FAILED",
            }
        )
    ]
    return document


def _persist(project, state, document):
    _summary(document)
    atomic(_run_dir(project, state) / "topology-clean.json", document)
    return document


def clean_topology(
    project,
    state,
    task_id=None,
    apply=False,
    *,
    git_run,
    git_identity,
    active_worktrees,
    clock,
):
    """Preview or recoverably remove merged, clean, inactive worktrees."""
    _reject_unresolved_authority(project, state)
    previous = _previous(project, state)
    rows = _inspect_rows(
        project,
        state,
        task_id,
        previous,
        git_run,
        git_identity,
        _active_paths(active_worktrees, project, state),
    )
    document = {
        "schema": 2,
        "run": state.get("id"),
        "plan_task": task_id,
        "apply": bool(apply),
        "at": clock(),
        "rows": rows,
        "applied": [],
        "refused": [],
        "prune_error": None,
        "pending": False,
        "steps": [],
    }
    prior_applied = [
        row for row in previous.get("applied") or []
        if row.get("verified_absent")
        and row.get("path")
        and not Path(row["path"]).exists()
    ]
    document["applied"].extend(prior_applied)
    if not apply:
        return _summary(document)

    for row in rows:
        if row.get("action") != "remove":
            continue
        path = Path(row["path"])
        if path.is_dir():
            removed = git_run(project.root, "worktree", "remove", row["path"])
            if removed.returncode:
                _fail(
                    row,
                    "worktree_remove",
                    (removed.stderr or removed.stdout or "git worktree remove failed").strip(),
                )
                _persist(project, state, document)
                continue
            if path.exists():
                _fail(row, "worktree_remove", "worktree still exists after removal")
                _persist(project, state, document)
                continue
            row["verified_absent"] = True
            _complete(row, "worktree_remove")
            row["worktree_disposition"] = {
                "schema": 1,
                "action": "remove",
                "state": "completed",
                "path": row["path"],
                "verified_absent": True,
                "completed_at": clock(),
            }
            _persist(project, state, document)
        elif _verified_removal(previous, path):
            row["verified_absent"] = True
            _complete(row, "worktree_remove")
        else:
            _fail(
                row,
                "worktree_remove",
                "missing workspace has no verified removal proof",
                reason="WORKSPACE_MISSING_NO_PROOF",
            )
            _persist(project, state, document)
            continue

        branch = row.get("branch")
        if branch:
            present, error = _branch_exists(git_run, project.root, branch)
            if error:
                _fail(row, "branch_delete", error)
                _persist(project, state, document)
                continue
            if present:
                current = git_run(
                    project.root,
                    "rev-parse",
                    "--verify",
                    f"refs/heads/{branch}^{{commit}}",
                )
                current_head = current.stdout.strip() if current.returncode == 0 else None
                if not current_head or current_head != row.get("head"):
                    _fail(
                        row,
                        "branch_delete",
                        "branch head changed: expected "
                        f"{row.get('head') or '<unknown>'}, found "
                        f"{current_head or '<unknown>'}",
                    )
                    _persist(project, state, document)
                    continue
                deleted = git_run(project.root, "branch", "-d", branch)
                if deleted.returncode:
                    _fail(
                        row,
                        "branch_delete",
                        (deleted.stderr or deleted.stdout or "git branch -d failed").strip(),
                    )
                    _persist(project, state, document)
                    continue
                present, error = _branch_exists(git_run, project.root, branch)
                if error or present is not False:
                    _fail(
                        row,
                        "branch_delete",
                        error or "branch still exists after deletion",
                    )
                    _persist(project, state, document)
                    continue
            _complete(row, "branch_delete")
        else:
            _complete(row, "branch_delete")
        row["action"] = "keep"
        row["reason"] = "REMOVED"
        row["pending_step"] = "prune"
        _persist(project, state, document)

    prune_ready = [
        row for row in rows
        if row.get("verified_absent")
        and _step(row, "worktree_remove").get("state") == "completed"
        and _step(row, "branch_delete").get("state") == "completed"
        and _step(row, "prune").get("state") != "completed"
    ]
    if prune_ready:
        prune = git_run(project.root, "worktree", "prune")
        if prune.returncode:
            document["prune_error"] = (
                prune.stderr or prune.stdout or "git worktree prune failed"
            ).strip()
            for row in prune_ready:
                _step(row, "prune")
                row["pending_step"] = "prune"
                row["reason"] = "PRUNE_FAILED"
        else:
            document["prune_error"] = None
            for row in prune_ready:
                _complete(row, "prune")
                if row.get("branch"):
                    row["reason"] = "REMOVED"
        _persist(project, state, document)

    if not document["prune_error"]:
        completed = {
            row["path"] for row in rows
            if row.get("verified_absent")
            and not row.get("pending_step")
            and row.get("pending_step") != "prune"
        }
        document["applied"].extend(
            row for row in rows
            if row["path"] in completed
            and row["path"] not in {item.get("path") for item in document["applied"]}
        )

    return _persist(project, state, document)
