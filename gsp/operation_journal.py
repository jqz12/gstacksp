"""Durable operation intents, state CAS, outbox, and document merges."""
import hashlib
import json
from pathlib import Path
import re

from .identity_store import IdentityStoreError
from .orchestration_types import DocumentEdit, RequestRef, SessionBinding, digest_value


ABSENT_STATE_DIGEST = hashlib.sha256(b"<absent>").hexdigest()


REPLAYABLE_STATE_COMMANDS = frozenset({"task-reopen", "documents-adopt", "recover-run"})


def project_write_logical_key(run_id, action_uid):
    """The single stable key definition for project writes and their recovery."""
    if (not isinstance(run_id, str) or not run_id.strip()
            or not isinstance(action_uid, str) or not action_uid.strip()):
        raise IdentityStoreError("COMMAND_REQUEST_INVALID")
    return "project-write:" + digest_value({"run_id": run_id, "action_uid": action_uid})


def project_write_marker(state, event, detail, action_uid, documents_root):
    """Freeze replay data with the candidate, never in a later projection.

    Existing same-action markers retain their exact shape for old candidates.
    Only the outer CLI attaches a request_digest or rework decision; neither
    those fields nor this descriptor grant caller permissions.
    """
    existing = state.get("_command_commit")
    if existing is not None and not isinstance(existing, dict):
        raise IdentityStoreError("COMMAND_REQUEST_INVALID")
    if existing is not None and existing.get("action_uid") == action_uid:
        if (existing.get("schema") != 1
                or existing.get("event", existing.get("command")) != event
                or not isinstance(existing.get("detail"), str)):
            raise IdentityStoreError("REQUEST_INPUT_CHANGED")
        return dict(existing)
    return {"schema": 1, "command": event, "event": event,
            "action_uid": action_uid, "detail": detail,
            "effects": {"documents_root": documents_root}}


def recover_current_project_write(project, state):
    """Finish published work before a caller interprets status/queue changes.

    A file-written transition may have removed its own queue entry. Rebuilding
    an action key from that changed state would create a different request.
    The frozen descriptor instead supplies the original action/event/detail.
    This is called only inside an authorized mutating/recovery CLI boundary.
    """
    if state.get("_command_commit") is None:
        return
    from .identity_store import IdentityStore
    store = IdentityStore(project.root)
    state_file = project.paths(state["id"])[0] / "state.json"
    finish_previous_command_projection(project, store, state_file, None)


def _command_projection_path(state_file, action_uid):
    key = hashlib.sha256(action_uid.encode("utf-8")).hexdigest()
    return Path(state_file).parent / "command-projections" / (key + ".json")


def finish_previous_command_projection(project, store, state_file, next_action_uid):
    """Drain a proved committed predecessor before a new state write.

    The completion file is a rebuildable cursor, never authorization or a test
    receipt. Skipping replay requires matching the current bytes AND the actual
    authoritative committed operation. Missing/corrupt cursor means replay.
    """
    state_file = Path(state_file)
    if not state_file.is_file():
        return
    raw = state_file.read_bytes()
    try:
        published = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise IdentityStoreError("STATE_CANDIDATE_INVALID") from exc
    marker = published.get("_command_commit")
    if marker is None:
        return
    if (not isinstance(marker, dict) or marker.get("schema") != 1
            or not isinstance(marker.get("event", marker.get("command")), str)
            or not marker.get("event", marker.get("command")).strip()
            or not isinstance(marker.get("action_uid"), str)
            or not marker["action_uid"].strip()
            or not isinstance(marker.get("detail"), str)):
        raise IdentityStoreError("COMMAND_REQUEST_INVALID")
    action_uid = marker["action_uid"]
    logical_key = project_write_logical_key(published.get("id"), action_uid)
    operation = store.operation_for_logical_key(logical_key)
    if (operation is None or operation["operation_kind"] != "project-write"
            or operation["phase"] not in {"prepared", "file-written", "committed"}
            or operation["state_path"] != store._normalize_operation_path(state_file)):
        raise IdentityStoreError("COMMAND_REPLAY_UNPROVEN")
    if action_uid == next_action_uid:
        return
    path = _command_projection_path(state_file, action_uid)
    try:
        cursor = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cursor = None
    expected = {"schema": 1, "action_uid": action_uid,
                "operation_uid": operation["operation_uid"],
                "state_digest": hashlib.sha256(raw).hexdigest()}
    if (cursor == expected and operation["phase"] == "committed"
            and operation["result_ref"] == expected["state_digest"]):
        return
    # Only an existing project/path-bound intent may be recovered. The marker
    # and completion hint cannot create an operation or grant caller authority.
    # Project.write rechecks the immutable candidate and current state bytes.
    project.write(published, marker.get("event", marker.get("command")),
                  marker["detail"], action_uid=action_uid)


def mark_command_projection_complete(state_file, frozen_state, operation):
    marker = frozen_state.get("_command_commit")
    if marker is None:
        return
    if not isinstance(marker.get("event", marker.get("command")), str):
        raise IdentityStoreError("COMMAND_REQUEST_INVALID")
    from .core import (atomic)
    atomic(_command_projection_path(state_file, marker["action_uid"]), {
        "schema": 1, "action_uid": marker["action_uid"],
        "operation_uid": operation.operation_uid, "state_digest": operation.result_ref,
    })


def state_digest(path):
    path = Path(path)
    if not path.is_file():
        return ABSENT_STATE_DIGEST
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _state_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _state_value_digest(value):
    return hashlib.sha256(_state_text(value).encode("utf-8")).hexdigest()


def _semantic_state(value):
    if not isinstance(value, dict):
        raise IdentityStoreError("STATE_CANDIDATE_INVALID")
    semantic = {key: item for key, item in value.items() if key != "updated"}
    try:
        return json.dumps(
            semantic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise IdentityStoreError("STATE_CANDIDATE_INVALID") from exc


def begin_intent(store, request, expected_state_digest):
    return store.begin_operation(request, expected_state_digest)


def get_operation(store, operation_uid):
    return store.get_operation(operation_uid)


def _freeze_published_legacy_candidate(
    store,
    operation_uid,
    path,
    semantic_digest,
):
    try:
        frozen_text = path.read_text(encoding="utf-8")
        return store.freeze_operation_candidate(
            operation_uid,
            path,
            frozen_text,
            semantic_digest,
        )
    except (OSError, UnicodeError) as exc:
        raise IdentityStoreError("OPERATION_CANDIDATE_UNAVAILABLE") from exc
    except IdentityStoreError as exc:
        if str(exc) not in {
            "STATE_CANDIDATE_INVALID",
            "OPERATION_CANDIDATE_UNAVAILABLE",
        }:
            raise
        raise IdentityStoreError("OPERATION_CANDIDATE_UNAVAILABLE") from exc


def commit_state(store, operation_uid, state_path, new_state, *, semantic_digest=None):
    path = Path(state_path).resolve()
    candidate_text = _state_text(new_state)
    new_digest = _state_value_digest(new_state)
    operation = store.get_operation(operation_uid)
    if operation.phase == "failed":
        raise IdentityStoreError("OPERATION_FAILED")
    current = state_digest(path)
    candidate = store.get_operation_candidate(operation_uid)
    if candidate is not None:
        if path != store.operation_state_path(operation_uid):
            raise IdentityStoreError("REQUEST_INPUT_CHANGED")
        if semantic_digest is None:
            if candidate.candidate_text != candidate_text or candidate.candidate_digest != new_digest:
                raise IdentityStoreError("REQUEST_INPUT_CHANGED")
        else:
            if (
                semantic_digest != candidate.semantic_digest
                or _semantic_state(json.loads(candidate.candidate_text))
                != _semantic_state(new_state)
            ):
                raise IdentityStoreError("REQUEST_INPUT_CHANGED")
    else:
        details = store.operation_details(operation_uid)
        if operation.phase == "committed":
            # Legacy committed operations remain read-only: do not create a
            # sidecar on a retry, but still verify the requested typed content.
            if path != store.operation_state_path(operation_uid):
                raise IdentityStoreError("REQUEST_INPUT_CHANGED")
            if semantic_digest is None:
                if new_digest != details["new_state_digest"]:
                    raise IdentityStoreError("REQUEST_INPUT_CHANGED")
            else:
                if semantic_digest != operation.request.input_digest:
                    raise IdentityStoreError("REQUEST_INPUT_CHANGED")
                if current != details["new_state_digest"]:
                    raise IdentityStoreError("STATE_AUTHORITY_MISMATCH")
                published = json.loads(path.read_text(encoding="utf-8"))
                if _semantic_state(published) != _semantic_state(new_state):
                    raise IdentityStoreError("REQUEST_INPUT_CHANGED")
            return recover_operation(store, operation_uid)
        if operation.phase == "intent":
            candidate = store.freeze_operation_candidate(
                operation_uid,
                path,
                candidate_text,
                semantic_digest or operation.request.input_digest,
            )
        else:
            if path != store.operation_state_path(operation_uid):
                raise IdentityStoreError("REQUEST_INPUT_CHANGED")
            if current == details.get("new_state_digest"):
                candidate = _freeze_published_legacy_candidate(
                    store,
                    operation_uid,
                    path,
                    semantic_digest or operation.request.input_digest,
                )
            elif new_digest == details.get("new_state_digest"):
                candidate = store.freeze_operation_candidate(
                    operation_uid,
                    path,
                    candidate_text,
                    semantic_digest or operation.request.input_digest,
                )
            else:
                raise IdentityStoreError("OPERATION_CANDIDATE_UNAVAILABLE")
    if operation.phase == "committed":
        return recover_operation(store, operation_uid)
    current = state_digest(path)
    if current == candidate.candidate_digest:
        store.mark_operation_written(operation_uid)
        return store.finish_operation(operation_uid, candidate.candidate_digest)
    if operation.phase == "file-written" or current != operation.expected_state_digest:
        raise IdentityStoreError("STATE_CAS_MISMATCH")
    from .core import (atomic)
    atomic(path, candidate.candidate_text)
    written = state_digest(path)
    if written != candidate.candidate_digest:
        raise IdentityStoreError("STATE_WRITE_INCOMPLETE")
    store.mark_operation_written(operation_uid)
    return store.finish_operation(operation_uid, candidate.candidate_digest)


def recover_operation(store, operation_uid):
    details = store.operation_details(operation_uid)
    operation = store.get_operation(operation_uid)
    if operation.phase in ("intent", "failed"):
        return operation
    if not details.get("state_path"):
        raise IdentityStoreError("OPERATION_PENDING")
    path = store.operation_state_path(operation_uid)
    current = state_digest(path)
    supports_candidate = operation.request.operation_kind in (
        "project-write",
        "state-write",
    )
    candidate = (
        store.get_operation_candidate(operation_uid) if supports_candidate else None
    )
    if operation.phase == "committed":
        authority_digest = (
            candidate.candidate_digest
            if candidate is not None
            else details.get("new_state_digest")
        )
        if current != authority_digest or current != operation.result_ref:
            raise IdentityStoreError("STATE_AUTHORITY_MISMATCH")
        return operation
    if (
        supports_candidate
        and candidate is None
        and current == details.get("new_state_digest")
    ):
        candidate = _freeze_published_legacy_candidate(
            store,
            operation_uid,
            path,
            operation.request.input_digest,
        )
    if candidate is not None and current == candidate.candidate_digest:
        store.mark_operation_written(operation_uid)
        return store.finish_operation(operation_uid, candidate.candidate_digest)
    if not supports_candidate and current == details.get("new_state_digest"):
        store.mark_operation_written(operation_uid)
        return store.finish_operation(operation_uid, details["new_state_digest"])
    if current == operation.expected_state_digest:
        return operation
    raise IdentityStoreError("OPERATION_PENDING")


def emit_event(store, operation_uid, event):
    return store.store_event(operation_uid, event)


def _validate_binding(store, binding):
    if not isinstance(binding, SessionBinding):
        raise IdentityStoreError("SESSION_REQUIRED")
    if binding.session.project_uid != store.project_uid():
        raise IdentityStoreError("SESSION_PROJECT_MISMATCH")
    if "write" not in binding.capabilities:
        raise IdentityStoreError("SESSION_WRITE_REQUIRED")
    session = store.resolve("session", binding.session.uid, None, write=True)
    if session != binding.session:
        raise IdentityStoreError("SESSION_IDENTITY_MISMATCH")
    work = store.resolve("work", binding.work_uid, None, write=True)
    if work.project_uid != store.project_uid():
        raise IdentityStoreError("SESSION_PROJECT_MISMATCH")
    if binding.controller_generation < 1 or binding.cancel_generation < 1:
        raise IdentityStoreError("CONTROL_VERSION_CHANGED")
    from .session_context import binding_state, canonical_binding_digest
    authoritative = store.read_session_binding(binding.binding_uid)
    if not authoritative:
        raise IdentityStoreError("SESSION_CONTEXT_INVALID")
    if authoritative != canonical_binding_digest(binding):
        raise IdentityStoreError("SESSION_CONTEXT_TAMPERED")
    authoritative_state = store.read_session_state(binding.binding_uid)
    if authoritative_state is None:
        raise IdentityStoreError("SESSION_CONTEXT_INVALID")
    if authoritative_state != binding_state(binding):
        raise IdentityStoreError("SESSION_BINDING_STALE")
    if authoritative_state["cancelled"]:
        raise IdentityStoreError("SESSION_BINDING_STALE")
    return session, work


def _merge_request(binding, path, expected_version, operations):
    inputs = {
        "path": str(path.resolve()).lower(),
        "expected_version": expected_version,
        "operations": [operation.to_dict() for operation in operations],
    }
    input_digest = digest_value(inputs)
    logical_key = "document-merge:" + binding.session.uid + ":" + digest_value({
        "path": str(path.resolve()).lower(),
        "input_digest": input_digest,
    })
    return RequestRef(
        request_uid=digest_value({
            "project_uid": binding.session.project_uid,
            "logical_key": logical_key,
            "operation_kind": "document-merge",
        }),
        logical_key=logical_key,
        input_digest=input_digest,
        project_uid=binding.session.project_uid,
        work_uid=binding.work_uid,
        attempt_uid=None,
        operation_kind="document-merge",
        controller_generation=binding.controller_generation,
        cancel_generation=binding.cancel_generation,
        contract_uid=None,
        contract_version=None,
        contract_digest=None,
    )


def _recover_document_adapter(store, operation_uid, path, is_backlog):
    reason = "journal adapter recovery " + operation_uid
    if is_backlog:
        from .todos import Backlog
        backlog = Backlog(store.root)
        if backlog.transaction.exists():
            backlog.recover(reason, operation_uid=operation_uid,
                            target_path=path)
    else:
        from .documents import Governance
        governance = Governance(store.root)
        if governance.pending.exists():
            governance.recover(operation_uid=operation_uid,
                               target_path=path)


def merge_shared_document(store, binding, path, expected_version, operations):
    _validate_binding(store, binding)
    path = Path(path).resolve()
    try:
        path.relative_to(store.root)
    except ValueError as exc:
        raise IdentityStoreError("DOCUMENT_PATH_ESCAPE") from exc
    if not operations or any(not isinstance(item, DocumentEdit) for item in operations):
        raise IdentityStoreError("DOCUMENT_EDIT_REQUIRED")
    if (
        not isinstance(expected_version, str)
        or re.fullmatch(r"[a-f0-9]{64}", expected_version) is None
    ):
        raise IdentityStoreError("DOCUMENT_VERSION_REQUIRED")
    request = _merge_request(binding, path, expected_version, operations)
    from .workflow_locks import (locked)
    with locked(store.root):
        # Handoff can invalidate a caller after preflight. Recheck at the
        # document write boundary before beginning or recovering an operation.
        _validate_binding(store, binding)
        from .todos import Backlog
        backlog = Backlog(store.root)
        is_backlog = path == backlog.file.resolve()
        if not is_backlog and not path.is_file():
            raise IdentityStoreError("DOCUMENT_NOT_FOUND")
        operation = begin_intent(store, request, state_digest(path))
        if operation.phase == "committed":
            return recover_operation(store, operation.operation_uid)
        if operation.phase == "failed":
            raise IdentityStoreError("OPERATION_FAILED")
        if operation.phase in ("prepared", "file-written"):
            _recover_document_adapter(
                store, operation.operation_uid, path, is_backlog
            )
            if state_digest(path) != operation.expected_state_digest:
                return recover_operation(store, operation.operation_uid)
        prepared = operation.phase in ("prepared", "file-written")
        try:
            if is_backlog:
                plan = backlog.plan_field_merge(expected_version, operations)
                store.prepare_operation(operation.operation_uid, path, plan["new_digest"])
                prepared = True
                backlog.commit(plan["text"], plan["records"],
                               "field merge " + operation.operation_uid,
                               operation_uid=operation.operation_uid)
            else:
                if path.suffix.lower() != ".json":
                    raise IdentityStoreError("DOCUMENT_MERGE_UNSUPPORTED")
                from .documents import Governance
                governance = Governance(store.root)
                plan = governance.plan_field_merge(path, operations)
                candidate = store.freeze_operation_candidate(
                    operation.operation_uid, path, _state_text(plan["record"]),
                    request.input_digest,
                )
                plan["record"] = json.loads(candidate.candidate_text)
                plan["new_digest"] = candidate.candidate_digest
                prepared = True
                governance._commit({plan["relative_path"]: plan["record"]},
                                    operation_uid=operation.operation_uid)
        except Exception as exc:
            if not prepared:
                store.fail_operation(operation.operation_uid, exc)
            raise
        written = state_digest(path)
        if written != plan["new_digest"]:
            raise IdentityStoreError("DOCUMENT_WRITE_INCOMPLETE")
        store.mark_operation_written(operation.operation_uid)
        return store.finish_operation(operation.operation_uid, written)
