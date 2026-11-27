"""Work-owned build resource registration and recoverable cleanup."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .build_policy import remove_tree
from .resource_registry import (
    ResourceOwner,
    ResourceRegistry,
    ResourceRegistryError,
)
from .core import WorkflowError, read


def _work_uid(state):
    value = state.get("work_uuid") or state.get("work_uid")
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError("RESOURCE_WORK_UID_REQUIRED")
    return value


def _owner(state, context):
    work_uid = _work_uid(state)
    context_owner = context.get("owner") or {}
    session_uid = (
        context_owner.get("session_uid")
        or state.get("controller_binding_uid")
        or state.get("binding_uid")
        or "controller:" + work_uid
    )
    generation = (
        context_owner.get("generation")
        or state.get("controller_generation")
        or 1
    )
    return ResourceOwner(work_uid, str(session_uid), int(generation))


def _request_uid(project, state, task_id, context, disposition):
    identity = {
        "schema": 1,
        "project": str(Path(project.root).resolve()),
        "work_uid": _work_uid(state),
        "run": state.get("id"),
        "task": task_id,
        "root": str(Path(context["root"]).resolve()),
        "mode": context.get("mode"),
        "provenance": context.get("provenance"),
        "disposition": disposition,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return "build-" + digest[:40]


def register_build_resource(project, state, task_id, context):
    """Persist one work-owned build-root reservation before creation."""
    root = context.get("root")
    if not root:
        return None
    mode = context.get("mode")
    if mode == "isolated":
        disposition = "delete"
    elif mode == "reuse-exclusive":
        disposition = "release"
    else:
        raise WorkflowError("BUILD_RESOURCE_MODE_INVALID " + str(mode))

    registry = ResourceRegistry(project.root)
    owner = _owner(state, context)
    canonical = registry._canonical_path(str(root))
    try:
        existing = next(
            (
                row for row in registry.list_for_work(owner.work_uid)
                if row["kind"] == "build_root"
                and row["identity"] == canonical
            ),
            None,
        )
        if existing is not None:
            if existing["disposition"] != disposition:
                raise WorkflowError("BUILD_RESOURCE_POLICY_CHANGED " + canonical)
            return registry.get(existing["request_uid"])
        return registry.reserve(
            _request_uid(project, state, task_id, context, disposition),
            owner,
            [{
                "kind": "build_root",
                "identity": canonical,
                "disposition": disposition,
            }],
        )
    except ResourceRegistryError as exc:
        raise WorkflowError("BUILD_RESOURCE_REGISTER_FAILED " + str(exc)) from exc


def _controlled_build_path(project, raw):
    base = (Path(project.root) / ".gsp" / "build").resolve()
    try:
        path = Path(raw).resolve(strict=False)
        path.relative_to(base)
    except (OSError, ValueError):
        return None
    return None if path == base else path


def _legacy_build_imports(project, state, indexed):
    """Read only this run's registered receipts; never enumerate old runs."""
    found = {}
    expected = (Path(project.root) / '.gsp/build' / str(state.get('number')) / state['id']).resolve()
    evidence_root = (Path(project.root) / '.gsp/runs' / state['id'] / 'evidence').resolve()
    for relative in state.get('evidence') or []:
        path = (Path(project.root) / relative).resolve()
        if not path.is_relative_to(evidence_root) or path.is_symlink() or not path.is_file():
            continue
        receipt = read(path)
        context = receipt.get('build_context') or {}
        raw = context.get('root')
        # Legacy isolated *main* roots have a deterministic work-owned location.
        # Other paths require explicit migration proof, not a guessed owner.
        if (context.get('mode') != 'isolated' or not isinstance(raw, str)
                or (Path(raw).resolve() != expected)
                or (context.get('owner') or {}).get('run') != state['id']):
            continue
        key = os.path.normcase(str(expected))
        if key not in indexed and expected.exists():
            found[key] = (receipt.get('task') or 'all', context)
    return list(found.values())


def _workspace_disposed(project, state, record):
    path = Path(record['identity']).resolve()
    if path.exists():
        return False
    run = Path(project.root) / '.gsp/runs' / state['id']
    dispatch_file = run / 'sessions/dispatch.json'
    if dispatch_file.is_file():
        for row in read(dispatch_file).get('dispatches', []):
            proof = row.get('worktree_disposition') or {}
            if (row.get('resource_request_uid') == record['request_uid']
                    and row.get('worktree_state') == 'removed'
                    and proof.get('action') == 'remove' and proof.get('state') == 'completed'
                    and proof.get('verified_absent') is True and proof.get('path')
                    and Path(proof['path']).resolve() == path):
                return True
    topology_file = run / 'topology-clean.json'
    if topology_file.is_file():
        proof = read(topology_file)
        return (proof.get('run') == state['id'] and proof.get('apply') is True
                and any(row.get('path') and Path(row['path']).resolve() == path
                        and row.get('verified_absent') is True for row in proof.get('applied', [])))
    return False


def cleanup_work_resources(project, state, dry_run=True):
    """Preview or continue cleanup from persisted work resource records."""
    work_uid = _work_uid(state)
    registry = ResourceRegistry(project.root)
    try:
        records = registry.list_for_work(work_uid)
    except ResourceRegistryError as exc:
        raise WorkflowError("RESOURCE_CLEANUP_LIST_FAILED " + str(exc)) from exc

    indexed = {row['identity'] for row in records if row['kind'] == 'build_root'}
    imports = _legacy_build_imports(project, state, indexed)
    import_preview = []
    for task_id, context in imports:
        if dry_run:
            import_preview.append({'kind': 'build_root', 'path': context['root'],
                                   'disposition': 'delete', 'state': 'would-register-and-delete'})
        elif not state.get('inflight') and not any(row['kind'] == 'slot' for row in records):
            register_build_resource(project, state, task_id, context)
    if imports and not dry_run:
        records = registry.list_for_work(work_uid)
    execution_pending = bool(state.get('inflight')) or any(row['kind'] == 'slot' for row in records)
    items = []
    for record in records:
        item = {
            "request_uid": record["request_uid"],
            "kind": record["kind"],
            "path": record["identity"],
            "disposition": record["disposition"],
        }
        if execution_pending:
            item.update({'state': 'pending', 'reason': 'EXECUTION_UNKNOWN'})
            items.append(item)
            continue
        try:
            if (record["kind"] == "build_root"
                    and (Path(record["identity"]) / ".gsp-build-lease.json").exists()):
                item.update({
                    'state': 'pending',
                    'reason': 'BUILD_LEASE_ACTIVE_OR_UNKNOWN',
                })
            elif record["disposition"] == "archive":
                item.update({
                    "state": "pending-host-action",
                    "pending_host_action": {
                        "schema": 1,
                        "kind": (
                            "worktree-archive"
                            if record["kind"] == "path"
                            else "resource-archive"
                        ),
                        "state": "pending",
                        "request_uid": record["request_uid"],
                        "path": record["identity"],
                    },
                })
            elif record["disposition"] == "release":
                if dry_run:
                    item["state"] = "would-release"
                else:
                    item["state"] = "released"
            elif record["kind"] != "build_root":
                if record['kind'] == 'path' and _workspace_disposed(project, state, record):
                    item['state'] = 'would-release-disposed' if dry_run else 'already-absent'
                else:
                    item.update({'state': 'pending', 'reason': 'WORKSPACE_LIFECYCLE_REQUIRED'})
            else:
                controlled = _controlled_build_path(project, record["identity"])
                if controlled is None:
                    item.update({
                        "state": "pending",
                        "reason": "UNCONTROLLED_BUILD_PATH",
                    })
                elif (controlled / '.gsp-build-lease.json').exists():
                    item.update({'state': 'pending', 'reason': 'BUILD_LEASE_ACTIVE_OR_UNKNOWN'})
                elif not controlled.exists():
                    if dry_run:
                        item["state"] = "would-release-absent"
                    else:
                        item["state"] = "already-absent"
                elif dry_run:
                    item["state"] = "would-delete"
                else:
                    remove_tree(controlled, project.root)
                    if controlled.exists():
                        raise OSError('resource still exists after deletion')
                    item.update({'state': 'deleted', 'verified_absent': True})
        except (OSError, ResourceRegistryError) as exc:
            item.update({
                "state": "pending",
                "reason": "CLEANUP_FAILED",
                "error": str(exc),
            })
        items.append(item)

    # A request can reserve several resources atomically. Do not release its
    # other claims while one resource is still pending or awaiting the host.
    if not dry_run:
        for request_uid in dict.fromkeys(row['request_uid'] for row in records):
            group = [item for item in items if item['request_uid'] == request_uid]
            if any(item['state'].startswith('pending') for item in group):
                continue
            owner = next(row['owner'] for row in records if row['request_uid'] == request_uid)
            try:
                registry.release(request_uid, owner)
            except ResourceRegistryError as exc:
                for item in group:
                    item.update({'state': 'pending', 'reason': 'RESOURCE_RELEASE_FAILED', 'error': str(exc)})

    items.extend(import_preview)
    # An unresolved execution blocks even when no resource rows were registered.
    if execution_pending and not items:
        items.append({'kind': 'execution', 'state': 'pending', 'reason': 'EXECUTION_UNKNOWN'})
    pending = sum(item["state"].startswith("pending") for item in items)
    preview = sum(item["state"].startswith("would-") for item in items)
    return {
        "schema": 1,
        "work_uid": work_uid,
        "dry_run": bool(dry_run),
        "items": items,
        "summary": {
            "selected": len(items),
            "pending": pending,
            "preview": preview,
            "released": sum(item["state"] in {
                "released", "already-absent", "deleted"
            } for item in items),
        },
        "complete": not any(
            item["state"].startswith(("pending", "would-"))
            for item in items
        ),
    }
