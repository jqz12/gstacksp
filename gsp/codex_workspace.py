"""Codex-native workspace intents without host-runtime ownership.

The adapter records what the host must do and verifies the host's observed
worktree receipt.  It never creates, archives, releases or removes a workspace.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Mapping

from .core import WorkflowError
from .git_topology import (
    WorkspaceExpectation,
    verify_registered_native_workspace,
)
from .host_actions import (
    ARCHIVE_WORKTREE,
    CREATE_WORKTREE,
    RESTORE_WORKTREE,
    HostAction,
    HostActionResult,
    HostActionStore,
    check_control_project,
    host_action_from_parts,
    parse_action_receipt,
    public_result,
)
from .orchestration_types import (
    RequestRef,
    SessionBinding,
    WorktreeRequest,
    digest_value,
)
from .worktree_adapters import WorktreeAdapter, WorktreeAdapterError


OWNER_ADAPTER = "codex-native-worktree"
CREATE_KIND = "worktree-create"
ARCHIVE_KIND = "worktree-archive"
_SHA1 = re.compile(r"[a-f0-9]{40}")
_DIGEST = re.compile(r"[a-f0-9]{32,64}")


def _same_path(left, right):
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(
        str(Path(right).resolve())
    )


def _path(value, code):
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(code)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise WorkflowError(code)
    return path.resolve()


def _branch(value):
    if (
        not isinstance(value, str)
        or not value.startswith("codex/")
        or any(ord(char) < 32 for char in value)
        or any(char in value for char in (" ", "\\", "@{", "..", "~", "^", ":", "?", "*", "[", "\x7f"))
        or "//" in value
        or value.endswith((".", "/", ".lock"))
    ):
        raise WorkflowError("CODEX_WORKSPACE_BRANCH_INVALID")
    return value


def _request_ref(value):
    if not isinstance(value, Mapping):
        raise WorkflowError("CODEX_WORKSPACE_REQUEST_INVALID")
    try:
        return RequestRef.from_dict(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowError("CODEX_WORKSPACE_REQUEST_INVALID") from exc


def _worktree_request(value):
    if isinstance(value, WorktreeRequest):
        return value
    if not isinstance(value, Mapping):
        raise WorkflowError("CODEX_WORKSPACE_REQUEST_INVALID")
    try:
        return WorktreeRequest(
            request=_request_ref(value["request"]),
            owner_adapter=value["owner_adapter"],
            source_root=value["source_root"],
            base_sha=value["base_sha"],
            branch=value["branch"],
            path_hint=value.get("path_hint"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowError("CODEX_WORKSPACE_REQUEST_INVALID") from exc


def _session_binding(value):
    if isinstance(value, SessionBinding):
        return value
    if not isinstance(value, Mapping):
        raise WorkflowError("CODEX_WORKSPACE_BINDING_REQUIRED")
    try:
        return SessionBinding.from_dict(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowError("CODEX_WORKSPACE_BINDING_INVALID") from exc


def _git(cwd, *argv):
    try:
        return subprocess.run(
            ["git", *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            ),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkflowError("CODEX_WORKSPACE_GIT_UNVERIFIED") from exc


@dataclass(frozen=True)
class CodexCreateIntent:
    """Stable input for one host-owned worktree creation."""

    request: WorktreeRequest
    project_uid: str
    task_uid: str
    owner: str
    control_root: str
    common_dir: str
    request_digest: str
    kind: str = CREATE_KIND
    state: str = "pending"
    schema: int = 1

    def to_dict(self):
        return {
            **asdict(self),
            "request": self.request.to_dict(),
        }

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, Mapping):
            raise WorkflowError("CODEX_WORKSPACE_INTENT_INVALID")
        expected = {
            "schema",
            "kind",
            "state",
            "request",
            "request_digest",
            "project_uid",
            "task_uid",
            "owner",
            "control_root",
            "common_dir",
        }
        if set(value) != expected:
            raise WorkflowError("CODEX_WORKSPACE_INTENT_INVALID fields")
        if (
            value.get("schema") != 1
            or value.get("kind") != CREATE_KIND
            or value.get("state") != "pending"
        ):
            raise WorkflowError("CODEX_WORKSPACE_INTENT_INVALID state")
        request = _worktree_request(value["request"])
        if value.get("request_digest") != digest_value(request.to_dict()):
            raise WorkflowError("CODEX_WORKSPACE_REQUEST_CHANGED")
        intent = cls(
            request=request,
            project_uid=value["project_uid"],
            task_uid=value["task_uid"],
            owner=value["owner"],
            control_root=value["control_root"],
            common_dir=value["common_dir"],
            request_digest=value["request_digest"],
            kind=value["kind"],
            state=value["state"],
            schema=value["schema"],
        )
        intent.validate()
        return intent

    def validate(self):
        request = self.request
        if (
            self.schema != 1
            or self.kind != CREATE_KIND
            or self.state != "pending"
            or not isinstance(request, WorktreeRequest)
            or not isinstance(request.request, RequestRef)
            or self.request_digest != digest_value(request.to_dict())
        ):
            raise WorkflowError("CODEX_WORKSPACE_INTENT_INVALID")
        for name in ("project_uid", "task_uid", "owner", "control_root", "common_dir"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise WorkflowError("CODEX_WORKSPACE_INTENT_INVALID " + name)


class CodexWorkspaceAdapter:
    """Plan and verify Codex worktrees using host receipts and Git facts."""

    def __init__(self, control_root, expected_project_uid):
        path = _path(str(control_root), "CODEX_CONTROL_ROOT_INVALID")
        if not path.is_dir():
            raise WorkflowError("CODEX_CONTROL_ROOT_MISSING")
        if (
            not isinstance(expected_project_uid, str)
            or not expected_project_uid.strip()
        ):
            raise WorkflowError("CODEX_PROJECT_UID_REQUIRED")
        self.control_root = path
        self.project_uid = expected_project_uid
        self.host_actions = HostActionStore(
            self.control_root, expected_project_uid
        )

    def _control(self):
        marker = self.control_root / ".gsp" / "project.json"
        if not marker.is_file():
            raise WorkflowError("CODEX_CONTROL_MARKER_MISSING")
        try:
            identity = json.loads(marker.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise WorkflowError("CODEX_CONTROL_MARKER_INVALID") from exc
        if identity.get("id") != self.project_uid:
            raise WorkflowError("CODEX_CONTROL_PROJECT_UID_MISMATCH")
        return WorktreeAdapter(
            self.control_root, expected_project_uid=self.project_uid
        ).inspect(self.control_root)

    def plan_create(
        self,
        request,
        *,
        task_uid,
        owner,
        actor_binding=None,
    ):
        return self._plan_create(
            request,
            task_uid=task_uid,
            owner=owner,
            actor_binding=actor_binding,
            persist=True,
        )

    def preview_create(self, request, *, task_uid, owner):
        """Build a non-persisted create intent for read-only inspection."""
        return self._plan_create(
            request,
            task_uid=task_uid,
            owner=owner,
            actor_binding=None,
            persist=False,
        )

    def _plan_create(
        self,
        request,
        *,
        task_uid,
        owner,
        actor_binding,
        persist,
    ):
        request = _worktree_request(request)
        if not isinstance(request.request, RequestRef):
            raise WorkflowError("CODEX_WORKSPACE_REQUEST_INVALID")
        if request.request.project_uid != self.project_uid:
            raise WorkflowError("CODEX_WORKSPACE_PROJECT_MISMATCH")
        if not isinstance(request.request.work_uid, str) or not request.request.work_uid.strip():
            raise WorkflowError("CODEX_WORKSPACE_WORK_UID_REQUIRED")
        if request.request.operation_kind != "native-worktree-create":
            raise WorkflowError("CODEX_WORKSPACE_OPERATION_INVALID")
        if request.owner_adapter != OWNER_ADAPTER:
            raise WorkflowError("CODEX_WORKSPACE_OWNER_ADAPTER_INVALID")
        if not _same_path(request.source_root, self.control_root):
            raise WorkflowError("CODEX_WORKSPACE_CONTROL_ROOT_MISMATCH")
        if _SHA1.fullmatch(str(request.base_sha)) is None:
            raise WorkflowError("CODEX_WORKSPACE_BASE_INVALID")
        _branch(request.branch)
        if request.path_hint is not None:
            _path(request.path_hint, "CODEX_WORKSPACE_PATH_INVALID")
        for name, value in (("task_uid", task_uid), ("owner", owner)):
            if not isinstance(value, str) or not value.strip():
                raise WorkflowError("CODEX_WORKSPACE_" + name.upper() + "_REQUIRED")
        control = self._control()
        if control.get("project_uid") != self.project_uid:
            raise WorkflowError("CODEX_CONTROL_PROJECT_UID_MISMATCH")
        intent = CodexCreateIntent(
            request=request,
            project_uid=self.project_uid,
            task_uid=task_uid,
            owner=owner,
            control_root=str(self.control_root),
            common_dir=str(control["common_dir"]),
            request_digest=digest_value(request.to_dict()),
        )
        intent.validate()
        if persist and actor_binding is None:
            raise WorkflowError("HOST_ACTION_BINDING_REQUIRED")
        if persist:
            binding = _session_binding(actor_binding)
            if (
                binding.project_uid != self.project_uid
                or binding.work_uid != request.request.work_uid
            ):
                raise WorkflowError("HOST_ACTION_BINDING_MISMATCH")
            if "write" not in binding.capabilities:
                raise WorkflowError("HOST_ACTION_CAPABILITY_REQUIRED write")
            authority = self.host_actions.authority(binding)
            args = {
                "allowAsync": True,
                "ref": request.base_sha,
                "sourceRoot": str(self.control_root),
            }
            if request.path_hint is not None:
                args["name"] = Path(request.path_hint).name
            host_intent = host_action_from_parts(
                request=request.request,
                action=CREATE_WORKTREE,
                project_uid=self.project_uid,
                task_uid=task_uid,
                owner=owner,
                authority=authority,
                attachment_id=None,
                attachment_identity_key=str(self.control_root),
                expected_head=request.base_sha,
                base_sha=request.base_sha,
                common_dir=str(control["common_dir"]),
                action_args=args,
            )
            self.host_actions.plan(host_intent)
        return intent

    def verify_create(
        self,
        intent,
        receipt,
        source_root,
        *,
        expected_head=None,
        allow_dirty=False,
    ):
        if isinstance(intent, Mapping):
            intent = CodexCreateIntent.from_dict(intent)
        if not isinstance(intent, CodexCreateIntent):
            raise WorkflowError("CODEX_WORKSPACE_INTENT_REQUIRED")
        intent.validate()
        if (
            intent.project_uid != self.project_uid
            or not _same_path(intent.control_root, self.control_root)
        ):
            raise WorkflowError("CODEX_WORKSPACE_CONTROL_ROOT_MISMATCH")
        if intent.request_digest != digest_value(intent.request.to_dict()):
            raise WorkflowError("CODEX_WORKSPACE_REQUEST_CHANGED")
        try:
            pending = self.host_actions.load(
                intent.request.request.request_uid
            )
        except WorkflowError as exc:
            if str(exc) == "HOST_ACTION_PENDING_MISSING":
                raise WorkflowError("HOST_ACTION_BINDING_REQUIRED") from exc
            raise
        if (
            pending.action != CREATE_WORKTREE
            or pending.request.to_dict() != intent.request.request.to_dict()
            or pending.project_uid != intent.project_uid
            or pending.work_uid != intent.request.request.work_uid
            or pending.task_uid != intent.task_uid
            or pending.owner != intent.owner
            or pending.expected_head != intent.request.base_sha
            or pending.base_sha != intent.request.base_sha
            or not _same_path(pending.common_dir, intent.common_dir)
        ):
            raise WorkflowError("HOST_ACTION_REQUEST_CHANGED")
        self.host_actions.revalidate(pending)
        source = _path(str(source_root), "CODEX_SOURCE_ROOT_INVALID")
        if intent.request.path_hint is not None and not _same_path(
            intent.request.path_hint, source
        ):
            raise WorkflowError("CODEX_WORKSPACE_SOURCE_PATH_MISMATCH")
        control = self._control()
        if not _same_path(intent.common_dir, control["common_dir"]):
            raise WorkflowError("CODEX_WORKSPACE_COMMON_DIR_CHANGED")
        head = expected_head or intent.request.base_sha
        if _SHA1.fullmatch(str(head)) is None:
            raise WorkflowError("CODEX_WORKSPACE_HEAD_INVALID")
        expected = WorkspaceExpectation(
            project_uid=intent.project_uid,
            work_uid=intent.request.request.work_uid,
            task_uid=intent.task_uid,
            owner=intent.owner,
            owner_adapter=intent.request.owner_adapter,
            base_sha=intent.request.base_sha,
            head_sha=head,
            branch=intent.request.branch,
            request_uid=intent.request.request.request_uid,
            request_digest=intent.request_digest,
            receipt_digest=digest_value(receipt),
            control_root=intent.control_root,
            source_root=str(source),
            common_dir=intent.common_dir,
        )
        proof = verify_registered_native_workspace(
            self.control_root,
            source,
            intent.request,
            receipt,
            expected,
            allow_dirty=allow_dirty,
            allow_detached=True,
        )
        binding_uid = digest_value(
            {
                "project_uid": proof.project_uid,
                "request_uid": proof.request_uid,
                "operation_id": proof.native_operation_id,
                "attachment_id": proof.attachment_id,
                "git_dir": proof.git_dir,
            }
        )[:32]
        binding = {
            "schema": 1,
            "ownership": "native",
            "binding_uid": binding_uid,
            "project_uid": proof.project_uid,
            "work_uid": proof.work_uid,
            "task_uid": proof.task_uid,
            "owner": proof.owner,
            "owner_adapter": proof.owner_adapter,
            "request_uid": proof.request_uid,
            "path": proof.source_root,
            "control_root": proof.control_root,
            "common_dir": proof.common_dir,
            "branch": proof.branch,
            "base_sha": proof.base_sha,
            "head": proof.head,
            "registered": proof.registered,
            "cleanup_action": "archive",
            "deletable": False,
            "verification": proof.to_dict(),
        }
        existing=self.host_actions.result(pending.request.request_uid)
        if existing is not None:
            previous=dict(existing.payload.get('binding') or {})
            old_proof=dict(previous.get('verification') or {})
            new_proof=dict(binding['verification'])
            old_proof.pop('verified_at',None)
            new_proof.pop('verified_at',None)
            if (old_proof!=new_proof or existing.receipt_digest!=digest_value(receipt)
                    or existing.action!=CREATE_WORKTREE):
                raise WorkflowError('HOST_ACTION_RESULT_CHANGED')
            return previous
        result=HostActionResult(action=CREATE_WORKTREE,request_uid=pending.request.request_uid,
            action_digest=pending.input_digest,operation_id=proof.native_operation_id,
            receipt_digest=digest_value(receipt),attachment_id=proof.attachment_id,
            attachment_identity_key=str(source),head=pending.expected_head,payload={'binding':binding})
        self.host_actions.confirm(pending,result)
        return binding

    def bind_existing(self, path, *, ownership, binding_uid=None):
        if ownership == "native":
            raise WorktreeAdapterError("CODEX_NATIVE_RECEIPT_REQUIRED")
        binding = WorktreeAdapter(
            self.control_root, expected_project_uid=self.project_uid
        ).bind(path, ownership=ownership, binding_uid=binding_uid)
        return {
            **binding,
            "owner_adapter": OWNER_ADAPTER,
            "verification": {"registered": True, "source": "git-worktree"},
        }

    def host_action(self, request_uid):
        return self.host_actions.load(request_uid)

    def host_action_result(self, request_uid):
        return self.host_actions.result(request_uid)

    def _actor_authority(self, actor_binding, request):
        binding = _session_binding(actor_binding)
        if (
            binding.project_uid != self.project_uid
            or binding.work_uid != request.request.work_uid
        ):
            raise WorkflowError("HOST_ACTION_BINDING_MISMATCH")
        return self.host_actions.authority(binding)

    def _native_binding_parts(self, binding):
        if not isinstance(binding, Mapping):
            raise WorkflowError("CODEX_WORKSPACE_BINDING_INVALID")
        ownership = binding.get("ownership")
        verification = binding.get("verification")
        if (
            ownership != "native"
            or binding.get("owner_adapter") != OWNER_ADAPTER
            or binding.get("cleanup_action") != "archive"
            or binding.get("deletable") is not False
            or binding.get("registered") is not True
            or not isinstance(verification, Mapping)
            or verification.get("registered") is not True
        ):
            raise WorkflowError("CODEX_NATIVE_RECEIPT_REQUIRED")
        try:
            path = _path(binding.get("path"), "CODEX_WORKSPACE_PATH_INVALID")
            common = _path(
                binding.get("common_dir"), "CODEX_WORKSPACE_COMMON_DIR_INVALID"
            )
        except WorkflowError as exc:
            raise WorkflowError("CODEX_NATIVE_RECEIPT_REQUIRED") from exc
        expected_binding = digest_value(
            {
                "project_uid": verification.get("project_uid"),
                "request_uid": verification.get("request_uid"),
                "operation_id": verification.get("native_operation_id"),
                "attachment_id": verification.get("attachment_id"),
                "git_dir": verification.get("git_dir"),
            }
        )[:32]
        if (
            binding.get("binding_uid") != expected_binding
            or binding.get("project_uid") != verification.get("project_uid")
            or binding.get("request_uid") != verification.get("request_uid")
            or not _same_path(path, verification.get("source_root"))
            or not _same_path(common, verification.get("common_dir"))
            or binding.get("branch") != verification.get("branch")
            or binding.get("head") != verification.get("head")
            or not isinstance(verification.get("attachment_id"), str)
            or not verification.get("attachment_id").strip()
        ):
            raise WorkflowError("CODEX_NATIVE_RECEIPT_REQUIRED")
        return path, common, {**verification, "attachment_identity_key": str(path)}

    def _inspect_native_binding(self, binding):
        path, common, _ = self._native_binding_parts(binding)
        if not path.is_dir():
            raise WorkflowError("CODEX_WORKSPACE_MISSING")
        try:
            current = WorktreeAdapter(
                self.control_root, expected_project_uid=self.project_uid
            ).inspect(path)
        except WorktreeAdapterError as exc:
            raise WorkflowError(str(exc)) from exc
        if (
            current.get("registered") is not True
            or not _same_path(current.get("common_dir"), common)
            or current.get("project_uid") != self.project_uid
            or current.get("branch") != binding.get("branch")
        ):
            raise WorkflowError("CODEX_WORKSPACE_GIT_IDENTITY_CHANGED")
        if current.get("head") != binding.get("head"):
            raise WorkflowError("CODEX_WORKSPACE_HEAD_DRIFT")
        return current

    def _common_dir(self):
        result = _git(self.control_root, "rev-parse", "--git-common-dir")
        if result.returncode or not result.stdout.strip():
            raise WorkflowError("CODEX_WORKSPACE_GIT_UNVERIFIED")
        raw = Path(result.stdout.strip())
        return (
            raw.resolve()
            if raw.is_absolute()
            else (self.control_root / raw).resolve()
        )

    def _registered_worktrees(self):
        result = _git(
            self.control_root, "worktree", "list", "--porcelain", "-z"
        )
        if result.returncode:
            raise WorkflowError("CODEX_WORKSPACE_GIT_UNVERIFIED")
        rows = []
        current = None
        for line in result.stdout.split("\0"):
            if not line:
                if current is not None:
                    rows.append(current)
                    current = None
                continue
            key, _, value = line.partition(" ")
            if key == "worktree":
                if current is not None:
                    rows.append(current)
                current = {"path": value}
            elif current is not None:
                current[key] = value
        if current is not None:
            rows.append(current)
        return rows

    def _archive_facts(self, intent):
        check_control_project(self.control_root, self.project_uid)
        if not _same_path(self._common_dir(), intent.common_dir):
            raise WorkflowError("CODEX_WORKSPACE_COMMON_DIR_CHANGED")
        path = Path(intent.attachment_identity_key)
        if path.exists():
            raise WorkflowError("CODEX_WORKSPACE_PRESENT")
        if any(
            row.get("path") and _same_path(row["path"], path)
            for row in self._registered_worktrees()
        ):
            raise WorkflowError("CODEX_WORKSPACE_STILL_REGISTERED")
        return {
            "registered": False,
            "workspace_present": False,
            "common_dir": str(self._common_dir()),
        }

    def _action_receipt(
        self,
        intent,
        receipt,
        *,
        result_type,
        attachment_state,
    ):
        operation_id, result, rows = parse_action_receipt(receipt, intent)
        if result.get("type") != result_type:
            raise WorkflowError("HOST_ACTION_RECEIPT_RESULT_MISMATCH")
        if result.get("attachmentId") != intent.attachment_id:
            raise WorkflowError("HOST_ACTION_RECEIPT_ATTACHMENT_MISMATCH")
        if (
            not result.get("identityKey")
            or not _same_path(
                result["identityKey"], intent.attachment_identity_key
            )
        ):
            raise WorkflowError("HOST_ACTION_RECEIPT_ATTACHMENT_MISMATCH")
        if result.get("head") != intent.expected_head:
            raise WorkflowError("CODEX_WORKSPACE_HEAD_DRIFT")
        matches = []
        for row in rows:
            payload = row.get("payload") if isinstance(row, Mapping) else None
            if not isinstance(payload, Mapping):
                continue
            if (
                row.get("id") == intent.attachment_id
                and row.get("attachmentType") == "worktree"
                and row.get("state") == attachment_state
                and row.get("identityKey")
                and _same_path(
                    row["identityKey"], intent.attachment_identity_key
                )
                and payload.get("hostId") == intent.owner
                and payload.get("root")
                and payload.get("workspaceRoot")
                and payload.get("sourceCwd")
                and _same_path(
                    payload["root"], intent.attachment_identity_key
                )
                and _same_path(
                    payload["workspaceRoot"], intent.attachment_identity_key
                )
                and _same_path(payload["sourceCwd"], self.control_root)
            ):
                matches.append(row)
        if len(matches) != 1:
            raise WorkflowError("HOST_ACTION_RECEIPT_ATTACHMENT_MISMATCH")
        return operation_id

    def plan_archive(
        self,
        binding,
        *,
        request,
        actor_binding,
        owner,
        evidence_digest,
        pull_request_identity_keys=None,
    ):
        request = _worktree_request(request)
        if (
            not isinstance(request.request, RequestRef)
            or request.request.operation_kind != "native-worktree-archive"
            or request.request.project_uid != self.project_uid
            or request.request.work_uid != binding.get("work_uid")
            or not _same_path(request.source_root, self.control_root)
        ):
            raise WorkflowError("CODEX_WORKSPACE_REQUEST_INVALID")
        if not isinstance(owner, str) or owner != binding.get("owner"):
            raise WorkflowError("CODEX_WORKSPACE_OWNER_MISMATCH")
        if (
            not isinstance(evidence_digest, str)
            or _DIGEST.fullmatch(evidence_digest) is None
        ):
            raise WorkflowError("HOST_ACTION_EVIDENCE_REQUIRED")
        authority = self._actor_authority(actor_binding, request)
        path, common, verification = self._native_binding_parts(binding)
        if request.path_hint is not None and not _same_path(
            request.path_hint, path
        ):
            raise WorkflowError("CODEX_WORKSPACE_SOURCE_PATH_MISMATCH")
        self._inspect_native_binding(binding)
        args = {"root": str(path)}
        if pull_request_identity_keys is not None:
            args["pullRequestIdentityKeys"] = list(
                pull_request_identity_keys
            )
        intent = host_action_from_parts(
            request=request.request,
            action=ARCHIVE_WORKTREE,
            project_uid=self.project_uid,
            task_uid=binding.get("task_uid") or "",
            owner=owner,
            authority=authority,
            attachment_id=verification["attachment_id"],
            attachment_identity_key=str(path),
            expected_head=binding["head"],
            base_sha=binding["base_sha"],
            common_dir=str(common),
            action_args=args,
            evidence_digest=evidence_digest,
        )
        return self.host_actions.plan(intent)

    def confirm_archive(self, intent, receipt):
        if isinstance(intent, Mapping):
            intent = HostAction.from_dict(intent)
        if not isinstance(intent, HostAction):
            raise WorkflowError("HOST_ACTION_INTENT_REQUIRED")
        if intent.action != ARCHIVE_WORKTREE:
            raise WorkflowError("HOST_ACTION_ACTION_MISMATCH")
        persisted = self.host_actions.load(intent.request.request_uid)
        if persisted.to_dict() != intent.to_dict():
            raise WorkflowError("HOST_ACTION_REQUEST_CHANGED")
        self.host_actions.revalidate(intent)
        existing = self.host_actions.result(intent.request.request_uid)
        if existing is not None:
            if (existing.action_digest != intent.input_digest
                    or existing.receipt_digest != digest_value(receipt)):
                raise WorkflowError("HOST_ACTION_RESULT_CHANGED")
            return public_result(existing)
        operation_id = self._action_receipt(
            intent,
            receipt,
            result_type="archived",
            attachment_state="archived",
        )
        facts = self._archive_facts(intent)
        archived_binding = {
            "schema": 1,
            "ownership": "native",
            "binding_uid": intent.binding_uid,
            "project_uid": intent.project_uid,
            "work_uid": intent.work_uid,
            "task_uid": intent.task_uid,
            "owner": intent.owner,
            "owner_adapter": OWNER_ADAPTER,
            "request_uid": intent.request.request_uid,
            "path": intent.attachment_identity_key,
            "control_root": str(self.control_root),
            "common_dir": intent.common_dir,
            "branch": None,
            "base_sha": intent.base_sha,
            "head": intent.expected_head,
            "registered": False,
            "cleanup_action": "archive",
            "deletable": False,
            "attachment_id": intent.attachment_id,
            "attachment_identity_key": intent.attachment_identity_key,
            "verification": {
                **facts,
                "attachment_id": intent.attachment_id,
                "attachment_identity_key": intent.attachment_identity_key,
            },
        }
        result = HostActionResult(
            action=ARCHIVE_WORKTREE,
            request_uid=intent.request.request_uid,
            action_digest=intent.input_digest,
            operation_id=operation_id,
            receipt_digest=digest_value(receipt),
            attachment_id=intent.attachment_id,
            attachment_identity_key=intent.attachment_identity_key,
            head=intent.expected_head,
            payload={"binding": archived_binding, "git_facts": facts},
        )
        confirmed = self.host_actions.confirm(intent, result)
        return public_result(confirmed)

    def plan_restore(
        self,
        archived,
        *,
        request,
        actor_binding,
        owner,
    ):
        request = _worktree_request(request)
        archived_uid = (
            archived.request_uid
            if isinstance(archived, HostActionResult)
            else archived.get("request_uid")
            if isinstance(archived, Mapping)
            else None
        )
        if not archived_uid:
            raise WorkflowError("HOST_ACTION_ARCHIVE_REQUIRED")
        archived_result = self.host_actions.result(archived_uid)
        archived_intent = self.host_actions.load(archived_uid)
        if (
            archived_result is None
            or archived_result.action != ARCHIVE_WORKTREE
            or archived_result.state != "confirmed"
            or archived_intent.action != ARCHIVE_WORKTREE
        ):
            raise WorkflowError("HOST_ACTION_ARCHIVE_REQUIRED")
        if (
            request.request.operation_kind != "native-worktree-restore"
            or request.request.project_uid != self.project_uid
            or request.request.work_uid != archived_intent.work_uid
            or not _same_path(request.source_root, self.control_root)
            or (
                request.path_hint is not None
                and not _same_path(
                    request.path_hint,
                    archived_result.attachment_identity_key,
                )
            )
        ):
            raise WorkflowError("CODEX_WORKSPACE_REQUEST_INVALID")
        if owner != archived_intent.owner:
            raise WorkflowError("CODEX_WORKSPACE_OWNER_MISMATCH")
        authority = self._actor_authority(actor_binding, request)
        self._archive_facts(archived_intent)
        restore_intent = host_action_from_parts(
            request=request.request,
            action=RESTORE_WORKTREE,
            project_uid=self.project_uid,
            task_uid=archived_intent.task_uid,
            owner=owner,
            authority=authority,
            attachment_id=archived_result.attachment_id,
            attachment_identity_key=archived_result.attachment_identity_key,
            expected_head=archived_result.head,
            base_sha=archived_intent.base_sha,
            common_dir=archived_intent.common_dir,
            action_args={"root": archived_result.attachment_identity_key},
            evidence_digest=archived_result.receipt_digest,
        )
        return self.host_actions.plan(restore_intent)

    def confirm_restore(self, intent, receipt):
        if isinstance(intent, Mapping):
            intent = HostAction.from_dict(intent)
        if not isinstance(intent, HostAction):
            raise WorkflowError("HOST_ACTION_INTENT_REQUIRED")
        if intent.action != RESTORE_WORKTREE:
            raise WorkflowError("HOST_ACTION_ACTION_MISMATCH")
        persisted = self.host_actions.load(intent.request.request_uid)
        if persisted.to_dict() != intent.to_dict():
            raise WorkflowError("HOST_ACTION_REQUEST_CHANGED")
        self.host_actions.revalidate(intent)
        existing = self.host_actions.result(intent.request.request_uid)
        if existing is not None:
            if (existing.action_digest != intent.input_digest
                    or existing.receipt_digest != digest_value(receipt)):
                raise WorkflowError("HOST_ACTION_RESULT_CHANGED")
            return public_result(existing)
        operation_id = self._action_receipt(
            intent,
            receipt,
            result_type="restored",
            attachment_state="active",
        )
        path = Path(intent.attachment_identity_key)
        if not path.is_dir():
            raise WorkflowError("CODEX_WORKSPACE_MISSING")
        try:
            current = WorktreeAdapter(
                self.control_root, expected_project_uid=self.project_uid
            ).inspect(path)
        except WorktreeAdapterError as exc:
            raise WorkflowError(str(exc)) from exc
        if (
            current.get("registered") is not True
            or current.get("detached") is not True
            or current.get("branch") is not None
            or current.get("project_uid") != self.project_uid
            or not _same_path(current.get("common_dir"), intent.common_dir)
        ):
            raise WorkflowError("CODEX_WORKSPACE_RESTORE_FACTS_INVALID")
        if current.get("head") != intent.expected_head:
            raise WorkflowError("CODEX_WORKSPACE_HEAD_DRIFT")
        restored_binding = {
            "schema": 1,
            "ownership": "native",
            "binding_uid": intent.binding_uid,
            "project_uid": intent.project_uid,
            "work_uid": intent.work_uid,
            "task_uid": intent.task_uid,
            "owner": intent.owner,
            "owner_adapter": OWNER_ADAPTER,
            "request_uid": intent.request.request_uid,
            "path": intent.attachment_identity_key,
            "control_root": str(self.control_root),
            "common_dir": intent.common_dir,
            "branch": None,
            "base_sha": intent.base_sha,
            "head": current["head"],
            "registered": True,
            "cleanup_action": "archive",
            "deletable": False,
            "attachment_id": intent.attachment_id,
            "attachment_identity_key": intent.attachment_identity_key,
            "verification": {
                **current,
                "detached": True,
                "attachment_id": intent.attachment_id,
                "attachment_identity_key": intent.attachment_identity_key,
            },
        }
        facts = {
            "registered": True,
            "detached": True,
            "head": current["head"],
            "common_dir": current["common_dir"],
        }
        result = HostActionResult(
            action=RESTORE_WORKTREE,
            request_uid=intent.request.request_uid,
            action_digest=intent.input_digest,
            operation_id=operation_id,
            receipt_digest=digest_value(receipt),
            attachment_id=intent.attachment_id,
            attachment_identity_key=intent.attachment_identity_key,
            head=intent.expected_head,
            payload={"binding": restored_binding, "git_facts": facts},
        )
        confirmed = self.host_actions.confirm(intent, result)
        return public_result(confirmed)

    def cleanup_plan(self, binding, *, reason=""):
        if not isinstance(binding, Mapping):
            raise WorkflowError("CODEX_WORKSPACE_BINDING_INVALID")
        ownership = binding.get("ownership")
        path = _path(binding.get("path"), "CODEX_WORKSPACE_PATH_INVALID")
        common = binding.get("common_dir")
        if not common or not _same_path(common, self._control()["common_dir"]):
            raise WorkflowError("CODEX_WORKSPACE_COMMON_DIR_CHANGED")
        action = {
            "native": "archive",
            "bound": "release",
            "legacy-managed": "remove",
        }.get(ownership)
        if action is None or binding.get("cleanup_action") != action:
            raise WorkflowError("CODEX_WORKSPACE_DISPOSITION_INVALID")
        if ownership == "native" and (
            binding.get("registered") is not True
            or not isinstance(binding.get("verification"), Mapping)
            or binding["verification"].get("registered") is not True
        ):
            raise WorkflowError("CODEX_NATIVE_RECEIPT_REQUIRED")
        if ownership == "native":
            verification = binding["verification"]
            expected_binding = digest_value(
                {
                    "project_uid": verification.get("project_uid"),
                    "request_uid": verification.get("request_uid"),
                    "operation_id": verification.get("native_operation_id"),
                    "attachment_id": verification.get("attachment_id"),
                    "git_dir": verification.get("git_dir"),
                }
            )[:32]
            if (
                binding.get("binding_uid") != expected_binding
                or binding.get("project_uid") != verification.get("project_uid")
                or binding.get("request_uid") != verification.get("request_uid")
                or not _same_path(path, verification.get("source_root"))
                or not _same_path(common, verification.get("common_dir"))
                or binding.get("branch") != verification.get("branch")
                or binding.get("head") != verification.get("head")
            ):
                raise WorkflowError("CODEX_NATIVE_RECEIPT_REQUIRED")
        deletable = ownership == "legacy-managed"
        if binding.get("deletable") is not deletable:
            raise WorkflowError("CODEX_WORKSPACE_DISPOSITION_INVALID")
        result = {
            "schema": 1,
            "kind": ARCHIVE_KIND if ownership == "native" else "worktree-" + action,
            "state": "pending",
            "action": action,
            "ownership": ownership,
            "binding_uid": binding.get("binding_uid"),
            "request_uid": binding.get("request_uid"),
            "project_uid": binding.get("project_uid") or self.project_uid,
            "path": str(path),
            "common_dir": str(Path(common).resolve()),
            "branch": binding.get("branch"),
            "head": binding.get("head"),
            "deletable": deletable,
            "reason": str(reason or ""),
            "host_action": None,
        }
        if ownership == "native":
            result["host_action"] = {
                "schema": 1,
                "kind": ARCHIVE_KIND,
                "state": "pending",
                "operation_id": verification.get("native_operation_id"),
                "attachment_id": verification.get("attachment_id"),
                "path": result["path"],
                "head": result["head"],
            }
        return result


__all__ = [
    "CodexCreateIntent",
    "CodexWorkspaceAdapter",
    "CREATE_KIND",
    "ARCHIVE_KIND",
    "OWNER_ADAPTER",
]
