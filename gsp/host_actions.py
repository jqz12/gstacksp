"""Durable envelopes for host-owned Codex workspace actions.

The module plans and records host actions.  It never invokes Codex tools.
Only the tool argument schemas needed by native worktrees are accepted.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Mapping

from .core import WorkflowError, atomic
from .identity_store import IdentityStore, IdentityStoreError
from .orchestration_types import RequestRef, SessionBinding, digest_value


CREATE_WORKTREE = "create_worktree"
ARCHIVE_WORKTREE = "archive_worktree"
RESTORE_WORKTREE = "restore_worktree"
ACTIONS = frozenset({CREATE_WORKTREE, ARCHIVE_WORKTREE, RESTORE_WORKTREE})
_SHA1 = re.compile(r"[a-f0-9]{40}")
_DIGEST = re.compile(r"[a-f0-9]{32,64}")
_INTENT_FIELDS = {
    "schema",
    "state",
    "action",
    "request",
    "request_digest",
    "project_uid",
    "work_uid",
    "task_uid",
    "owner",
    "binding_uid",
    "binding_digest",
    "controller_generation",
    "cancel_generation",
    "attachment_id",
    "attachment_identity_key",
    "expected_head",
    "base_sha",
    "common_dir",
    "action_args",
    "evidence_digest",
    "input_digest",
}
_RESULT_FIELDS = {
    "schema",
    "state",
    "action",
    "request_uid",
    "action_digest",
    "operation_id",
    "receipt_digest",
    "attachment_id",
    "attachment_identity_key",
    "head",
    "payload",
}
_TOOL_FIELDS = {
    CREATE_WORKTREE: (
        frozenset({"allowAsync", "ref"}),
        frozenset({"name", "sourceRoot"}),
    ),
    ARCHIVE_WORKTREE: (
        frozenset({"root"}),
        frozenset({"pullRequestIdentityKeys"}),
    ),
    RESTORE_WORKTREE: (frozenset({"root"}), frozenset()),
}


def _invalid(message="HOST_ACTION_INVALID"):
    raise WorkflowError(message)


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _absolute(value):
    if not _nonempty(value):
        return False
    path = Path(value)
    return path.is_absolute() and ".." not in path.parts


def _same_path(left, right):
    try:
        return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(
            str(Path(right).resolve())
        )
    except (OSError, TypeError, ValueError):
        return False


def validate_host_action_args(action, args):
    """Validate one supported Codex tool call without invoking it."""
    if action not in ACTIONS or not isinstance(args, Mapping):
        _invalid("HOST_ACTION_ARGS_INVALID")
    required, optional = _TOOL_FIELDS[action]
    keys = set(args)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        _invalid("HOST_ACTION_ARGS_INVALID fields")
    if action == CREATE_WORKTREE:
        if args.get("allowAsync") is not True:
            _invalid("HOST_ACTION_ARGS_INVALID allowAsync")
        if not _nonempty(args.get("ref")):
            _invalid("HOST_ACTION_ARGS_INVALID ref")
        if "name" in args and not _nonempty(args["name"]):
            _invalid("HOST_ACTION_ARGS_INVALID name")
        if "sourceRoot" in args and not _absolute(args["sourceRoot"]):
            _invalid("HOST_ACTION_ARGS_INVALID sourceRoot")
    elif action == ARCHIVE_WORKTREE:
        if not _absolute(args.get("root")):
            _invalid("HOST_ACTION_ARGS_INVALID root")
        if "pullRequestIdentityKeys" in args:
            values = args["pullRequestIdentityKeys"]
            if (
                not isinstance(values, list)
                or any(not _nonempty(item) for item in values)
                or len(values) != len(set(values))
            ):
                _invalid("HOST_ACTION_ARGS_INVALID pullRequestIdentityKeys")
    elif action == RESTORE_WORKTREE and not _absolute(args.get("root")):
        _invalid("HOST_ACTION_ARGS_INVALID root")
    return dict(args)


def _session_binding(value):
    if isinstance(value, SessionBinding):
        return value
    if isinstance(value, Mapping):
        try:
            return SessionBinding.from_dict(value)
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowError("HOST_ACTION_BINDING_INVALID") from exc
    raise WorkflowError("HOST_ACTION_BINDING_INVALID")


def canonical_binding_digest(binding):
    """Match the immutable digest owned by ``session_context``."""
    binding = _session_binding(binding)
    return digest_value(
        {
            "binding_uid": binding.binding_uid,
            "session_uid": binding.session.uid,
            "work_uid": binding.work_uid,
            "kind": binding.kind,
            "host": binding.host,
            "host_thread_id": binding.host_thread_id,
            "host_state": binding.host_state,
            "capabilities": sorted(binding.capabilities),
            "project_uid": binding.project_uid,
            "common_dir": binding.common_dir,
            "handle_path": binding.handle_path,
        }
    )


def _load_authority(store, binding_uid):
    try:
        immutable = store.read_session_binding(binding_uid)
        state = store.read_session_state(binding_uid)
    except IdentityStoreError as exc:
        raise WorkflowError(str(exc)) from exc
    if immutable is None or state is None:
        raise WorkflowError("HOST_ACTION_BINDING_MISSING")
    return immutable, state


def _require_current_controller(store, work_uid, binding_uid):
    try:
        controller = store.read_work_controller(work_uid)
    except IdentityStoreError as exc:
        raise WorkflowError(str(exc)) from exc
    if controller is None or controller.get("binding_uid") != binding_uid:
        raise WorkflowError("HOST_ACTION_CONTROLLER_STALE")
    return controller


def verify_authoritative_binding(store, project_uid, binding):
    """Return an immutable authority snapshot for one current binding."""
    binding = _session_binding(binding)
    if (
        binding.project_uid != project_uid
        or binding.cancelled
        or binding.controller_generation < 1
        or binding.cancel_generation < 1
    ):
        raise WorkflowError("HOST_ACTION_BINDING_STALE")
    immutable, state = _load_authority(store, binding.binding_uid)
    if immutable != canonical_binding_digest(binding):
        raise WorkflowError("HOST_ACTION_BINDING_TAMPERED")
    if state != {
        "controller_generation": binding.controller_generation,
        "cancel_generation": binding.cancel_generation,
        "cancelled": binding.cancelled,
    } or state["cancelled"]:
        raise WorkflowError("HOST_ACTION_BINDING_STALE")
    _require_current_controller(store, binding.work_uid, binding.binding_uid)
    return {
        "binding_uid": binding.binding_uid,
        "binding_digest": immutable,
        "controller_generation": state["controller_generation"],
        "cancel_generation": state["cancel_generation"],
    }


def revalidate_authority_snapshot(
    store,
    *,
    project_uid,
    work_uid,
    binding_uid,
    binding_digest,
    controller_generation,
    cancel_generation,
):
    immutable, state = _load_authority(store, binding_uid)
    if immutable != binding_digest:
        raise WorkflowError("HOST_ACTION_BINDING_TAMPERED")
    if (
        state["controller_generation"] != controller_generation
        or state["cancel_generation"] != cancel_generation
        or state["cancelled"]
    ):
        raise WorkflowError("HOST_ACTION_BINDING_STALE")
    if not _nonempty(project_uid) or not _nonempty(work_uid):
        raise WorkflowError("HOST_ACTION_BINDING_INVALID")
    _require_current_controller(store, work_uid, binding_uid)


def _input_digest(
    request,
    *,
    project_uid,
    work_uid,
    task_uid,
    owner,
    attachment_id,
    attachment_identity_key,
    expected_head,
    base_sha,
    common_dir,
    action_args,
    evidence_digest,
):
    return digest_value(
        {
            "request": request.to_dict(),
            "project_uid": project_uid,
            "work_uid": work_uid,
            "task_uid": task_uid,
            "owner": owner,
            "attachment_id": attachment_id,
            "attachment_identity_key": attachment_identity_key,
            "expected_head": expected_head,
            "base_sha": base_sha,
            "common_dir": common_dir,
            "action_args": action_args,
            "evidence_digest": evidence_digest,
        }
    )


@dataclass(frozen=True)
class HostAction:
    request: RequestRef
    action: str
    project_uid: str
    work_uid: str
    task_uid: str
    owner: str
    binding_uid: str
    binding_digest: str
    controller_generation: int
    cancel_generation: int
    attachment_id: str | None
    attachment_identity_key: str
    expected_head: str
    base_sha: str
    common_dir: str
    action_args: Mapping
    evidence_digest: str | None
    input_digest: str
    state: str = "pending"
    schema: int = 1

    def to_dict(self):
        return {
            "schema": self.schema,
            "state": self.state,
            "action": self.action,
            "request": self.request.to_dict(),
            "request_digest": digest_value(self.request.to_dict()),
            "project_uid": self.project_uid,
            "work_uid": self.work_uid,
            "task_uid": self.task_uid,
            "owner": self.owner,
            "binding_uid": self.binding_uid,
            "binding_digest": self.binding_digest,
            "controller_generation": self.controller_generation,
            "cancel_generation": self.cancel_generation,
            "attachment_id": self.attachment_id,
            "attachment_identity_key": self.attachment_identity_key,
            "expected_head": self.expected_head,
            "base_sha": self.base_sha,
            "common_dir": self.common_dir,
            "action_args": dict(self.action_args),
            "evidence_digest": self.evidence_digest,
            "input_digest": self.input_digest,
        }

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, Mapping) or set(value) != _INTENT_FIELDS:
            _invalid("HOST_ACTION_INTENT_INVALID fields")
        try:
            request = RequestRef.from_dict(value["request"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowError("HOST_ACTION_INTENT_INVALID request") from exc
        intent = cls(
            schema=value["schema"],
            state=value["state"],
            action=value["action"],
            request=request,
            project_uid=value["project_uid"],
            work_uid=value["work_uid"],
            task_uid=value["task_uid"],
            owner=value["owner"],
            binding_uid=value["binding_uid"],
            binding_digest=value["binding_digest"],
            controller_generation=value["controller_generation"],
            cancel_generation=value["cancel_generation"],
            attachment_id=value["attachment_id"],
            attachment_identity_key=value["attachment_identity_key"],
            expected_head=value["expected_head"],
            base_sha=value["base_sha"],
            common_dir=value["common_dir"],
            action_args=value["action_args"],
            evidence_digest=value["evidence_digest"],
            input_digest=value["input_digest"],
        )
        intent.validate()
        if value["request_digest"] != digest_value(request.to_dict()):
            _invalid("HOST_ACTION_REQUEST_CHANGED")
        return intent

    def validate(self):
        if (
            self.schema != 1
            or self.state != "pending"
            or self.action not in ACTIONS
            or not isinstance(self.request, RequestRef)
            or self.request.project_uid != self.project_uid
            or self.request.work_uid != self.work_uid
            or any(
                not _nonempty(value)
                for value in (
                    self.project_uid,
                    self.work_uid,
                    self.task_uid,
                    self.owner,
                    self.binding_uid,
                    self.binding_digest,
                )
            )
            or _DIGEST.fullmatch(self.binding_digest) is None
            or type(self.controller_generation) is not int
            or self.controller_generation < 1
            or type(self.cancel_generation) is not int
            or self.cancel_generation < 1
            or (
                self.attachment_id is not None
                and not _nonempty(self.attachment_id)
            )
            or not _absolute(self.attachment_identity_key)
            or not _absolute(self.common_dir)
            or _SHA1.fullmatch(self.expected_head) is None
            or _SHA1.fullmatch(self.base_sha) is None
            or (
                self.evidence_digest is not None
                and _DIGEST.fullmatch(self.evidence_digest) is None
            )
        ):
            _invalid("HOST_ACTION_INTENT_INVALID")
        try:
            action_args = validate_host_action_args(
                self.action, self.action_args
            )
        except WorkflowError as exc:
            raise WorkflowError("HOST_ACTION_INTENT_INVALID action_args") from exc
        if self.request.controller_generation is not None and (
            self.request.controller_generation != self.controller_generation
        ):
            _invalid("HOST_ACTION_REQUEST_CHANGED controller_generation")
        if self.request.cancel_generation is not None and (
            self.request.cancel_generation != self.cancel_generation
        ):
            _invalid("HOST_ACTION_REQUEST_CHANGED cancel_generation")
        if self.action == ARCHIVE_WORKTREE and self.evidence_digest is None:
            _invalid("HOST_ACTION_EVIDENCE_REQUIRED")
        expected = _input_digest(
            self.request,
            project_uid=self.project_uid,
            work_uid=self.work_uid,
            task_uid=self.task_uid,
            owner=self.owner,
            attachment_id=self.attachment_id,
            attachment_identity_key=self.attachment_identity_key,
            expected_head=self.expected_head,
            base_sha=self.base_sha,
            common_dir=self.common_dir,
            action_args=action_args,
            evidence_digest=self.evidence_digest,
        )
        if self.input_digest != expected:
            _invalid("HOST_ACTION_REQUEST_CHANGED input_digest")


@dataclass(frozen=True)
class HostActionResult:
    action: str
    request_uid: str
    action_digest: str
    operation_id: str
    receipt_digest: str
    attachment_id: str
    attachment_identity_key: str
    head: str
    payload: Mapping
    state: str = "confirmed"
    schema: int = 1

    def to_dict(self):
        return {
            "schema": self.schema,
            "state": self.state,
            "action": self.action,
            "request_uid": self.request_uid,
            "action_digest": self.action_digest,
            "operation_id": self.operation_id,
            "receipt_digest": self.receipt_digest,
            "attachment_id": self.attachment_id,
            "attachment_identity_key": self.attachment_identity_key,
            "head": self.head,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, Mapping) or set(value) != _RESULT_FIELDS:
            _invalid("HOST_ACTION_RESULT_INVALID fields")
        result = cls(
            schema=value["schema"],
            state=value["state"],
            action=value["action"],
            request_uid=value["request_uid"],
            action_digest=value["action_digest"],
            operation_id=value["operation_id"],
            receipt_digest=value["receipt_digest"],
            attachment_id=value["attachment_id"],
            attachment_identity_key=value["attachment_identity_key"],
            head=value["head"],
            payload=value["payload"],
        )
        result.validate()
        return result

    def validate(self):
        if (
            self.schema != 1
            or self.state != "confirmed"
            or self.action not in ACTIONS
            or any(
                not _nonempty(value)
                for value in (
                    self.request_uid,
                    self.action_digest,
                    self.operation_id,
                    self.receipt_digest,
                    self.attachment_id,
                )
            )
            or not _absolute(self.attachment_identity_key)
            or _SHA1.fullmatch(self.head) is None
            or not isinstance(self.payload, Mapping)
            or _DIGEST.fullmatch(self.action_digest) is None
            or _DIGEST.fullmatch(self.receipt_digest) is None
        ):
            _invalid("HOST_ACTION_RESULT_INVALID")


class HostActionStore:
    """Persist pending intents and confirmed host receipts by request UID."""

    def __init__(self, control_root, expected_project_uid):
        self.root = Path(control_root).resolve()
        self.project_uid = expected_project_uid
        self.meta = self.root / ".gsp" / "orchestration"
        self.intent_dir = self.meta / "host-actions"
        self.result_dir = self.meta / "host-action-results"
        self.identity = IdentityStore(self.root)

    def _intent_path(self, request_uid):
        if not _nonempty(request_uid) or Path(request_uid).name != request_uid:
            raise WorkflowError("HOST_ACTION_REQUEST_INVALID")
        return self.intent_dir / (request_uid + ".json")

    def _result_path(self, request_uid):
        if not _nonempty(request_uid) or Path(request_uid).name != request_uid:
            raise WorkflowError("HOST_ACTION_REQUEST_INVALID")
        return self.result_dir / (request_uid + ".json")

    def authority(self, binding):
        return verify_authoritative_binding(
            self.identity, self.project_uid, binding
        )

    def revalidate(self, intent):
        revalidate_authority_snapshot(
            self.identity,
            project_uid=intent.project_uid,
            work_uid=intent.work_uid,
            binding_uid=intent.binding_uid,
            binding_digest=intent.binding_digest,
            controller_generation=intent.controller_generation,
            cancel_generation=intent.cancel_generation,
        )

    def plan(self, intent):
        intent.validate()
        path = self._intent_path(intent.request.request_uid)
        if path.is_file():
            existing = self.load(intent.request.request_uid)
            if existing.to_dict() != intent.to_dict():
                raise WorkflowError("HOST_ACTION_REQUEST_CHANGED")
            return existing
        atomic(path, intent.to_dict())
        return self.load(intent.request.request_uid)

    def load(self, request_uid):
        path = self._intent_path(request_uid)
        if not path.is_file():
            raise WorkflowError("HOST_ACTION_PENDING_MISSING")
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise WorkflowError("HOST_ACTION_INTENT_INVALID") from exc
        intent = HostAction.from_dict(value)
        if intent.request.request_uid != request_uid:
            raise WorkflowError("HOST_ACTION_INTENT_INVALID request path")
        return intent

    def result(self, request_uid):
        path = self._result_path(request_uid)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise WorkflowError("HOST_ACTION_RESULT_INVALID") from exc
        result = HostActionResult.from_dict(value)
        if result.request_uid != request_uid:
            raise WorkflowError("HOST_ACTION_RESULT_INVALID request path")
        return result

    def _publish(self, result):
        """Publish one complete result without replacing an existing result."""
        target = self._result_path(result.request_uid)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=target.name + ".", dir=target.parent
        )
        temporary = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(result.to_dict(), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        published = self.result(result.request_uid)
        if published is None:
            raise WorkflowError("HOST_ACTION_RESULT_PUBLISH_UNKNOWN")
        return published

    def confirm(self, intent, result):
        intent.validate()
        result.validate()
        self.revalidate(intent)
        if (
            result.action != intent.action
            or result.request_uid != intent.request.request_uid
            or result.action_digest != intent.input_digest
            or (intent.action!=CREATE_WORKTREE and result.attachment_id != intent.attachment_id)
            or (intent.action!=CREATE_WORKTREE and not _same_path(
                result.attachment_identity_key,
                intent.attachment_identity_key,
            ))
            or result.head != intent.expected_head
        ):
            raise WorkflowError("HOST_ACTION_RESULT_CHANGED")
        existing = self.result(intent.request.request_uid)
        if existing is not None:
            if existing.to_dict() != result.to_dict():
                raise WorkflowError("HOST_ACTION_RESULT_CHANGED")
            return existing
        published = self._publish(result)
        if published.to_dict() != result.to_dict():
            raise WorkflowError("HOST_ACTION_RESULT_CHANGED")
        return published


def host_action_from_parts(
    *,
    request,
    action,
    project_uid,
    task_uid,
    owner,
    authority,
    attachment_id,
    attachment_identity_key,
    expected_head,
    base_sha,
    common_dir,
    action_args,
    evidence_digest=None,
):
    if not isinstance(request, RequestRef):
        raise WorkflowError("HOST_ACTION_REQUEST_INVALID")
    action_args = validate_host_action_args(action, action_args)
    intent = HostAction(
        request=request,
        action=action,
        project_uid=project_uid,
        work_uid=request.work_uid or "",
        task_uid=task_uid,
        owner=owner,
        binding_uid=authority["binding_uid"],
        binding_digest=authority["binding_digest"],
        controller_generation=authority["controller_generation"],
        cancel_generation=authority["cancel_generation"],
        attachment_id=attachment_id,
        attachment_identity_key=str(Path(attachment_identity_key).resolve()),
        expected_head=expected_head,
        base_sha=base_sha,
        common_dir=str(Path(common_dir).resolve()),
        action_args=action_args,
        evidence_digest=evidence_digest,
        input_digest=_input_digest(
            request,
            project_uid=project_uid,
            work_uid=request.work_uid or "",
            task_uid=task_uid,
            owner=owner,
            attachment_id=attachment_id,
            attachment_identity_key=str(
                Path(attachment_identity_key).resolve()
            ),
            expected_head=expected_head,
            base_sha=base_sha,
            common_dir=str(Path(common_dir).resolve()),
            action_args=action_args,
            evidence_digest=evidence_digest,
        ),
    )
    intent.validate()
    return intent


def parse_action_receipt(receipt, intent):
    """Return validated action and attachment payload from an MCP receipt."""
    if receipt is None:
        raise WorkflowError("HOST_ACTION_RECEIPT_REQUIRED")
    if not isinstance(receipt, Mapping) or set(receipt) != {
        "schema",
        "kind",
        "request_uid",
        "operation_id",
        "action_status",
        "attachments_after_action",
    }:
        raise WorkflowError("HOST_ACTION_RECEIPT_INVALID")
    if (
        receipt.get("schema") != 1
        or receipt.get("kind") != "observed-native-tool-receipts"
        or receipt.get("request_uid") != intent.request.request_uid
    ):
        raise WorkflowError("HOST_ACTION_RECEIPT_REQUEST_MISMATCH")

    def payload(key):
        envelope = receipt.get(key)
        value = envelope.get("value") if isinstance(envelope, Mapping) else None
        if (
            not isinstance(envelope, Mapping)
            or envelope.get("status") != "fulfilled"
            or not isinstance(value, Mapping)
            or value.get("isError") is not False
            or not isinstance(value.get("content"), list)
        ):
            raise WorkflowError("HOST_ACTION_RECEIPT_INVALID " + key)
        for item in value["content"]:
            if not isinstance(item, Mapping) or item.get("type") != "text":
                continue
            try:
                parsed = json.loads(item.get("text") or "")
            except (TypeError, ValueError) as exc:
                raise WorkflowError("HOST_ACTION_RECEIPT_INVALID " + key) from exc
            if isinstance(parsed, Mapping):
                return parsed
        raise WorkflowError("HOST_ACTION_RECEIPT_INVALID " + key)

    action = payload("action_status")
    attachments = payload("attachments_after_action")
    operation_id = str(action.get("operationId") or "")
    result = action.get("result")
    rows = attachments.get("attachments")
    if (
        action.get("stage") != "completed"
        or not operation_id
        or receipt.get("operation_id") != operation_id
        or not isinstance(result, Mapping)
        or not isinstance(rows, list)
    ):
        raise WorkflowError("HOST_ACTION_RECEIPT_INVALID")
    return operation_id, result, rows


def public_result(result):
    if not isinstance(result, HostActionResult):
        raise WorkflowError("HOST_ACTION_RESULT_INVALID")
    value = result.to_dict()
    payload = value.pop("payload")
    return {**value, **dict(payload)}


def check_control_project(control_root, expected_project_uid):
    marker = Path(control_root) / ".gsp" / "project.json"
    try:
        value = json.loads(marker.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise WorkflowError("CODEX_CONTROL_MARKER_INVALID") from exc
    if value.get("id") != expected_project_uid:
        raise WorkflowError("CODEX_CONTROL_PROJECT_UID_MISMATCH")


__all__ = [
    "ARCHIVE_WORKTREE",
    "CREATE_WORKTREE",
    "RESTORE_WORKTREE",
    "HostAction",
    "HostActionResult",
    "HostActionStore",
    "canonical_binding_digest",
    "check_control_project",
    "host_action_from_parts",
    "parse_action_receipt",
    "public_result",
    "revalidate_authority_snapshot",
    "validate_host_action_args",
    "verify_authoritative_binding",
]
