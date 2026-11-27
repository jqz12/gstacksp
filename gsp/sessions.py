"""Task-scoped cross-session registry, leases and document handoffs."""
import copy
import json
from pathlib import Path, PurePosixPath
import os
import re
import shutil
import uuid

from .identity_store import IdentityStore, IdentityStoreError
from .operation_journal import begin_intent, project_write_logical_key
from .orchestration_types import RequestRef, SessionBinding, digest_value
from .core import (WorkflowError, atomic, inside, now, read, redact, sha)
from .project import (Project)
from .workflow_support import (request_action_uid, request_binding_context, request_report_action_context)
from .session_context import (
    CONTROLLER_KINDS,
    legacy_read_only_placeholder,
    binding_state,
    binding_with_state,
    canonical_binding_digest,
    load_binding,
    require_capability,
    require_decision_controller,
    require_row_capability,
    row_context,
)

ROLES = {"controller", "worker", "integration", "observer"}
WRITE_MODES = {"write", "read-only"}
REPORT_STATUSES = {"DONE", "BLOCKED", "CHANGE_REQUEST"}
ACTIVE = {"active", "waiting", "reported"}
SESSION_RE = re.compile(r"^S[0-9]{2,}$")


def _paths(project, state):
    run, doc = project.paths(state["id"])
    machine = run / "sessions"
    human = doc / "sessions"
    machine.mkdir(parents=True, exist_ok=True)
    human.mkdir(parents=True, exist_ok=True)
    (human / "history").mkdir(parents=True, exist_ok=True)
    return machine, human


def _default(state):
    return {"schema": 1, "task": state["id"], "number": state.get("number"),
            "controller_session": None, "next_session": 1,
            "workspace_leases": {}, "sessions": [], "tasks": {}, "events": [], "outbox": []}


def load_registry(project, state, *, create=True):
    machine = (_paths(project, state)[0] if create else
               project.paths(state['id'])[0] / 'sessions')
    file = machine / "index.json"
    if not create and not file.is_file():
        registry = _default(state)
    else:
        registry = _default(state) if not file.exists() else read(file)
    if registry.get("task") != state["id"]:
        raise WorkflowError("SESSION_REGISTRY_TASK_MISMATCH")
    for key, default in (("schema", 1), ("controller_session", None),
                         ("next_session", 1), ("workspace_leases", {}),
                         ("sessions", []), ("tasks", {}), ("events", []), ("outbox", [])):
        registry.setdefault(key, default)
    return registry


def _save(project, state, registry):
    machine, human = _paths(project, state)
    atomic(machine / "index.json", registry)
    lines = [
        f"# {state.get('number') or state['id']} 会话登记", "",
        f"- 任务：{state['id']}",
        f"- 当前 controller：{registry.get('controller_session') or 'NONE'}",
        f"- 下一个会话号：S{int(registry.get('next_session', 1)):02d}", "",
        "| 会话 | 角色 | 宿主 | T 任务 | 写入 | worktree | 状态 | 声明路径 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in registry.get("sessions", []):
        paths = ", ".join(row.get("claimed_paths") or []) or "—"
        lines.append("| {id} | {role} | {host} | {task} | {mode} | {worktree} | {status} | {paths} |".format(
            id=row["id"], role=row["role"], host=row.get("host") or "unknown",
            task=row.get("task") or "—", mode=row.get("write_mode") or "read-only",
            worktree=row.get("worktree") or "—", status=row.get("status") or "unknown",
            paths=paths.replace("|", "\\|")))
    if registry.get("events"):
        lines.extend(["", "## 最近事件", ""])
        lines.extend(f"- {event.get('at')} {event.get('event')} {event.get('session') or '—'} {event.get('detail') or ''}"
                     for event in registry["events"][-20:])
    atomic(human / "index.md", "\n".join(lines) + "\n")


def _event(registry, name, session=None, detail=""):
    registry.setdefault("events", []).append({
        "id": uuid.uuid4().hex, "at": now(), "event": name,
        "session": session, "detail": redact(detail),
    })
    registry["events"] = registry["events"][-200:]


def _find(registry, token):
    token = str(token or "").strip()
    if not token:
        raise WorkflowError("SESSION_ID_REQUIRED")
    rows = [row for row in registry.get("sessions", [])
            if row.get("id") == token or row.get("thread_id") == token
            or row.get("host_session_id") == token
            or (row.get("session_ref") or {}).get("canonical_id") == token
            or (row.get("session_ref") or {}).get("uid") == token]
    if len(rows) != 1:
        raise WorkflowError("SESSION_NOT_FOUND " + token)
    return rows[0]


def _allocate(registry):
    used = {row.get("id") for row in registry.get("sessions", [])}
    number = int(registry.get("next_session", 1))
    while f"S{number:02d}" in used:
        number += 1
    registry["next_session"] = number + 1
    return f"S{number:02d}"


def _session_ref(project, state, args, session_id, workspace, paths, binding):
    if binding is not None:
        if (
            binding.session is None
            or binding.session.kind != "session"
            or binding.session.project_uid != IdentityStore(project.root).project_uid()
        ):
            raise WorkflowError("SESSION_IDENTITY_MISMATCH")
        return binding.session.to_dict()
    store = IdentityStore(project.root)
    logical_key = f"session-register:{state['work_uuid']}:{session_id}"
    request = RequestRef(
        request_uid=digest_value({
            "project_uid": store.project_uid(),
            "logical_key": logical_key,
            "operation_kind": "session-register",
        }),
        logical_key=logical_key,
        input_digest=digest_value({
            "role": args.role,
            "host": args.host or "unknown",
            "thread": args.thread,
            "workspace": str(workspace),
            "paths": list(paths),
            "write_mode": args.write_mode,
        }),
        project_uid=store.project_uid(),
        work_uid=state["work_uuid"],
        attempt_uid=None,
        operation_kind="session-register",
    )
    try:
        return store.allocate("session", None, request).to_dict()
    except IdentityStoreError as exc:
        raise WorkflowError("SESSION_IDENTITY_ALLOCATION_FAILED " + str(exc)) from exc


def _workspace(project, state, args, role):
    raw = getattr(args, "worktree", None)
    if not raw:
        topology = state.get("git_topology") or {}
        raw = (topology.get("control_root") if role == "controller" else
               topology.get("workspace_root") or topology.get("control_root")) or str(project.root)
    path = Path(raw).resolve()
    if not inside(project.root, path):
        from .worktree_adapters import WorktreeAdapter, WorktreeAdapterError
        try:
            inspection = WorktreeAdapter(
                project.root,
                expected_project_uid=(
                    state.get("project_id") or project.identity().get("id")
                ),
            ).inspect(path)
        except WorktreeAdapterError as exc:
            raise WorkflowError(
                "SESSION_WORKSPACE_ESCAPE " + str(path) + " " + str(exc)
            ) from exc
        if inspection.get("path") != str(path):
            raise WorkflowError("SESSION_WORKSPACE_ESCAPE " + str(path))
    return path


def _normalize_paths(workspace, values):
    result = []
    for raw in values or []:
        value = str(raw or "").strip().replace("\\", "/")
        if not value:
            continue
        candidate = Path(value)
        if candidate.is_absolute():
            try:
                value = candidate.resolve().relative_to(Path(workspace).resolve()).as_posix()
            except ValueError as exc:
                raise WorkflowError("SESSION_PATH_ESCAPE " + value) from exc
        else:
            value = PurePosixPath(value).as_posix()
        value = value.strip("/") or "."
        if (value == ".." or value.startswith("../") or "/../" in value or value.endswith("/..")
                or "\x00" in value):
            raise WorkflowError("SESSION_PATH_ESCAPE " + value)
        if value not in result:
            result.append(value)
    return result


def _overlap(left, right):
    return left == "." or right == "." or left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _key(path):
    return os.path.normcase(str(Path(path).resolve()))


def _lease_owner(registry, workspace):
    owner_id = registry.get("workspace_leases", {}).get(_key(workspace))
    return next((row for row in registry.get("sessions", [])
                 if row.get("id") == owner_id and row.get("status") in ACTIVE
                 and row.get("write_mode") == "write"), None)


def _conflicts(registry, workspace, paths, exclude=None):
    key = _key(workspace)
    conflicts = []
    for row in registry.get("sessions", []):
        if row.get("id") == exclude or row.get("status") not in ACTIVE or row.get("write_mode") != "write":
            continue
        if legacy_read_only_placeholder(registry, row):
            continue
        if _key(row.get("worktree")) != key:
            continue
        for left in paths:
            for right in row.get("claimed_paths") or []:
                if _overlap(left, right):
                    conflicts.append((row["id"], left, right))
    return conflicts


def _task(project, state, value):
    return project.item(state, value) if value else None


def _persist_binding_handles(project, *bindings):
    for binding in bindings:
        payload = read(project.root / binding.handle_path)
        if not isinstance(payload, dict):
            raise WorkflowError("SESSION_CONTEXT_INVALID")
        expected = binding.to_dict()
        if payload.get("binding") != expected:
            payload["binding"] = expected
            atomic(project.root / binding.handle_path, payload)


def _binding_from_handle_unchecked(project, value):
    """Load immutable binding facts without trusting the mutable handle state."""
    path = Path(value)
    if not path.is_absolute():
        path = project.root / path
    payload = read(path)
    raw = payload.get("binding") if isinstance(payload, dict) else None
    try:
        binding = SessionBinding.from_dict(raw)
    except (TypeError, ValueError, KeyError) as exc:
        raise WorkflowError("SESSION_CONTEXT_INVALID") from exc
    try:
        authoritative = IdentityStore(project.root).read_session_binding(
            binding.binding_uid
        )
    except IdentityStoreError as exc:
        raise WorkflowError(str(exc)) from exc
    if canonical_binding_digest(binding) != authoritative:
        raise WorkflowError("SESSION_CONTEXT_TAMPERED")
    return binding


def _transition_key(task_id, operation, old_uid, new_uid):
    return digest_value({
        "task": task_id,
        "operation": operation,
        "old_binding_uid": old_uid,
        "new_binding_uid": new_uid,
    })


def _transition_intent_path(project, key):
    return (
        project.root / ".gsp" / "orchestration" /
        "controller-transition-intents" / (key + ".json")
    )


def _load_transition_intent(project, task_id, operation, old_uid, new_uid):
    key = _transition_key(task_id, operation, old_uid, new_uid)
    path = _transition_intent_path(project, key)
    if not path.exists():
        return None
    envelope = read(path)
    payload = envelope.get("payload") if isinstance(envelope, dict) else None
    if (
        envelope.get("schema") != 1
        or envelope.get("payload_digest") != digest_value(payload)
        or not isinstance(payload, dict)
        or payload.get("task") != task_id
        or payload.get("operation") != operation
        or payload.get("old_binding", {}).get("binding_uid") != old_uid
        or payload.get("new_binding", {}).get("binding_uid") != new_uid
    ):
        raise WorkflowError("CONTROLLER_TRANSITION_INTENT_INVALID")
    return envelope


def _save_transition_intent(project, envelope):
    payload = envelope["payload"]
    key = _transition_key(
        payload["task"], payload["operation"],
        payload["old_binding"]["binding_uid"],
        payload["new_binding"]["binding_uid"],
    )
    path = _transition_intent_path(project, key)
    if path.exists():
        existing = read(path)
        if (
            not isinstance(existing, dict)
            or existing.get("payload_digest") != envelope.get("payload_digest")
            or existing.get("payload") != payload
        ):
            raise WorkflowError("CONTROLLER_TRANSITION_INTENT_CHANGED")
        operation_uid = existing.get("operation_uid")
        if envelope.get("operation_uid") and operation_uid != envelope["operation_uid"]:
            raise WorkflowError("CONTROLLER_TRANSITION_INTENT_CHANGED")
        envelope = existing
    else:
        atomic(path, envelope)
    return envelope


def _new_transition_intent(
    project, state, operation, old_binding, new_binding,
    next_generation, old_session, new_session, reason, new_record=None,
):
    payload = {
        "task": state["id"],
        "work_uid": state["work_uuid"],
        "operation": operation,
        "old_session": old_session,
        "new_session": new_session,
        "old_binding": old_binding.to_dict(),
        "new_binding": new_binding.to_dict(),
        "next_generation": next_generation,
        "reason": redact(reason),
    }
    if new_record is not None:
        payload["new_record"] = new_record
    envelope = {
        "schema": 1,
        "payload": payload,
        "payload_digest": digest_value(payload),
    }
    return _save_transition_intent(project, envelope)


def _transition_request(project, envelope):
    payload = envelope["payload"]
    input_digest = envelope["payload_digest"]
    old_binding = payload.get("old_binding")
    if old_binding is None:
        if payload.get("operation") != "establish-controller":
            raise WorkflowError("CONTROLLER_TRANSITION_INTENT_INVALID")
        old_uid = None
        old_generation = None
        old_cancel_generation = None
    elif isinstance(old_binding, dict):
        old_uid = old_binding.get("binding_uid")
        old_generation = old_binding.get("controller_generation")
        old_cancel_generation = old_binding.get("cancel_generation")
    else:
        raise WorkflowError("CONTROLLER_TRANSITION_INTENT_INVALID")
    key = _transition_key(
        payload["task"], payload["operation"],
        old_uid,
        payload["new_binding"]["binding_uid"],
    )
    logical_key = "controller-transition:" + key
    project_uid = IdentityStore(project.root).project_uid()
    return RequestRef(
        request_uid=digest_value({
            "project_uid": project_uid,
            "logical_key": logical_key,
            "operation_kind": "controller-transition",
        }),
        logical_key=logical_key,
        input_digest=input_digest,
        project_uid=project_uid,
        work_uid=payload["work_uid"],
        attempt_uid=None,
        operation_kind="controller-transition",
        controller_generation=old_generation,
        cancel_generation=old_cancel_generation,
        contract_uid=None,
        contract_version=None,
        contract_digest=None,
    )


def _transition_binding(store, raw, work_uid):
    """Return the intent binding and its current authoritative state."""
    try:
        candidate = SessionBinding.from_dict(raw)
        authoritative_digest = store.read_session_binding(candidate.binding_uid)
        authoritative_state = store.read_session_state(candidate.binding_uid)
    except (TypeError, ValueError, KeyError, IdentityStoreError) as exc:
        raise WorkflowError("CONTROLLER_TRANSITION_INTENT_INVALID") from exc
    if (
        authoritative_digest is None
        or authoritative_state is None
        or canonical_binding_digest(candidate) != authoritative_digest
        or candidate.work_uid != work_uid
    ):
        raise WorkflowError("CONTROLLER_TRANSITION_INTENT_TAMPERED")
    return candidate, binding_with_state(candidate, authoritative_state)


def _authoritative_transition_binding(store, raw, work_uid):
    """Reconstruct one intent binding from immutable authority plus state."""
    return _transition_binding(store, raw, work_uid)[1]


def _validate_transition_intent(store, envelope, *, allow_transitioned):
    """Validate every intent fact needed before allocating an operation."""
    payload = envelope.get("payload") if isinstance(envelope, dict) else None
    if (
        not isinstance(payload, dict)
        or envelope.get("schema") != 1
        or envelope.get("payload_digest") != digest_value(payload)
        or not isinstance(payload.get("task"), str)
        or not payload["task"].strip()
        or not isinstance(payload.get("work_uid"), str)
        or not payload["work_uid"].strip()
        or not isinstance(payload.get("new_session"), str)
        or not payload["new_session"].strip()
        or type(payload.get("next_generation")) is not int
        or payload["next_generation"] < 1
    ):
        raise WorkflowError("CONTROLLER_TRANSITION_INTENT_INVALID")
    work_uid = payload["work_uid"]
    operation = payload.get("operation")
    old_raw = payload.get("old_binding")
    if old_raw is None:
        if operation != "establish-controller":
            raise WorkflowError("CONTROLLER_TRANSITION_INTENT_INVALID")
        old_candidate = None
        old_current = None
    else:
        if operation not in {"handoff", "takeover"}:
            raise WorkflowError("CONTROLLER_TRANSITION_INTENT_INVALID")
        old_candidate, old_current = _transition_binding(
            store, old_raw, work_uid,
        )
    new_candidate, new_current = _transition_binding(
        store, payload.get("new_binding"), work_uid,
    )
    require_capability(new_current, "write", work_uid)

    if old_raw is None:
        if (
            new_current.kind not in {"main", "main_chat"}
            or new_current.controller_generation != payload["next_generation"]
        ):
            raise WorkflowError("CONTROLLER_TRANSITION_INTENT_TAMPERED")
        if new_current.cancelled:
            raise WorkflowError("SESSION_BINDING_STALE")
    else:
        old_expected = binding_state(old_candidate)
        new_expected = binding_state(new_candidate)
        if payload["next_generation"] <= max(
            old_expected["controller_generation"],
            new_expected["controller_generation"],
        ):
            raise WorkflowError("CONTROLLER_TRANSITION_INTENT_TAMPERED")
        if not allow_transitioned:
            if old_current.cancelled or new_current.cancelled:
                raise WorkflowError("SESSION_BINDING_STALE")
            if (
                binding_state(old_current) != old_expected
                or binding_state(new_current) != new_expected
            ):
                raise WorkflowError("CONTROLLER_TRANSITION_INTENT_TAMPERED")
        else:
            old_applied = {
                "controller_generation": old_expected["controller_generation"],
                "cancel_generation": old_expected["cancel_generation"] + 1,
                "cancelled": True,
            }
            new_applied = {
                "controller_generation": payload["next_generation"],
                "cancel_generation": new_expected["cancel_generation"],
                "cancelled": False,
            }
            untouched = (
                not old_current.cancelled
                and binding_state(old_current) == old_expected
                and binding_state(new_current) == new_expected
            )
            applied = (
                binding_state(old_current) == old_applied
                and binding_state(new_current) == new_applied
            )
            if not untouched and not applied:
                raise WorkflowError("CONTROLLER_TRANSITION_INTENT_TAMPERED")
            if applied:
                try:
                    controller = store.read_work_controller(work_uid)
                except IdentityStoreError as exc:
                    raise WorkflowError(str(exc)) from exc
                if (
                    controller is None
                    or controller.get("binding_uid") != new_current.binding_uid
                    or controller.get("run_id") != payload["task"]
                ):
                    raise WorkflowError("CONTROLLER_TRANSITION_INTENT_TAMPERED")

    record = payload.get("new_record")
    if record is None:
        if old_raw is None:
            raise WorkflowError("CONTROLLER_TRANSITION_INTENT_TAMPERED")
    elif (
        not isinstance(record, dict)
        or record.get("id") != payload["new_session"]
        or record.get("role") != "controller"
        or record.get("host") != new_candidate.host
        or record.get("thread_id") != new_candidate.host_thread_id
        or record.get("binding_uid") != new_candidate.binding_uid
        or record.get("context") != new_candidate.handle_path
        or record.get("work_uid") != work_uid
        or record.get("write_mode") != "write"
        or not record.get("worktree")
        or record.get("status") != "active"
        or (
            record.get("context_binding") is not None
            and record.get("context_binding") != payload["new_binding"]
        )
        or (
            record.get("controller_generation") is not None
            and record.get("controller_generation")
            != new_candidate.controller_generation
        )
        or (
            record.get("cancel_generation") is not None
            and record.get("cancel_generation") != new_candidate.cancel_generation
        )
    ):
        raise WorkflowError("CONTROLLER_TRANSITION_INTENT_TAMPERED")
    return old_current, new_current


def _run_controller_transition(project, envelope, intent_path=None):
    store = IdentityStore(project.root)
    payload = envelope["payload"]
    # Pending intents can be replayed after a crash. Rebuild both bindings from
    # authoritative immutable facts and current mutable state before allocating
    # the operation or advancing generations. A changed payload cannot add a
    # capability, substitute a work item, or start from stale state.
    operation_uid = envelope.get("operation_uid")
    operation = None
    if operation_uid:
        operation = store.get_operation(operation_uid)
    old_authoritative, new_authoritative = _validate_transition_intent(
        store, envelope, allow_transitioned=operation is not None,
    )
    request = _transition_request(project, envelope)
    if operation_uid:
        if operation.request != request:
            raise WorkflowError("CONTROLLER_TRANSITION_INTENT_CHANGED")
    else:
        operation = begin_intent(store, request, envelope["payload_digest"])
        envelope["operation_uid"] = operation.operation_uid
        if intent_path is None:
            intent_path = _transition_intent_path(project, _transition_key(
                payload["task"], payload["operation"],
                payload["old_binding"]["binding_uid"]
                if payload.get("old_binding") is not None else None,
                payload["new_binding"]["binding_uid"],
            ))
        atomic(intent_path, envelope)
    if operation.phase == "failed":
        raise WorkflowError("OPERATION_FAILED")
    if old_authoritative is None:
        old_binding = None
        if (
            new_authoritative.kind not in {"main", "main_chat"}
            or new_authoritative.cancelled
            or new_authoritative.controller_generation
            != payload.get("next_generation")
        ):
            raise WorkflowError("CONTROLLER_TRANSITION_INTENT_TAMPERED")
        new_state = store.read_session_state(new_authoritative.binding_uid)
        if new_state is None:
            raise WorkflowError("CONTROLLER_TRANSITION_INTENT_TAMPERED")
        new_binding = binding_with_state(
            SessionBinding.from_dict(payload["new_binding"]), new_state,
        )
    else:
        try:
            transition = store.transition_controller_bindings(
                payload["old_binding"]["binding_uid"],
                payload["old_binding"]["controller_generation"],
                payload["new_binding"]["binding_uid"],
                payload["new_binding"]["controller_generation"],
                payload["next_generation"],
                work_uid=payload["work_uid"], run_id=payload["task"],
            )
        except IdentityStoreError as exc:
            raise WorkflowError(str(exc)) from exc
        old_binding = binding_with_state(
            SessionBinding.from_dict(payload["old_binding"]), transition["old"],
        )
        new_binding = binding_with_state(
            SessionBinding.from_dict(payload["new_binding"]), transition["new"],
        )
    result_ref = digest_value({
        "old": old_binding.to_dict() if old_binding is not None else None,
        "new": new_binding.to_dict(),
    })
    if operation.phase == "committed" and operation.result_ref != result_ref:
        raise WorkflowError("OPERATION_RESULT_CHANGED")
    if operation.phase != "committed":
        key = _transition_key(
            payload["task"], payload["operation"],
            old_binding.binding_uid if old_binding is not None else None,
            new_binding.binding_uid,
        )
        journal_path = (
            project.root / ".gsp" / "orchestration" /
            "controller-transition-journal" / (key + ".json")
        )
        try:
            store.prepare_operation(operation.operation_uid, journal_path, result_ref)
        except IdentityStoreError as exc:
            raise WorkflowError(str(exc)) from exc
        operation = store.mark_operation_written(operation.operation_uid)
    return envelope, old_binding, new_binding, operation.operation_uid, result_ref


def _finish_controller_transition(project, envelope, operation_uid, result_ref):
    try:
        return IdentityStore(project.root).finish_operation(operation_uid, result_ref)
    except IdentityStoreError as exc:
        raise WorkflowError(str(exc)) from exc


def _handoff_document_path(project, state, envelope):
    payload = envelope["payload"]
    _, human = _paths(project, state)
    key = _transition_key(
        payload["task"], payload["operation"],
        payload["old_binding"]["binding_uid"],
        payload["new_binding"]["binding_uid"],
    )
    return human / f"handoff-{payload['old_session']}-to-{payload['new_session']}-{key[:8]}.md"


def _write_handoff_document(project, state, envelope):
    payload = envelope["payload"]
    path = _handoff_document_path(project, state, envelope)
    if not path.exists():
        atomic(
            path,
            f"# Controller handoff {payload['old_session']} to {payload['new_session']}\n\n"
            f"- 时间：{now()}\n- 原因：{redact(payload['reason'])}\n",
        )
    return path


def _sync_controller_transition(
    project, state, envelope, old_binding, new_binding, operation_uid,
    result_ref,
):
    payload = envelope["payload"]
    registry = load_registry(project, state)
    if payload.get("old_binding") is None:
        if payload.get("operation") != "establish-controller":
            raise WorkflowError("CONTROLLER_TRANSITION_INTENT_INVALID")
        active = [
            row for row in registry.get("sessions", [])
            if row.get("role") == "controller"
            and row.get("status") in ACTIVE
        ]
        active_ids = {row.get("id") for row in active}
        if active and not (
            active_ids == {payload.get("new_session")}
            and registry.get("controller_session") == payload.get("new_session")
        ):
            raise WorkflowError("CONTROLLER_LEASE_REQUIRED")
        new = next(
            (row for row in registry["sessions"]
             if row.get("id") == payload["new_session"]),
            None,
        )
        if new is None:
            new_record = payload.get("new_record")
            if not isinstance(new_record, dict):
                raise WorkflowError("CONTROLLER_TRANSITION_INTENT_INVALID")
            new = dict(new_record)
            registry["sessions"].append(new)
        if (
            new.get("role") != "controller"
            or new.get("binding_uid") != new_binding.binding_uid
            or new.get("context") != new_binding.handle_path
            or new.get("work_uid") != new_binding.work_uid
            or new.get("write_mode") != "write"
            or not new.get("worktree")
        ):
            raise WorkflowError("CONTROLLER_TRANSITION_INTENT_TAMPERED")
        already_applied = registry.get("controller_session") == new["id"]
        _persist_binding_handles(project, new_binding)
        new["context_binding"] = new_binding.to_dict()
        new["binding_uid"] = new_binding.binding_uid
        new["controller_generation"] = new_binding.controller_generation
        new["cancel_generation"] = new_binding.cancel_generation
        new["role"] = "controller"
        new["status"] = "active"
        new["invalidated_by_takeover"] = False
        new["updated_at"] = now()
        registry["controller_session"] = new["id"]
        registry.setdefault("workspace_leases", {})[
            _key(new["worktree"])
        ] = new["id"]
        if not already_applied:
            _event(
                registry, "controller-established", new["id"],
                payload.get("reason") or "initial controller recovery",
            )
        _save(project, state, registry)
        return _finish_controller_transition(
            project, envelope, operation_uid, result_ref,
        )
    old = _find(registry, payload["old_session"])
    new = next(
        (row for row in registry["sessions"]
         if row.get("id") == payload["new_session"]),
        None,
    )
    if new is None:
        new_record = payload.get("new_record")
        if payload["operation"] != "takeover" or not isinstance(new_record, dict):
            raise WorkflowError("SESSION_NOT_FOUND " + payload["new_session"])
        new = new_record
        registry["sessions"].append(new)
    _persist_binding_handles(project, old_binding, new_binding)
    _sync_controller_rows(
        registry, old, new, old_binding, new_binding, payload["reason"],
        event_name=(
            "controller-takeover" if payload["operation"] == "takeover"
            else "controller-handoff"
        ),
    )
    if payload["operation"] == "handoff":
        _write_handoff_document(project, state, envelope)
    _save(project, state, registry)
    return _finish_controller_transition(
        project, envelope, operation_uid, result_ref,
    )


def _execute_controller_transition(project, state, envelope):
    envelope, old_binding, new_binding, operation_uid, result_ref = (
        _run_controller_transition(project, envelope)
    )
    _sync_controller_transition(
        project, state, envelope, old_binding, new_binding,
        operation_uid, result_ref,
    )
    return envelope, old_binding, new_binding


def _recover_controller_transitions(project, state):
    root = (
        project.root / ".gsp" / "orchestration" /
        "controller-transition-intents"
    )
    recovered = []
    if not root.is_dir():
        return recovered
    for path in sorted(root.glob("*.json")):
        envelope = read(path)
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("task") != state["id"]
            or payload.get("work_uid") != state.get("work_uuid")
        ):
            continue
        operation_uid = envelope.get("operation_uid")
        if not operation_uid:
            # A crash may happen after the durable intent is written but before
            # the operation row is allocated.  Run the same fail-closed
            # preflight through the normal transition path, then finish every
            # projection before reporting recovery.
            envelope, old_binding, new_binding, operation_uid, result_ref = (
                _run_controller_transition(project, envelope, path)
            )
            _sync_controller_transition(
                project, state, envelope, old_binding, new_binding,
                operation_uid, result_ref,
            )
            recovered.append(operation_uid)
            continue
        store = IdentityStore(project.root)
        operation = store.get_operation(operation_uid)
        if operation.phase == "failed":
            raise WorkflowError("OPERATION_FAILED")
        if operation.phase == "committed":
            # Completed intents stay on disk as audit history.  They must not
            # be replayed after later controller transitions advance the same
            # bindings again.
            continue
        try:
            envelope, old_binding, new_binding, operation_uid, result_ref = (
                _run_controller_transition(project, envelope, path)
            )
            _sync_controller_transition(
                project, state, envelope, old_binding, new_binding,
                operation_uid, result_ref,
            )
        except (WorkflowError, IdentityStoreError):
            raise
        recovered.append(operation_uid)
    return recovered


def _recover_controller_transition_for_handles(
    project, state, old_binding, new_binding, operation,
):
    envelope = _load_transition_intent(
        project, state["id"], operation,
        old_binding.binding_uid, new_binding.binding_uid,
    )
    if envelope is None:
        return False
    _recover_controller_transitions(project, state)
    return True


def recover_binding_handle(root, binding):
    """Replay one pending transition, then revalidate the handle once."""
    project = Project(root)
    states = {}
    for state_file in sorted((project.meta / "runs").glob("*/state.json")):
        try:
            state = read(state_file)
        except WorkflowError:
            continue
        if state.get("work_uuid"):
            states[state["work_uuid"]] = state
    state = states.get(binding.work_uid)
    if state is None:
        raise WorkflowError("SESSION_CONTEXT_TAMPERED handle state does not match authoritative binding")
    intents = (
        project.root / ".gsp" / "orchestration" /
        "controller-transition-intents"
    )
    pending = []
    for path in intents.glob("*.json") if intents.is_dir() else ():
        envelope = read(path)
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        if not isinstance(payload, dict):
            continue
        if binding.binding_uid not in {
            payload.get("old_binding", {}).get("binding_uid"),
            payload.get("new_binding", {}).get("binding_uid"),
        }:
            continue
        operation_uid = envelope.get("operation_uid")
        if operation_uid:
            try:
                operation = IdentityStore(project.root).get_operation(operation_uid)
            except IdentityStoreError as exc:
                raise WorkflowError(str(exc)) from exc
            if operation.phase == "committed":
                # Completed transitions remain as audit history. A handle that
                # disagrees with their authoritative state is tampering.
                continue
            if operation.phase == "failed":
                raise WorkflowError("OPERATION_FAILED")
        pending.append((path, envelope))
    if not pending:
        raise WorkflowError("SESSION_CONTEXT_TAMPERED handle state does not match authoritative binding")
    if len(pending) != 1:
        raise WorkflowError("SESSION_CONTEXT_TAMPERED multiple unfinished transitions")
    intent_path, envelope = pending[0]
    envelope, old_binding, new_binding, operation_uid, result_ref = (
        _run_controller_transition(project, envelope, intent_path)
    )
    _sync_controller_transition(
        project, state, envelope, old_binding, new_binding,
        operation_uid, result_ref,
    )
    return load_binding(project.root / binding.handle_path, _recover_once=True)


def resume_pending_controller(project, state, binding, *, preflight=False):
    """Finish a durable transition when the caller is its exact new binding."""
    if not isinstance(binding, SessionBinding):
        raise WorkflowError("SESSION_CONTEXT_REQUIRED")
    if binding.work_uid != state.get("work_uuid"):
        raise WorkflowError("WORK_UID_MISMATCH caller context belongs to another work")
    root = (
        project.root / ".gsp" / "orchestration" /
        "controller-transition-intents"
    )
    pending = []
    for path in sorted(root.glob("*.json")) if root.is_dir() else ():
        envelope = read(path)
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        if not isinstance(payload, dict):
            continue
        if (
            payload.get("task") != state.get("id")
            or payload.get("work_uid") != state.get("work_uuid")
        ):
            continue
        new_raw = payload.get("new_binding")
        if not isinstance(new_raw, dict):
            raise WorkflowError("CONTROLLER_TRANSITION_INTENT_INVALID")
        if new_raw.get("binding_uid") != binding.binding_uid:
            continue
        if (
            envelope.get("schema") != 1
            or envelope.get("payload_digest") != digest_value(payload)
            or new_raw.get("handle_path") != binding.handle_path
        ):
            raise WorkflowError("CONTROLLER_TRANSITION_INTENT_INVALID")
        operation_uid = envelope.get('operation_uid')
        if operation_uid:
            operation = IdentityStore(project.root).get_operation(operation_uid)
            if operation.request != _transition_request(project, envelope):
                raise WorkflowError('CONTROLLER_TRANSITION_INTENT_CHANGED')
            if operation.phase == 'committed':
                continue
            if operation.phase == 'failed':
                raise WorkflowError('OPERATION_FAILED')
        pending.append((path, envelope))
    if not pending:
        return None if preflight else binding
    if len(pending) != 1:
        raise WorkflowError("CONTROLLER_TRANSITION_INTENT_CHANGED")
    intent_path, envelope = pending[0]
    if preflight:
        # Initial publication may be interrupted before the session row exists.
        # Admit only the already authoritative owner of that exact pending
        # transition; this probe must not recover or write any projection.
        payload = envelope['payload']
        if payload.get('operation') != 'establish-controller':
            return None
        store = IdentityStore(project.root)
        old_binding, _ = _validate_transition_intent(
            store, envelope, allow_transitioned=False,
        )
        if old_binding is not None:
            return None
        require_decision_controller(store, binding, binding.work_uid)
        return binding
    envelope, old_binding, new_binding, operation_uid, result_ref = (
        _run_controller_transition(project, envelope, intent_path)
    )
    _sync_controller_transition(
        project, state, envelope, old_binding, new_binding,
        operation_uid, result_ref,
    )
    return load_binding(
        project.root / binding.handle_path, _recover_once=True,
    )


def _sync_controller_rows(registry, old, new, old_binding, new_binding, reason,
                          event_name="controller-handoff"):
    if new.get("write_mode") != "write":
        require_capability(new_binding, "write", new_binding.work_uid)
    for row, binding in ((old, old_binding), (new, new_binding)):
        row["context_binding"] = binding.to_dict()
        row["binding_uid"] = binding.binding_uid
        row["controller_generation"] = binding.controller_generation
        row["cancel_generation"] = binding.cancel_generation
        row["updated_at"] = now()
    old_key = _key(old["worktree"])
    if registry.setdefault("workspace_leases", {}).get(old_key) == old["id"]:
        registry["workspace_leases"].pop(old_key, None)
    old["status"] = "handed-off"
    old["invalidated_by_takeover"] = True
    new["role"] = "controller"
    new["status"] = "active"
    new["invalidated_by_takeover"] = False
    if new.get("write_mode") != "write":
        new["write_mode"] = "write"
        new["worktree"] = old["worktree"]
        new["claimed_paths"] = list(old.get("claimed_paths") or ["."])
    registry["workspace_leases"][_key(new["worktree"])] = new["id"]
    already_applied = (
        registry.get("controller_session") == new["id"]
        and old.get("status") == "handed-off"
        and new.get("role") == "controller"
    )
    registry["controller_session"] = new["id"]
    if not already_applied:
        _event(registry, event_name, new["id"], reason)


def register_session(project, state, args, *, preflight=False):
    if preflight and (state.get('git_topology') or {}).get('intent') != 'cross-session':
        raise WorkflowError('SESSION_MODE_REQUIRED use --intent cross-session')
    registry = load_registry(project, state, create=False)
    role = args.role
    mode = args.write_mode
    workspace_group_mode = getattr(args, "workspace_group", None)
    if workspace_group_mode not in {None, "parent"}:
        raise WorkflowError("WORKSPACE_GROUP_MODE_INVALID")
    group_registration = workspace_group_mode == "parent"
    if role not in ROLES:
        raise WorkflowError("SESSION_ROLE_INVALID")
    if mode not in WRITE_MODES:
        raise WorkflowError("SESSION_WRITE_MODE_INVALID")
    if role == "observer" and mode == "write":
        raise WorkflowError("OBSERVER_CANNOT_WRITE")
    if group_registration and (
        role not in {"worker", "integration"} or mode != "write"
    ):
        raise WorkflowError("WORKSPACE_GROUP_WORKER_REQUIRED")
    context_path = str(getattr(args, "context", None) or "").strip()
    if not context_path:
        mode = "read-only"
    task = _task(project, state, getattr(args, "task", None))
    if role in {"worker", "integration"} and task is None:
        raise WorkflowError("SESSION_TASK_REQUIRED")
    session_id = str(getattr(args, "session", None) or "").strip() or _allocate(registry)
    if not SESSION_RE.fullmatch(session_id):
        raise WorkflowError("SESSION_ID_INVALID use S01, S02 ...")
    if any(row.get("id") == session_id for row in registry["sessions"]):
        raise WorkflowError("SESSION_ID_EXISTS " + session_id)
    if args.thread and any(row.get("thread_id") == args.thread for row in registry["sessions"]):
        raise WorkflowError("SESSION_THREAD_EXISTS " + args.thread)
    active_controllers = [row for row in registry["sessions"]
                          if row.get("role") == "controller" and row.get("status") in ACTIVE
                          and not legacy_read_only_placeholder(registry, row)]
    old_controller = next((row for row in active_controllers
                           if row.get("id") == registry.get("controller_session")),
                          active_controllers[0] if active_controllers else None)
    # Validate every input BEFORE mutating any controller or handle state, so a
    # rejected takeover cannot partially invalidate the current controller.
    takeover_pending = False
    if role == "controller" and context_path and old_controller:
        if not args.takeover:
            raise WorkflowError("CONTROLLER_LEASE_HELD " + old_controller["id"])
        if not str(args.reason or "").strip():
            raise WorkflowError("TAKEOVER_REASON_REQUIRED")
        takeover_pending = True
    workspace = _workspace(project, state, args, role)
    paths = _normalize_paths(workspace, getattr(args, "path", None))
    if mode == "write" and not paths:
        raise WorkflowError("SESSION_PATHS_REQUIRED declare --path before writing")
    # A controller takeover inherits the workspace lease; otherwise a second
    # writer in the same workspace is rejected.
    lease = _lease_owner(registry, workspace)
    if (
        mode == "write"
        and lease
        and not group_registration
        and (not takeover_pending or lease["id"] != old_controller["id"])
    ):
        raise WorkflowError("WORKSPACE_WRITE_LEASE_HELD " + lease["id"])
    conflict = _conflicts(
        registry,
        workspace,
        paths,
        exclude=(
            old_controller.get("id")
            if takeover_pending
            else None
        ),
    ) if not group_registration else []
    if conflict:
        raise WorkflowError("SESSION_PATH_CONFLICT " + conflict[0][1])
    binding = None
    # A context-free registration cannot be a real writer: it is recorded as a
    # legacy read-only session (no workspace lease), and any actual edit is
    # rejected later by _write_session / the hook with SESSION_CONTEXT_REQUIRED.
    if context_path:
        candidate = Path(context_path)
        if not candidate.is_absolute():
            candidate = project.root / candidate
        def authorize_registration(binding, store):
            if store.root.resolve() != project.root.resolve():
                raise WorkflowError('SESSION_PROJECT_MISMATCH')
            if binding.host != (args.host or "unknown"):
                raise WorkflowError("SESSION_HOST_MISMATCH")
            if binding.host_thread_id and args.thread and binding.host_thread_id != args.thread:
                raise WorkflowError("SESSION_THREAD_MISMATCH")
            if binding.work_uid != state["work_uuid"]:
                raise WorkflowError("WORK_UID_MISMATCH context belongs to another work")
            required = ('write' if mode == 'write' else
                        'report' if role in {'worker', 'integration'} else None)
            if required:
                require_capability(binding, required, state['work_uuid'])
            elif binding.cancelled:
                raise WorkflowError("SESSION_BINDING_STALE cancelled binding")
            if role == "controller" and binding.kind not in CONTROLLER_KINDS:
                raise WorkflowError("SESSION_CONTROLLER_CONTEXT_REQUIRED")
            if role in {"worker", "integration"} and binding.kind not in {
                "child", "integration", "independent_worker", "helper",
                "native_executor",
            }:
                raise WorkflowError("SESSION_CHILD_CONTEXT_REQUIRED")

        binding = load_binding(candidate, before_recovery=authorize_registration)
    elif takeover_pending:
        raise WorkflowError("SESSION_CONTEXT_REQUIRED")

    if group_registration:
        if takeover_pending:
            raise WorkflowError("WORKSPACE_GROUP_TAKEOVER_FORBIDDEN")
        if binding is None:
            raise WorkflowError("SESSION_CONTEXT_REQUIRED")
        controller_id = registry.get("controller_session")
        controller = next(
            (
                row for row in registry.get("sessions", [])
                if row.get("id") == controller_id
            ),
            None,
        )
        if (
            controller is None
            or controller.get("role") != "controller"
            or controller.get("status") not in ACTIVE
            or not controller.get("binding_uid")
            or not controller.get("worktree")
        ):
            raise WorkflowError("CONTROLLER_LEASE_REQUIRED")
        if Path(workspace).resolve() != Path(controller["worktree"]).resolve():
            raise WorkflowError("WORKSPACE_GROUP_PARENT_STEM_MISMATCH")

        from .dispatch import _save_dispatch, load_dispatch
        from .resource_registry import ResourceRegistry
        from .workspace_groups import (
            SessionWorkspaceLeaseAuthority,
            build_workspace_group,
            claim_workspace_group,
        )

        dispatch_data = load_dispatch(project, state)
        dispatch_row = next(
            (
                row for row in reversed(dispatch_data.get("dispatches", []))
                if row.get("task") == task["id"]
                and row.get("status") not in {"superseded", "cleaned"}
            ),
            None,
        )
        if dispatch_row is None:
            raise WorkflowError("WORKSPACE_GROUP_DISPATCH_REQUIRED " + task["id"])
        if dispatch_row.get("workspace_group_mode") != "parent":
            raise WorkflowError("WORKSPACE_GROUP_DISPATCH_REQUIRED " + task["id"])
        group = build_workspace_group(
            project.root,
            project.root,
            controller["worktree"],
            project_uid=IdentityStore(project.root).project_uid(),
            work_uid=state["work_uuid"],
            parent_binding_uid=controller["binding_uid"],
            controller_binding_uid=controller["binding_uid"],
            controller_generation=int(
                controller.get("controller_generation") or 1
            ),
            cancel_generation=int(
                controller.get("cancel_generation") or 1
            ),
            source_writer_binding_uid=binding.binding_uid,
        )
        if dispatch_row.get("branch") not in {None, group.branch}:
            raise WorkflowError("WORKSPACE_GROUP_BRANCH_MISMATCH")
        session_ref = _session_ref(
            project, state, args, session_id, workspace, paths, binding,
        )
        record = {
            "id": session_id,
            "role": role,
            "host": args.host or "unknown",
            "thread_id": args.thread,
            "task": task["id"],
            "branch": group.branch,
            "worktree": str(workspace),
            "write_mode": mode,
            "claimed_paths": paths,
            "status": "active",
            "started_at": now(),
            "updated_at": now(),
            "context": binding.handle_path,
            "context_mode": "bound",
            "binding_uid": binding.binding_uid,
            "work_uid": binding.work_uid,
            "controller_generation": binding.controller_generation,
            "cancel_generation": binding.cancel_generation,
            "context_binding": binding.to_dict(),
            "session_ref": session_ref,
        }
        if preflight:
            SessionWorkspaceLeaseAuthority(
                project.root, state["id"]
            ).preflight_claim(
                group,
                task_uid=task["id"],
                source_writer_session_id=session_id,
                source_writer_binding_uid=binding.binding_uid,
                source_writer_generation=binding.controller_generation,
                source_writer_cancel_generation=binding.cancel_generation,
                session_record=record,
            )
            return {
                "binding_uid": binding.binding_uid,
                "group_uid": group.group_uid,
                "preflight": True,
            }
        receipt = claim_workspace_group(
            group,
            project_uid=group.project_uid,
            work_uid=group.work_uid,
            control_root=group.control_root,
            workspace_root=group.workspace_root,
            branch=group.branch,
            task_uid=task["id"],
            contract_digest=dispatch_row["content_digest"],
            source_writer_session_id=session_id,
            source_writer_binding_uid=binding.binding_uid,
            source_writer_generation=binding.controller_generation,
            source_writer_cancel_generation=binding.cancel_generation,
            resource_registry=ResourceRegistry(project.root),
            lease_authority=SessionWorkspaceLeaseAuthority(
                project.root, state["id"]
            ),
            requested_mode="sequential",
            unknown_inflight=state.get("inflight") is not None,
            session_record=record,
        )
        dispatch_row.update({
            "status": (
                "adapter-ready"
                if dispatch_row.get("dispatch_mode") in {"auto", "adapter"}
                and dispatch_row.get("platform") != "manual"
                else "manual-required"
            ),
            "workspace_group_state": "bound",
            "workspace_group_resource_request_uid": group.resource_request_uid,
            "source_writer_session_id": session_id,
            "source_writer_binding_uid": binding.binding_uid,
            "host_observation": "unverified",
            "updated_at": now(),
        })
        _save_dispatch(project, state, dispatch_data)
        registry = load_registry(project, state)
        registry.setdefault("tasks", {}).setdefault(task["id"], {}).update({
            "owner_session": session_id,
            "dispatch_id": dispatch_row["id"],
            "dispatch_status": dispatch_row["status"],
            "workspace_group_uid": group.group_uid,
        })
        _save(project, state, registry)
        record = next(
            row for row in registry["sessions"] if row.get("id") == session_id
        )
        _, human = _paths(project, state)
        label = re.sub(
            r"[^a-z0-9]+", "-", f"{role}-{args.host or 'unknown'}".lower()
        ).strip("-") or "session"
        atomic(
            human / f"{session_id}-{label}.md",
            f"# {session_id} — {role}\n\n- 任务：{state['id']}\n"
            f"- 宿主：{args.host or 'unknown'}\n- thread：{args.thread or 'unknown'}\n"
            f"- workspace group：{group.group_uid}\n- worktree：{workspace}\n"
            f"- 写入模式：{mode}\n- 声明路径：{', '.join(paths) or '—'}\n",
        )
        return {
            "task": state["id"],
            "session": record,
            "controller_session": registry.get("controller_session"),
            "workspace_group_claim": receipt.to_dict(),
        }
    old_binding = None
    transition_envelope = None
    if takeover_pending:
        old_binding = require_row_capability(
            project, old_controller, "handoff", state["work_uuid"],
        )
        require_capability(binding, "handoff", state["work_uuid"])
        if binding.kind not in CONTROLLER_KINDS:
            raise WorkflowError("SESSION_CONTROLLER_CONTEXT_REQUIRED")
        # A takeover promotes the target into the workspace writer.  Validate
        # that authority before creating a durable intent or mutating either
        # binding; a read-only/handoff-only target must leave the old
        # controller untouched and be safe to retry.
        require_capability(binding, "write", state["work_uuid"])
        generations = [
            int(row.get("controller_generation") or 1)
            for row in registry.get("sessions", [])
            if row.get("role") == "controller"
        ]
        generation = max(generations or [1]) + 1
        if preflight:
            return {'binding_uid': binding.binding_uid, 'preflight': True}
        session_ref = _session_ref(
            project, state, args, session_id, workspace, paths, binding,
        )
        record = {
            "id": session_id, "role": role, "host": args.host or "unknown",
            "thread_id": args.thread, "task": task.get("id") if task else None,
            "branch": args.branch, "worktree": str(workspace), "write_mode": mode,
            "claimed_paths": paths, "status": "active", "started_at": now(),
            "updated_at": now(), "context": binding.handle_path,
            "context_mode": "bound", "binding_uid": binding.binding_uid,
            "work_uid": binding.work_uid,
            "controller_generation": binding.controller_generation,
            "cancel_generation": binding.cancel_generation,
            "context_binding": binding.to_dict(),
            "session_ref": session_ref,
        }
        transition_envelope = _new_transition_intent(
            project, state, "takeover", old_binding, binding, generation,
            old_controller["id"], session_id, args.reason, new_record=record,
        )
        transition_envelope, old_binding, binding = _execute_controller_transition(
            project, state, transition_envelope,
        )
        registry = load_registry(project, state)
        record = next(
            row for row in registry["sessions"]
            if row.get("id") == session_id
        )
    else:
        if role == "controller" and binding is not None:
            try:
                store = IdentityStore(project.root)
                owner = (store.read_work_controller(binding.work_uid) if preflight else
                         store.claim_initial_work_controller(
                             binding.work_uid, state['id'], binding.binding_uid,
                         ))
            except IdentityStoreError as exc:
                raise WorkflowError(str(exc)) from exc
            if owner is not None and owner["binding_uid"] != binding.binding_uid:
                raise WorkflowError("CONTROLLER_LEASE_REQUIRED")
        if preflight:
            return {'binding_uid': binding.binding_uid if binding else None,
                    'preflight': True}
        session_ref = _session_ref(
            project, state, args, session_id, workspace, paths, binding,
        )
        record = {
            "id": session_id, "role": role, "host": args.host or "unknown",
            "thread_id": args.thread, "task": task.get("id") if task else None,
            "branch": args.branch, "worktree": str(workspace), "write_mode": mode,
            "claimed_paths": paths, "status": "active", "started_at": now(), "updated_at": now(),
            "context": binding.handle_path if binding else None,
            "context_mode": "bound" if binding else "legacy-read-only",
            "binding_uid": binding.binding_uid if binding else None,
            "work_uid": binding.work_uid if binding else state.get("work_uuid"),
            "controller_generation": binding.controller_generation if binding else None,
            "cancel_generation": binding.cancel_generation if binding else None,
            "session_ref": session_ref,
        }
        if binding:
            record["context_binding"] = binding.to_dict()
    # A write session without a durable bound context is recorded as
    # legacy-read-only and never receives a workspace write lease.
    if mode == "write" and binding is None:
        record["context_mode"] = "legacy-read-only"
    if transition_envelope is None:
        registry["sessions"].append(record)
        if role == "controller" and binding is not None:
            registry["controller_session"] = session_id
        if mode == "write" and binding is not None:
            registry.setdefault("workspace_leases", {})[_key(workspace)] = session_id
        _event(registry, "session-register", session_id, f"{role} {mode}")
    _, human = _paths(project, state)
    label = re.sub(r"[^a-z0-9]+", "-", f"{role}-{args.host or 'unknown'}".lower()).strip("-") or "session"
    atomic(human / f"{session_id}-{label}.md",
           f"# {session_id} — {role}\n\n- 任务：{state['id']}\n- 宿主：{args.host or 'unknown'}\n- thread：{args.thread or 'unknown'}\n- worktree：{workspace}\n- 写入模式：{mode}\n- 声明路径：{', '.join(paths) or '—'}\n")
    _save(project, state, registry)
    return {"task": state["id"], "session": record, "controller_session": registry.get("controller_session")}


def _write_session(project, state, token):
    registry = load_registry(project, state)
    row = _find(registry, token)
    if row.get("status") not in ACTIVE:
        raise WorkflowError("SESSION_WRITE_LEASE_REQUIRED " + row["id"])
    # Context and authoritative state are the primary gates.  A legacy row
    # fails with SESSION_CONTEXT_REQUIRED before any lease reasoning.
    binding = row_context(project, row)
    owner = _lease_owner(registry, row.get("worktree"))
    if row.get("write_mode") != "write" or not owner or owner["id"] != row["id"]:
        raise WorkflowError("SESSION_WRITE_LEASE_REQUIRED " + row["id"])
    require_capability(binding, "write", state.get("work_uuid"))
    if row.get("invalidated_by_takeover"):
        raise WorkflowError("SESSION_BINDING_STALE " + row["id"])
    return registry, row



def require_controller_binding(project, state, caller_binding):
    """Require a plan writer to be the registered active controller."""
    if caller_binding is None:
        return None
    require_capability(caller_binding, "plan", state.get("work_uuid"))
    require_decision_controller(
        IdentityStore(project.root), caller_binding, state.get("work_uuid"),
    )
    return caller_binding


def require_actor_binding(caller_binding, row, action, work_uid):
    """Require the CLI caller to be the session row it is acting for."""
    if caller_binding is None:
        return None
    # Actor identity must be established before capability checks. Otherwise a
    # foreign observer is diagnosed as an unentitled controller instead of the
    # session row it is attempting to impersonate.
    if (
        caller_binding.binding_uid != row.get("binding_uid")
        or caller_binding.handle_path != row.get("context")
    ):
        raise WorkflowError("SESSION_ACTOR_MISMATCH " + str(row.get("id")))
    require_capability(caller_binding, action, work_uid)
    return caller_binding


def release_session_lease(project, state, token, reason=''):
    """Release a worker write lease after controller acceptance."""
    registry = load_registry(project, state)
    row = _find(registry, token)
    if row.get('status') == 'released':
        return {'session': token, 'released': True, 'idempotent': True}
    if isinstance(row.get('workspace_group'), dict):
        task_id = row.get('task')
        if (
            not task_id
            or (registry.get('tasks') or {}).get(task_id, {}).get(
                'acceptance_status'
            ) != 'accepted'
        ):
            raise WorkflowError('WORKSPACE_GROUP_TASK_NOT_ACCEPTED')
    key = _key(row.get('worktree')) if row.get('worktree') else None
    if key and registry.get('workspace_leases', {}).get(key) == row.get('id'):
        registry['workspace_leases'].pop(key, None)
    row['status'] = 'released'
    row['released_at'] = now()
    row['release_reason'] = redact(reason)
    row['updated_at'] = now()
    _event(registry, 'session-lease-released', row['id'], reason)
    _save(project, state, registry)
    return {'session': row['id'], 'released': True, 'idempotent': False}

def claim_session(project, state, token, paths, replace=False, caller_binding=None):
    registry, row = _write_session(project, state, token)
    require_row_capability(project, row, "claim", state.get("work_uuid"))
    require_actor_binding(caller_binding, row, "claim", state.get("work_uuid"))
    normalized = _normalize_paths(Path(row["worktree"]), paths)
    if not normalized:
        raise WorkflowError("SESSION_PATHS_REQUIRED declare --path")
    if _conflicts(registry, row["worktree"], normalized, row["id"]):
        raise WorkflowError("SESSION_PATH_CONFLICT " + normalized[0])
    merged = normalized if replace else list(dict.fromkeys((row.get("claimed_paths") or []) + normalized))
    if _conflicts(registry, row["worktree"], merged, row["id"]):
        raise WorkflowError("SESSION_PATH_CONFLICT " + merged[0])
    row["claimed_paths"] = merged
    row["updated_at"] = now()
    _event(registry, "session-claim", row["id"], ", ".join(normalized))
    _save(project, state, registry)
    return {"task": state["id"], "session": row, "claimed_paths": merged}


def _document(human, name, text, replace=False):
    target = human / name
    if target.exists():
        if not replace:
            raise WorkflowError("SESSION_DOCUMENT_EXISTS " + name)
        stamp = now().replace(":", "").replace("+00:00", "Z").replace("-", "")
        shutil.move(str(target), str(human / "history" / f"{target.stem}-{stamp}.md"))
    atomic(target, text)
    return target


def write_brief(project, state, args, caller_binding=None):
    registry = load_registry(project, state)
    author = _find(registry, args.session)
    owner = _find(registry, args.owner or args.session)
    if author.get("role") not in {"controller", "integration"}:
        raise WorkflowError("SESSION_BRIEF_AUTHOR_REQUIRED")
    if author.get("role") == "controller" and registry.get("controller_session") != author["id"]:
        raise WorkflowError("CONTROLLER_LEASE_REQUIRED")
    require_row_capability(project, author, "brief", state.get("work_uuid"))
    require_actor_binding(caller_binding, author, "brief", state.get("work_uuid"))
    task = project.item(state, args.task)
    if owner.get("task") not in (None, task["id"]):
        raise WorkflowError("SESSION_TASK_MISMATCH")
    if owner.get("write_mode") == "write":
        if args.path:
            claim_session(project, state, owner["id"], args.path)
            registry = load_registry(project, state)
            owner = _find(registry, args.owner or args.session)
        if not owner.get("claimed_paths"):
            raise WorkflowError("SESSION_PATHS_REQUIRED brief needs --path")
    if not args.title.strip() or not args.objective.strip():
        raise WorkflowError("SESSION_BRIEF_REQUIRED")
    _, human = _paths(project, state)
    build = {"build_root": None, "build_env": {}}
    if owner.get("worktree") and inside(Path(project.root) / ".gsp" / "worktrees",
                                       Path(owner["worktree"])):
        from .worktree_lifecycle import build_environment
        build = build_environment(project, state, task)
    lines = [
        f"# {task['id']} Brief — {args.title.strip()}", "",
        f"- GSP：{state.get('number') or state['id']}", f"- 任务：{task['id']}",
        f"- 发出会话：{author['id']}", f"- 执行会话：{owner['id']}",
        f"- worktree：{owner.get('worktree') or '—'}",
        f"- 构建根：{build.get('build_root') or '—'}",
        *[f"- {key}={value}" for key, value in (build.get("build_env") or {}).items()],
        "",
        "## 目标", "", args.objective.strip(), "",
        "## 允许路径", "",
    ]
    lines.extend(f"- {path}" for path in (owner.get("claimed_paths") or args.path or []))
    lines.extend(["", "## 接口", ""])
    lines.extend(f"- {item}" for item in (args.interface or [])) or lines.append("- 无新增公共接口")
    lines.extend(["", "## 测试", ""])
    lines.extend(f"- {item}" for item in (args.test or [])) or lines.append("- 由子会话补充局部测试")
    lines.extend(["", "## 依赖", "", f"- {args.depends or '无'}",
                  "", "## 禁止范围", "", f"- {args.forbid or '不得扩大到其他 T 任务或公共契约'}",
                  "", "## 交付条件", "", "- 完成局部实现并提交结构化 report；公共冲突先返回 CHANGE_REQUEST。", ""])
    path = _document(human, f"{task['id']}-brief.md", "\n".join(lines), args.replace)
    registry = load_registry(project, state)
    registry.setdefault("tasks", {})[task["id"]] = {
        **registry.get("tasks", {}).get(task["id"], {}),
        "brief_status": "briefed", "brief": str(path.relative_to(project.root)).replace("\\", "/"),
        "owner_session": owner["id"], "author_session": author["id"],
        "claimed_paths": owner.get("claimed_paths") or args.path or [],
    }
    _event(registry, "brief-written", owner["id"], task["id"])
    _save(project, state, registry)
    return {"task": state["id"], "plan_task": task["id"], "brief": registry["tasks"][task["id"]]["brief"], "owner_session": owner["id"]}


def _report_action_uid(state, args, caller_binding):
    """Keep one typed claim address stable across workflow/contract changes."""
    return request_action_uid(
        "session-report", args,
        context={
            "caller": request_binding_context(caller_binding),
            "work_identity": {key: state.get(key) for key in
                              ("id", "project_id", "work_uuid")},
        },
    )


def _report_request_uid(project, state, args, caller_binding):
    """Bind the claim to authoritative definitions and approval/seal facts."""
    context = request_report_action_context(caller_binding, state)
    if state.get("current_contract") is None:
        context["task_definition_digest"] = project.task_definition_hash(state)
    return request_action_uid(
        "session-report", args, state,
        context=context,
    )


def _validate_report_request(project, state, args, caller_binding):
    if caller_binding is None:
        raise WorkflowError("SESSION_BINDING_REQUIRED")
    return _preflight_report_request(
        project, state, args, caller_binding, create=False,
    )


def _authoritative_report_binding(project, caller_binding):
    """Reject a cached or edited binding that differs from current authority."""
    if not isinstance(caller_binding, SessionBinding):
        raise WorkflowError("SESSION_BINDING_REQUIRED")
    store = IdentityStore(project.root)
    try:
        authoritative_digest = store.read_session_binding(
            caller_binding.binding_uid,
        )
        authoritative_state = store.read_session_state(
            caller_binding.binding_uid,
        )
    except IdentityStoreError as exc:
        raise WorkflowError("SESSION_CONTEXT_TAMPERED") from exc
    if (
        authoritative_digest is None
        or authoritative_state is None
        or canonical_binding_digest(caller_binding) != authoritative_digest
        or binding_state(caller_binding) != authoritative_state
        or caller_binding.project_uid != project.identity().get("id")
    ):
        raise WorkflowError("SESSION_CONTEXT_TAMPERED")
    return binding_with_state(caller_binding, authoritative_state)


def _preflight_report_request(
    project, state, args, caller_binding, *, create=False,
):
    """Validate one report actor from ledger and authority without repair."""
    if caller_binding is None:
        raise WorkflowError("SESSION_BINDING_REQUIRED")
    caller_binding = _authoritative_report_binding(project, caller_binding)
    registry = load_registry(project, state, create=create)
    row = _find(registry, args.session)
    canonical_args = copy.copy(args)
    canonical_args.session = row["id"]
    lease_id = registry.get("workspace_leases", {}).get(
        _key(row.get("worktree")),
    )
    report_statuses = ACTIVE | {"blocked"}
    owner = next(
        (
            item for item in registry.get("sessions", [])
            if item.get("id") == lease_id
            and item.get("status") in report_statuses
            and item.get("write_mode") == "write"
        ),
        None,
    )
    if row.get("status") not in report_statuses:
        raise WorkflowError("SESSION_WRITE_LEASE_REQUIRED " + row["id"])
    if (
        row.get("write_mode") != "write"
        or not owner
        or owner.get("id") != row["id"]
    ):
        raise WorkflowError("SESSION_WRITE_LEASE_REQUIRED " + row["id"])
    if row.get("invalidated_by_takeover"):
        raise WorkflowError("SESSION_BINDING_STALE " + row["id"])
    work_uid = state.get("work_uuid")
    require_capability(caller_binding, "write", work_uid)
    require_actor_binding(caller_binding, row, "report", state.get("work_uuid"))
    task = project.item(state, canonical_args.task)
    canonical_args.task = task["id"]
    if row.get("task") != task["id"]:
        raise WorkflowError("SESSION_TASK_MISMATCH")
    mapping = registry.get("tasks", {}).get(task["id"]) or {}
    if mapping.get("owner_session") and mapping["owner_session"] != row["id"]:
        raise WorkflowError("SESSION_REPORT_OWNER_MISMATCH")
    status = str(canonical_args.status).upper()
    if status not in REPORT_STATUSES or not canonical_args.summary.strip():
        raise WorkflowError("SESSION_REPORT_REQUIRED")
    action_uid = _report_action_uid(state, canonical_args, caller_binding)
    requested_uid = _report_request_uid(
        project, state, canonical_args, caller_binding,
    )
    record = _report_operation_record(project, state, action_uid)
    if record is not None and record.get("effect") is not None:
        effect = record["effect"]
        if effect.get("request_uid", effect.get("action_uid")) != requested_uid:
            raise WorkflowError("SESSION_REPORT_REQUEST_CONFLICT stale request context")
    marker = state.get("_command_commit") or {}
    pending_uid = marker.get("action_uid")
    if marker.get("event") == "session-report" and pending_uid != action_uid:
        pending = _report_operation_record(project, state, pending_uid)
        if pending is None or pending.get("effect") is None:
            raise WorkflowError("SESSION_REPORT_AUTHORITY_INVALID")
        effect = pending["effect"]
        complete = (pending["operation"]["phase"] == "committed"
                    and pending.get("event") is not None
                    and digest_value(registry) == effect.get("registry_after_digest"))
        if not complete and effect.get("request_uid", effect.get("action_uid")) != requested_uid:
            raise WorkflowError("SESSION_REPORT_REQUEST_CONFLICT pending request context")
    return (
        registry, row, task, mapping, status, canonical_args,
        caller_binding,
    )


def _report_effect_matches_request(
    project, state, args, caller_binding, effect, action_uid=None,
):
    (
        registry, row, task, mapping, status, canonical_args,
        caller_binding,
    ) = (
        _validate_report_request(project, state, args, caller_binding)
    )
    if not isinstance(effect, dict):
        raise WorkflowError("SESSION_REPORT_REQUEST_CONFLICT")
    expected_request_uid = _report_request_uid(
        project, state, canonical_args, caller_binding,
    )
    recorded_request_uid = effect.get(
        "request_uid", effect.get("action_uid"),
    )
    if (
        effect.get("kind") != "report"
        or effect.get("session") != row["id"]
        or effect.get("task") != task["id"]
        or effect.get("status") != status
        or recorded_request_uid != expected_request_uid
        or (
            action_uid is not None
            and effect.get("action_uid") != action_uid
        )
    ):
        raise WorkflowError("SESSION_REPORT_REQUEST_CONFLICT")
    expected_text = _report_text(
        state, task, row, status, canonical_args,
    )
    if sha(expected_text.encode("utf-8")) != effect.get("report_digest"):
        raise WorkflowError("SESSION_REPORT_REQUEST_CONFLICT")
    return effect


def _report_state_file(project, state):
    run, _doc = project.paths(state["id"])
    return (run / "state.json").resolve()


def _report_operation_record(project, state, action_uid):
    """Resolve one report request from the existing immutable journal chain."""
    if not isinstance(action_uid, str) or not action_uid.startswith("act-"):
        raise WorkflowError("SESSION_REPORT_REQUEST_INVALID")
    state_file = _report_state_file(project, state)
    expected_state_path = state_file.relative_to(project.root).as_posix()
    store = IdentityStore(project.root)
    try:
        details = store.operation_for_logical_key(
            project_write_logical_key(state["id"], action_uid),
        )
        if details is None:
            return None
        if (
            details.get("operation_kind") != "project-write"
            or details.get("state_path") != expected_state_path
        ):
            raise WorkflowError("SESSION_REPORT_AUTHORITY_INVALID")
        phase = details.get("phase")
        if phase == "failed":
            raise WorkflowError("SESSION_REPORT_AUTHORITY_INVALID")
        candidate = store.get_operation_candidate(details["operation_uid"])
        if candidate is None:
            if phase != "intent":
                raise WorkflowError("SESSION_REPORT_AUTHORITY_INVALID")
            return {
                "store": store,
                "operation": details,
                "candidate": None,
                "frozen_state": None,
                "effect": None,
                "event": None,
                "state_file": state_file,
            }
        frozen_state = json.loads(candidate.candidate_text)
        marker = frozen_state.get("_command_commit") or {}
        effect = (marker.get("effects") or {}).get("report") or {}
        if (
            marker.get("schema") != 1
            or marker.get("command") != "session-report"
            or marker.get("event") != "session-report"
            or marker.get("action_uid") != action_uid
            or marker.get("request_digest") != action_uid
            or effect.get("kind") != "report"
            or effect.get("action_uid") != action_uid
            or (
                phase == "committed"
                and details.get("result_ref") != candidate.candidate_digest
            )
        ):
            raise WorkflowError("SESSION_REPORT_AUTHORITY_INVALID")
        events = store.event_projection(state_file)
        matches = [
            event for event in events
            if (event.get("payload") or {}).get("event") == "session-report"
            and (event.get("payload") or {}).get("action_uid") == action_uid
        ]
        if len(matches) > 1:
            raise WorkflowError("SESSION_REPORT_AUTHORITY_INVALID")
        event = matches[0] if matches else None
        if event is not None:
            payload = event.get("payload") or {}
            if (
                phase != "committed"
                or event.get("kind") != "project-write"
                or payload.get("state_digest") != details.get("result_ref")
                or payload.get("state_path") != expected_state_path
            ):
                raise WorkflowError("SESSION_REPORT_AUTHORITY_INVALID")
        return {
            "store": store,
            "operation": details,
            "candidate": candidate,
            "frozen_state": frozen_state,
            "effect": effect,
            "event": event,
            "state_file": state_file,
        }
    except (IdentityStoreError, OSError, UnicodeError, TypeError, ValueError) as exc:
        raise WorkflowError("SESSION_REPORT_AUTHORITY_INVALID") from exc


def _latest_report_operation(project, state, task_id):
    state_file = _report_state_file(project, state)
    store = IdentityStore(project.root)
    try:
        events = store.event_projection(state_file)
    except (IdentityStoreError, OSError, UnicodeError, TypeError, ValueError) as exc:
        raise WorkflowError("SESSION_REPORT_AUTHORITY_INVALID") from exc
    for event in reversed(events):
        payload = event.get("payload") or {}
        if payload.get("event") != "session-report":
            continue
        action_uid = payload.get("action_uid")
        record = _report_operation_record(project, state, action_uid)
        if record is None or record.get("event") is None:
            raise WorkflowError("SESSION_REPORT_AUTHORITY_INVALID")
        if (record.get("effect") or {}).get("task") == task_id:
            return record
    return None


def _pending_report_authority(
    project, state, args, caller_binding, requested_action_uid,
):
    """Recover an unfinished marker before a changed request can replace it."""
    marker = state.get("_command_commit") or {}
    if marker.get("event") != "session-report":
        return None
    marker_action_uid = marker.get("action_uid")
    if marker_action_uid == requested_action_uid:
        return None
    record = _report_operation_record(project, state, marker_action_uid)
    if record is None or record.get("effect") is None:
        raise WorkflowError("SESSION_REPORT_AUTHORITY_INVALID")
    effect = record["effect"]
    if (
        record["operation"]["phase"] == "committed"
        and record.get("event") is not None
        and digest_value(load_registry(project, state, create=False))
        == effect.get("registry_after_digest")
    ):
        return None
    _report_effect_matches_request(
        project, state, args, caller_binding, effect,
    )
    return record


def _verified_previous_report(project, state, task, report_file):
    """Require the live artifact and its history proof to match the journal."""
    authority = _latest_report_operation(project, state, task["id"])
    report_relative = report_file.relative_to(project.root).as_posix()
    if authority is None:
        if report_file.exists():
            raise WorkflowError("SESSION_REPORT_DOCUMENT_CONFLICT")
        return None, None
    effect = authority["effect"]
    if effect.get("report_path") != report_relative:
        raise WorkflowError("SESSION_REPORT_AUTHORITY_INVALID")
    if not report_file.is_file():
        raise WorkflowError("SESSION_REPORT_DOCUMENT_CONFLICT")
    report_bytes = report_file.read_bytes()
    if sha(report_bytes) != effect.get("report_digest"):
        raise WorkflowError("SESSION_REPORT_DOCUMENT_CONFLICT")
    archive_relative = effect.get("archive_path")
    if archive_relative is not None:
        archive_path = _project_path(
            project, archive_relative, "SESSION_REPORT_AUTHORITY_INVALID",
        )
        if (
            not archive_path.is_file()
            or sha(archive_path.read_bytes()) != effect.get("archive_digest")
        ):
            raise WorkflowError("SESSION_REPORT_HISTORY_CONFLICT")
    return authority, report_bytes


def _assert_report_effect_complete(project, state, effect):
    registry = load_registry(project, state)
    if digest_value(registry) != effect.get("registry_after_digest"):
        raise WorkflowError("SESSION_REPORT_REGISTRY_CONFLICT")
    report_path = _project_path(
        project, effect.get("report_path"), "SESSION_REPORT_EFFECT_INVALID",
    )
    if not report_path.is_file() or sha(report_path.read_bytes()) != effect.get("report_digest"):
        raise WorkflowError("SESSION_REPORT_DOCUMENT_CONFLICT")
    if effect.get("archive_path") is not None:
        archive_path = _project_path(
            project, effect.get("archive_path"), "SESSION_REPORT_EFFECT_INVALID",
        )
        if (
            not archive_path.is_file()
            or sha(archive_path.read_bytes()) != effect.get("archive_digest")
        ):
            raise WorkflowError("SESSION_REPORT_HISTORY_CONFLICT")


def _report_text(state, task, row, status, args):
    lines = [
        f"# {task['id']} Report — {status}", "",
        f"- GSP：{state.get('number') or state['id']}", f"- 会话：{row['id']}",
        f"- 宿主：{row.get('host') or 'unknown'}", f"- 状态：{status}", "",
        "## Summary", "", args.summary.strip(), "",
    ]
    for label, values, empty in (
        ("FILES", args.files, "无文件"),
        ("TESTS", args.tests, "未提供"),
        ("INTERFACES", args.interfaces, "无变化"),
        ("EVIDENCE", args.evidence, "未提供"),
    ):
        lines.extend([f"## {label}", ""])
        lines.extend(f"- {item}" for item in (values or [])) or lines.append(f"- {empty}")
        lines.append("")
    lines.extend([
        "## NEED_FROM_MAIN", "", f"- {args.need_from_main or '无'}",
        "", "## Ruling", "", f"- {args.ruling or '无'}", "",
    ])
    return "\n".join(lines)


def _report_registry_candidate(registry, patch):
    after = copy.deepcopy(registry)
    task_id = patch["task"]
    session_id = patch["session"]
    after.setdefault("tasks", {})[task_id] = copy.deepcopy(patch["task_mapping"])
    row = next(
        (item for item in after.get("sessions", []) if item.get("id") == session_id),
        None,
    )
    if row is None:
        raise WorkflowError("SESSION_REPORT_REGISTRY_CONFLICT")
    row.update(copy.deepcopy(patch["session_state"]))
    after.setdefault("events", []).append(copy.deepcopy(patch["event"]))
    after["events"] = after["events"][-200:]
    after.setdefault("outbox", []).append(copy.deepcopy(patch["outbox"]))
    after["outbox"] = after["outbox"][-500:]
    return after


def _apply_report_registry_patch(registry, patch):
    after = _report_registry_candidate(registry, patch)
    if digest_value(after) != patch["registry_after_digest"]:
        raise WorkflowError("SESSION_REPORT_REGISTRY_CONFLICT")
    return after


def prepare_report(project, state, args, caller_binding=None, action_uid=None):
    """Freeze one report request without publishing any report-side effect."""
    (
        registry, row, task, mapping, status, canonical_args,
        caller_binding,
    ) = _validate_report_request(
        project, state, args, caller_binding,
    )
    request_uid = _report_request_uid(project, state, canonical_args, caller_binding)
    action_uid = action_uid or _report_action_uid(
        state, canonical_args, caller_binding,
    )
    if not isinstance(action_uid, str) or not action_uid.startswith("act-"):
        raise WorkflowError("SESSION_REPORT_REQUEST_INVALID")
    run, doc = project.paths(state["id"])
    human = doc / "sessions"
    report_file = human / f"{task['id']}-report.md"
    report_relative = report_file.relative_to(project.root).as_posix()
    report_text = _report_text(state, task, row, status, canonical_args)
    report_digest = sha(report_text.encode("utf-8"))
    _authority, previous_report = _verified_previous_report(
        project, state, task, report_file,
    )
    target_before = sha(previous_report) if previous_report is not None else None
    reported_at = now()
    archive_relative = None
    archive_text = None
    archive_digest = None
    if previous_report is not None:
        stamp = (
            reported_at
            .replace(":", "")
            .replace("+00:00", "Z")
            .replace("-", "")
        )
        suffix = action_uid.removeprefix("act-")[:12]
        archive = human / "history" / (
            f"{report_file.stem}-{stamp}-{suffix}{report_file.suffix}"
        )
        archive_relative = archive.relative_to(project.root).as_posix()
        if archive.exists():
            raise WorkflowError("SESSION_REPORT_HISTORY_CONFLICT")
        try:
            archive_text = previous_report.decode("utf-8")
        except UnicodeError as exc:
            raise WorkflowError("SESSION_REPORT_DOCUMENT_CONFLICT") from exc
        archive_digest = sha(previous_report)

    mapping_after = copy.deepcopy(mapping)
    mapping_after.update({
        "report_status": status,
        "report": report_relative,
        "report_session": row["id"],
        "reported_at": reported_at,
    })
    registry_patch = {
        "task": task["id"],
        "session": row["id"],
        "task_mapping": mapping_after,
        "session_state": {
            "status": "blocked" if status != "DONE" else "reported",
            "updated_at": reported_at,
        },
        "event": {
            "id": uuid.uuid4().hex,
            "at": reported_at,
            "event": "report-written",
            "session": row["id"],
            "detail": f"{task['id']} {status}",
        },
        "outbox": {
            "id": uuid.uuid4().hex,
            "task": task["id"],
            "session": row["id"],
            "status": status,
            "report": report_relative,
            "worktree": row.get("worktree"),
            "reported_at": reported_at,
            "delivered": False,
            "created_at": reported_at,
        },
    }
    registry_before_digest = digest_value(registry)
    prospective = _report_registry_candidate(registry, registry_patch)
    registry_patch["registry_after_digest"] = digest_value(prospective)

    result = {
        "task": state["id"],
        "plan_task": task["id"],
        "report": report_relative,
        "status": status,
    }
    effect = {
        "schema": 1,
        "kind": "report",
        "action_uid": action_uid,
        "request_uid": request_uid,
        "task": task["id"],
        "session": row["id"],
        "status": status,
        "report_path": report_relative,
        "report_text": report_text,
        "report_digest": report_digest,
        "target_before_digest": target_before,
        "archive_path": archive_relative,
        "archive_text": archive_text,
        "archive_digest": archive_digest,
        "registry_path": (
            run / "sessions" / "index.json"
        ).relative_to(project.root).as_posix(),
        "registry_before_digest": registry_before_digest,
        "registry_after_digest": digest_value(prospective),
        "registry_patch": registry_patch,
        "result": result,
    }
    return effect, result


def _project_path(project, relative, code):
    if not isinstance(relative, str) or not relative:
        raise WorkflowError(code)
    path = (project.root / relative).resolve()
    if not inside(project.root, path):
        raise WorkflowError(code)
    return path


def apply_report_effect(project, state, effect):
    """Publish one frozen report effect and resume from its exact cursor."""
    if (
        not isinstance(effect, dict)
        or effect.get("schema") != 1
        or effect.get("kind") != "report"
        or effect.get("task") not in {
            item.get("id") for item in state.get("tasks", [])
        }
    ):
        raise WorkflowError("SESSION_REPORT_EFFECT_INVALID")
    registry_path = _project_path(
        project, effect.get("registry_path"), "SESSION_REPORT_EFFECT_INVALID",
    )
    expected_registry = project.paths(state["id"])[0] / "sessions" / "index.json"
    if registry_path != expected_registry.resolve():
        raise WorkflowError("SESSION_REPORT_EFFECT_INVALID")
    report_path = _project_path(
        project, effect.get("report_path"), "SESSION_REPORT_EFFECT_INVALID",
    )
    archive_path = (
        _project_path(
            project, effect.get("archive_path"), "SESSION_REPORT_EFFECT_INVALID",
        )
        if effect.get("archive_path") is not None else None
    )
    registry = load_registry(project, state)
    current_registry_digest = digest_value(registry)
    before_digest = effect.get("registry_before_digest")
    after_digest = effect.get("registry_after_digest")
    registry_after = None
    registry_complete = False
    if current_registry_digest == before_digest:
        registry_after = _apply_report_registry_patch(
            registry, effect["registry_patch"],
        )
    elif current_registry_digest == after_digest:
        registry_complete = True
    else:
        raise WorkflowError("SESSION_REPORT_REGISTRY_CONFLICT")

    current_report_digest = (
        sha(report_path.read_bytes()) if report_path.is_file() else None
    )
    if current_report_digest not in {
        effect.get("target_before_digest"),
        effect.get("report_digest"),
    }:
        raise WorkflowError("SESSION_REPORT_DOCUMENT_CONFLICT")
    if registry_complete and current_report_digest != effect.get("report_digest"):
        raise WorkflowError("SESSION_REPORT_DOCUMENT_CONFLICT")
    if archive_path is not None:
        if archive_path.is_file():
            if sha(archive_path.read_bytes()) != effect.get("archive_digest"):
                raise WorkflowError("SESSION_REPORT_HISTORY_CONFLICT")
        elif registry_complete:
            raise WorkflowError("SESSION_REPORT_HISTORY_CONFLICT")
        else:
            atomic(archive_path, effect["archive_text"])
    if not registry_complete and current_report_digest != effect.get("report_digest"):
        atomic(report_path, effect["report_text"])
    if registry_after is not None:
        _save(project, state, registry_after)
    else:
        _save(project, state, registry)
    from .dispatch import release_execution_slot_for_report
    release_execution_slot_for_report(
        project,
        state,
        effect["result"]["plan_task"],
        effect["result"]["status"],
        effect["registry_patch"]["session"],
    )
    return copy.deepcopy(effect["result"])


def write_report(project, state, args, caller_binding=None, action_uid=None):
    """Durably bind one report request to its Project.write effect."""
    (
        registry, row, task, mapping, status, canonical_args,
        caller_binding,
    ) = _validate_report_request(project, state, args, caller_binding)
    action_uid = action_uid or _report_action_uid(
        state, canonical_args, caller_binding,
    )
    record = _pending_report_authority(
        project, state, canonical_args, caller_binding, action_uid,
    )
    if record is not None:
        action_uid = record["effect"]["action_uid"]
    else:
        record = _report_operation_record(project, state, action_uid)
    if record is not None:
        phase = record["operation"]["phase"]
        effect = record.get("effect")
        if effect is not None:
            _report_effect_matches_request(
                project, state, canonical_args, caller_binding, effect,
                action_uid=action_uid,
            )
        if phase in {"prepared", "file-written"}:
            if effect is None:
                raise WorkflowError("SESSION_REPORT_AUTHORITY_INVALID")
            project.write(
                record["frozen_state"], "session-report",
                record["frozen_state"]["_command_commit"]["detail"],
                action_uid=action_uid,
            )
            record = _report_operation_record(project, state, action_uid)
        if record["operation"]["phase"] == "committed":
            if record.get("event") is None:
                state_file = record["state_file"]
                if (
                    not state_file.is_file()
                    or sha(state_file.read_bytes())
                    != record["operation"].get("result_ref")
                ):
                    raise WorkflowError("SESSION_REPORT_AUTHORITY_INVALID")
                project.write(
                    record["frozen_state"], "session-report",
                    record["frozen_state"]["_command_commit"]["detail"],
                    action_uid=action_uid,
                )
            else:
                _assert_report_effect_complete(
                    project, record["frozen_state"], record["effect"],
                )
            result = copy.deepcopy(record["effect"]["result"])
            from .dispatch import release_execution_slot_for_report
            release_execution_slot_for_report(
                project,
                state,
                result["plan_task"],
                result["status"],
                record["effect"]["registry_patch"]["session"],
            )
            result["replayed"] = True
            return result
        if record["operation"]["phase"] != "intent":
            raise WorkflowError("SESSION_REPORT_AUTHORITY_INVALID")
    elif row.get("status") == "blocked":
        raise WorkflowError("SESSION_WRITE_LEASE_REQUIRED " + row["id"])

    effect, result = prepare_report(
        project, state, canonical_args, caller_binding, action_uid=action_uid,
    )
    _run, doc = project.paths(state["id"])
    detail = json.dumps(result, ensure_ascii=False, sort_keys=True)
    state["_command_commit"] = {
        "schema": 1,
        "command": "session-report",
        "event": "session-report",
        "action_uid": action_uid,
        "request_digest": action_uid,
        "detail": detail,
        "effects": {
            "documents_root": doc.relative_to(project.root).as_posix(),
            "report": effect,
        },
    }
    project.write(state, "session-report", detail, action_uid=action_uid)
    from .dispatch import release_execution_slot_for_report
    release_execution_slot_for_report(
        project,
        state,
        result["plan_task"],
        result["status"],
        effect["registry_patch"]["session"],
    )
    return copy.deepcopy(result)


def replay_controller_handoff(project, state, args, caller_binding, *, complete=False):
    """Authorize the recorded successor before repairing an exact handoff.

    This is a read-only check until ``complete`` is requested. A completed
    operation returns its recorded outcome without rewriting projections.
    """
    registry = load_registry(project, state, create=False)
    old = _find(registry, args.from_session)
    new = _find(registry, args.to_session)
    if caller_binding.binding_uid != new.get('binding_uid'):
        return None
    envelope = _load_transition_intent(
        project, state['id'], 'handoff', old.get('binding_uid'),
        new.get('binding_uid'),
    )
    if envelope is None:
        return None
    payload = envelope['payload']
    if (
        payload.get('work_uid') != state.get('work_uuid')
        or payload.get('old_session') != old['id']
        or payload.get('new_session') != new['id']
        or payload.get('reason') != redact(args.reason)
        or getattr(args, 'argv', None)
    ):
        raise WorkflowError('CONTROLLER_TRANSITION_INTENT_CHANGED')
    store = IdentityStore(project.root)
    old_current = _authoritative_transition_binding(
        store, payload['old_binding'], state['work_uuid'],
    )
    new_current = _authoritative_transition_binding(
        store, payload['new_binding'], state['work_uuid'],
    )
    require_capability(new_current, 'handoff', state['work_uuid'])
    require_capability(new_current, 'write', state['work_uuid'])
    if (
        new_current.kind not in CONTROLLER_KINDS
        or caller_binding.to_dict() != new_current.to_dict()
        or new.get('context') != new_current.handle_path
    ):
        raise WorkflowError('SESSION_ACTOR_MISMATCH ' + new['id'])
    old_before = SessionBinding.from_dict(payload['old_binding'])
    new_before = SessionBinding.from_dict(payload['new_binding'])
    next_generation = payload.get('next_generation')
    if (
        type(next_generation) is not int
        or next_generation <= max(
            old_before.controller_generation, new_before.controller_generation,
        )
        or old_before.cancelled or new_before.cancelled
    ):
        raise WorkflowError('CONTROLLER_TRANSITION_INTENT_INVALID')
    old_after = binding_with_state(old_before, {
        'controller_generation': old_before.controller_generation,
        'cancel_generation': old_before.cancel_generation + 1,
        'cancelled': True,
    })
    new_after = binding_with_state(new_before, {
        'controller_generation': next_generation,
        'cancel_generation': new_before.cancel_generation,
        'cancelled': False,
    })
    owner = store.read_work_controller(state['work_uuid'])
    before = old_current == old_before and new_current == new_before
    after = old_current == old_after and new_current == new_after
    if before:
        require_decision_controller(store, old_current, state['work_uuid'])
    elif not after or owner is None or owner['binding_uid'] != new_current.binding_uid:
        raise WorkflowError('CONTROL_VERSION_CHANGED')
    operation_uid = envelope.get('operation_uid')
    operation = store.get_operation(operation_uid) if operation_uid else None
    if operation is not None:
        if operation.request != _transition_request(project, envelope):
            raise WorkflowError('CONTROLLER_TRANSITION_INTENT_CHANGED')
        if operation.phase == 'failed':
            raise WorkflowError('OPERATION_FAILED')
        if before and operation.phase != 'intent':
            raise WorkflowError('CONTROL_VERSION_CHANGED')
    elif after:
        raise WorkflowError('CONTROLLER_TRANSITION_INTENT_INVALID')
    committed = operation is not None and operation.phase == 'committed'
    if committed:
        if (
            operation.result_ref != digest_value({
                'old': old_after.to_dict(), 'new': new_after.to_dict(),
            })
            or registry.get('controller_session') != new['id']
            or new.get('status') not in ACTIVE
        ):
            raise WorkflowError('OPERATION_RESULT_CHANGED')
    elif complete:
        _execute_controller_transition(project, state, envelope)
    return {
        'task': state['id'], 'controller_session': new['id'],
        'from': old['id'], 'to': new['id'], 'status': 'handed-off',
        'replayed': committed,
    }


def handoff_controller(project, state, args, caller_binding=None):
    registry = load_registry(project, state)
    old = _find(registry, args.from_session)
    new = _find(registry, args.to_session)
    if registry.get("controller_session") != old["id"]:
        raise WorkflowError("CONTROLLER_SOURCE_MISMATCH")
    if old["status"] not in ACTIVE or new["status"] not in ACTIVE:
        raise WorkflowError("SESSION_NOT_ACTIVE")
    if old["id"] == new["id"]:
        raise WorkflowError("CONTROLLER_TARGET_SAME")
    old_binding = require_row_capability(
        project, old, "handoff", state.get("work_uuid"),
    )
    require_actor_binding(caller_binding, old, "handoff", state.get("work_uuid"))
    new_binding = require_row_capability(
        project, new, "handoff", state.get("work_uuid"),
    )
    if new_binding.kind not in CONTROLLER_KINDS:
        raise WorkflowError("SESSION_CONTROLLER_CONTEXT_REQUIRED")
    require_capability(new_binding, "write", state.get("work_uuid"))
    next_generation = max(
        old_binding.controller_generation, new_binding.controller_generation,
    ) + 1
    envelope = _new_transition_intent(
        project, state, "handoff", old_binding, new_binding, next_generation,
        old["id"], new["id"], args.reason,
    )
    _execute_controller_transition(project, state, envelope)
    return {"task": state["id"], "controller_session": new["id"], "from": old["id"], "to": new["id"], "status": "handed-off"}


def session_status(project, state):
    registry = load_registry(project, state)
    blockers = []
    active = [row for row in registry.get("sessions", []) if row.get("status") in ACTIVE]
    if active and not registry.get("controller_session"):
        blockers.append({"code": "CONTROLLER_MISSING"})
    if len([row for row in active if row.get("role") == "controller"]) > 1:
        blockers.append({"code": "CONTROLLER_LEASE_CONFLICT"})
    for row in active:
        if row.get("write_mode") != "write":
            continue
        owner = _lease_owner(registry, row.get("worktree"))
        if not owner or owner["id"] != row["id"]:
            blockers.append({"code": "WORKSPACE_WRITE_LEASE_INVALID", "session": row["id"]})
        if not row.get("claimed_paths"):
            blockers.append({"code": "SESSION_PATHS_MISSING", "session": row["id"]})
    for task_id, mapping in registry.get("tasks", {}).items():
        if mapping.get("report_status") == "BLOCKED":
            blockers.append({"code": "SESSION_TASK_BLOCKED", "task": task_id})
        if mapping.get("report_status") == "CHANGE_REQUEST":
            blockers.append({"code": "SESSION_CHANGE_REQUEST", "task": task_id})
    return {"schema": registry.get("schema", 1), "task": state["id"], "number": state.get("number"),
            "status": "blocked" if blockers else "ready",
            "controller_session": registry.get("controller_session"),
            "workspace_leases": registry.get("workspace_leases", {}),
            "sessions": registry.get("sessions", []), "tasks": registry.get("tasks", {}),
            "blockers": blockers}


def authorize_edit(project, state, token, paths, context_path=None):
    registry = load_registry(project, state)
    row = _find(registry, token)
    absolute = [Path(path).resolve() for path in paths]
    control_documents = (
        project.control_document_paths(state) if absolute else ()
    )
    only_control_documents = bool(absolute) and all(
        path in control_documents for path in absolute
    )
    if (
        only_control_documents
        and row.get("role") == "controller"
        and registry.get("controller_session") == row.get("id")
        and row.get("status") in ACTIVE
    ):
        caller = row_context(project, row)
        if context_path:
            explicit = load_binding(context_path)
            if (
                explicit.binding_uid != row.get("binding_uid")
                or explicit.handle_path != row.get("context")
            ):
                raise WorkflowError(
                    "SESSION_CONTEXT_TAMPERED explicit context does not match session"
                )
            caller = explicit
        require_controller_binding(project, state, caller)
        normalized = _normalize_paths(project.root, absolute)
        return {
            "session": row["id"],
            "paths": normalized,
            "allowed": True,
            "authority": "controller-control-document",
        }

    registry, row = _write_session(project, state, token)
    if context_path:
        explicit = load_binding(context_path)
        if (
            explicit.binding_uid != row.get("binding_uid")
            or explicit.handle_path != row.get("context")
        ):
            raise WorkflowError(
                "SESSION_CONTEXT_TAMPERED explicit context does not match session"
            )
        require_capability(explicit, "write", state.get("work_uuid"))
    normalized = _normalize_paths(Path(row["worktree"]), absolute)
    if any(path in control_documents for path in absolute):
        # Source ownership is not ownership of the work's frozen contract or
        # final review/acceptance documents. Reuse the planning authority gate;
        # no source-directory allowlist is introduced here.
        caller = explicit if context_path else row_context(project, row)
        require_controller_binding(project, state, caller)
    conflict = _conflicts(
        registry, row["worktree"], normalized, exclude=row["id"],
    )
    if conflict:
        raise WorkflowError("SESSION_PATH_CONFLICT " + conflict[0][1])
    return {"session": row["id"], "paths": normalized, "allowed": True}
