"""Sequential workspace reuse over the existing lease and resource authorities."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
from typing import Literal

from .core import WorkflowError, atomic, now, read
from .orchestration_types import digest_value
from .resource_registry import (
    ResourceOwner,
    ResourceRegistry,
    ResourceRegistryError,
)
from .worktree_adapters import WorktreeAdapter, WorktreeAdapterError


GroupMode = Literal["sequential", "parallel"]
EligibilityMode = Literal[
    "reuse",
    "create_required",
    "pending_host",
    "legacy_managed",
    "blocked",
]


def _required_text(value, code):
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(code)
    return value.strip()


def _same_path(left, right):
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(
        str(Path(right).resolve())
    )


def _lease_key(path):
    return os.path.normcase(str(Path(path).resolve()))


def _lease_revision(registry):
    return digest_value(
        {
            "schema": 1,
            "workspace_leases": registry.get("workspace_leases") or {},
        }
    )


@dataclass(frozen=True)
class WorkspaceGroup:
    """Stable sequential ownership of one registered parent workspace."""

    schema: int
    group_uid: str
    project_uid: str
    work_uid: str
    control_root: str
    workspace_root: str
    common_dir: str
    branch: str
    base_sha: str
    parent_binding_uid: str
    controller_binding_uid: str
    controller_generation: int
    cancel_generation: int
    mode: GroupMode
    state: Literal["active", "released"]
    source_writer_binding_uid: str | None
    resource_request_uid: str

    def to_dict(self):
        return asdict(self)

    def validate(self):
        if self.schema != 1 or self.mode not in {"sequential", "parallel"}:
            raise WorkflowError("WORKSPACE_GROUP_INVALID")
        if self.state not in {"active", "released"}:
            raise WorkflowError("WORKSPACE_GROUP_STATE_INVALID")
        for name in (
            "group_uid",
            "project_uid",
            "work_uid",
            "control_root",
            "workspace_root",
            "common_dir",
            "branch",
            "base_sha",
            "parent_binding_uid",
            "controller_binding_uid",
            "resource_request_uid",
        ):
            _required_text(
                getattr(self, name),
                "WORKSPACE_GROUP_INVALID " + name,
            )
        for name in ("controller_generation", "cancel_generation"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
            ):
                raise WorkflowError(
                    "WORKSPACE_GROUP_INVALID " + name
                )
        if (
            self.source_writer_binding_uid is not None
            and (
                not isinstance(self.source_writer_binding_uid, str)
                or not self.source_writer_binding_uid.strip()
            )
        ):
            raise WorkflowError(
                "WORKSPACE_GROUP_INVALID source_writer_binding_uid"
            )
        return self


@dataclass(frozen=True)
class WorkspaceEligibility:
    task_uid: str
    contract_digest: str
    group_uid: str | None
    mode: EligibilityMode
    eligible: bool
    reasons: tuple[str, ...]
    blockers: tuple[str, ...]
    requires_new_context: bool = True
    requires_independent_review: bool = True

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class WorkspaceClaimReceipt:
    group_uid: str
    task_uid: str
    contract_digest: str
    claim_request_uid: str
    resource_request_uid: str
    source_writer_session_id: str
    source_writer_binding_uid: str
    controller_generation: int
    cancel_generation: int
    lease_revision: str
    state: Literal["committed", "pending", "unknown"]
    host_observation: Literal["unverified"] = "unverified"
    pending_action: dict | None = None

    def to_dict(self):
        return asdict(self)


def build_workspace_group(
    project_root,
    control_root,
    workspace_root,
    *,
    project_uid,
    work_uid,
    parent_binding_uid,
    controller_binding_uid,
    controller_generation,
    cancel_generation,
    mode="sequential",
    source_writer_binding_uid=None,
):
    """Verify real Git facts and derive stable group/resource identities."""
    project_uid = _required_text(
        project_uid, "WORKSPACE_GROUP_PROJECT_UID_REQUIRED"
    )
    work_uid = _required_text(
        work_uid, "WORKSPACE_GROUP_WORK_UID_REQUIRED"
    )
    parent_binding_uid = _required_text(
        parent_binding_uid, "WORKSPACE_GROUP_PARENT_BINDING_REQUIRED"
    )
    controller_binding_uid = _required_text(
        controller_binding_uid,
        "WORKSPACE_GROUP_CONTROLLER_BINDING_REQUIRED",
    )
    if mode not in {"sequential", "parallel"}:
        raise WorkflowError("WORKSPACE_GROUP_MODE_INVALID")
    control = Path(control_root).resolve()
    workspace = Path(workspace_root).resolve()
    if not control.is_dir() or not workspace.is_dir():
        raise WorkflowError("WORKSPACE_GROUP_ROOT_MISSING")
    try:
        inspection = WorktreeAdapter(
            control, expected_project_uid=project_uid
        ).inspect(workspace)
    except WorktreeAdapterError as exc:
        raise WorkflowError(str(exc)) from exc
    if inspection.get("project_uid") != project_uid:
        raise WorkflowError("WORKSPACE_GROUP_PROJECT_MISMATCH")
    branch = inspection.get("branch")
    if not isinstance(branch, str) or not branch.strip():
        raise WorkflowError("WORKSPACE_GROUP_BRANCH_REQUIRED")
    control_inspection = WorktreeAdapter(
        control, expected_project_uid=project_uid
    ).inspect(control)
    if os.path.normcase(str(inspection["common_dir"])) != os.path.normcase(
        str(control_inspection["common_dir"])
    ):
        raise WorkflowError("WORKSPACE_GROUP_COMMON_DIR_MISMATCH")
    identity = {
        "schema": 1,
        "project_uid": project_uid,
        "work_uid": work_uid,
        "control_root": os.path.normcase(str(control)),
        "workspace_root": os.path.normcase(str(workspace)),
        "common_dir": os.path.normcase(
            str(Path(inspection["common_dir"]).resolve())
        ),
        "branch": branch,
    }
    group_uid = digest_value(identity)
    resource_request_uid = digest_value(
        {
            "schema": 1,
            "kind": "workspace-group-root-branch",
            "group_uid": group_uid,
        }
    )
    return WorkspaceGroup(
        schema=1,
        group_uid=group_uid,
        project_uid=project_uid,
        work_uid=work_uid,
        control_root=str(control),
        workspace_root=str(workspace),
        common_dir=str(Path(inspection["common_dir"]).resolve()),
        branch=branch,
        base_sha=str(inspection["head"]),
        parent_binding_uid=parent_binding_uid,
        controller_binding_uid=controller_binding_uid,
        controller_generation=controller_generation,
        cancel_generation=cancel_generation,
        mode=mode,
        state="active",
        source_writer_binding_uid=source_writer_binding_uid,
        resource_request_uid=resource_request_uid,
    ).validate()


def evaluate_workspace_group(
    group,
    *,
    project_uid,
    work_uid,
    control_root,
    workspace_root,
    branch,
    task_uid,
    contract_digest,
    requested_mode,
    controller_binding_uid,
    controller_generation,
    cancel_generation,
    unknown_inflight=False,
    legacy_compatibility=False,
):
    """Return a side-effect-free reuse/create/legacy decision."""
    task_uid = _required_text(task_uid, "WORKSPACE_GROUP_TASK_REQUIRED")
    contract_digest = _required_text(
        contract_digest, "WORKSPACE_GROUP_CONTRACT_REQUIRED"
    )
    if requested_mode not in {"sequential", "parallel"}:
        raise WorkflowError("WORKSPACE_GROUP_MODE_INVALID")
    reasons = ["host_observation=unverified"]
    if group is None:
        if legacy_compatibility:
            return WorkspaceEligibility(
                task_uid,
                contract_digest,
                None,
                "legacy_managed",
                True,
                tuple(reasons + ["explicit_legacy_compatibility"]),
                (),
            )
        if requested_mode == "parallel":
            return WorkspaceEligibility(
                task_uid,
                contract_digest,
                None,
                "pending_host",
                False,
                tuple(reasons),
                ("WORKSPACE_GROUP_HOST_CREATE_PENDING",),
            )
        return WorkspaceEligibility(
            task_uid,
            contract_digest,
            None,
            "blocked",
            False,
            tuple(reasons),
            ("WORKSPACE_GROUP_REQUIRED",),
        )
    if not isinstance(group, WorkspaceGroup):
        raise WorkflowError("WORKSPACE_GROUP_INVALID")
    group.validate()
    blockers = []
    if group.project_uid != project_uid:
        blockers.append("WORKSPACE_GROUP_PROJECT_MISMATCH")
    if group.work_uid != work_uid:
        blockers.append("WORKSPACE_GROUP_WORK_MISMATCH")
    if not _same_path(group.control_root, control_root):
        blockers.append("WORKSPACE_GROUP_CONTROL_ROOT_MISMATCH")
    if not _same_path(group.workspace_root, workspace_root):
        blockers.append("WORKSPACE_GROUP_ROOT_MISMATCH")
    if group.branch != branch:
        blockers.append("WORKSPACE_GROUP_BRANCH_MISMATCH")
    if group.controller_binding_uid != controller_binding_uid:
        blockers.append("WORKSPACE_GROUP_CONTROLLER_MISMATCH")
    if (
        group.controller_generation != controller_generation
        or group.cancel_generation != cancel_generation
    ):
        blockers.append("WORKSPACE_GROUP_STALE_GENERATION")
    if group.state != "active":
        blockers.append("WORKSPACE_GROUP_RELEASED")
    if unknown_inflight:
        blockers.append("WORKSPACE_GROUP_EXECUTION_UNKNOWN")
    if (
        group.source_writer_binding_uid
        and group.source_writer_binding_uid
        == group.controller_binding_uid
    ):
        blockers.append("WORKSPACE_GROUP_CONTROLLER_WRITE_FORBIDDEN")
    if blockers:
        return WorkspaceEligibility(
            task_uid,
            contract_digest,
            group.group_uid,
            "blocked",
            False,
            tuple(reasons),
            tuple(blockers),
        )
    if requested_mode == "parallel" or group.mode != "sequential":
        return WorkspaceEligibility(
            task_uid,
            contract_digest,
            group.group_uid,
            "pending_host",
            False,
            tuple(reasons + ["parallel_reuse_forbidden"]),
            ("WORKSPACE_GROUP_HOST_CREATE_PENDING",),
        )
    return WorkspaceEligibility(
        task_uid,
        contract_digest,
        group.group_uid,
        "reuse",
        True,
        tuple(reasons + ["sequential_source_writer_group"]),
        (),
    )


class SessionWorkspaceLeaseAuthority:
    """CAS provider over the existing sessions/index.json lease map."""

    def __init__(self, project_root, run_id):
        self.project_root = Path(project_root).resolve()
        self.run_id = _required_text(
            run_id, "WORKSPACE_GROUP_RUN_REQUIRED"
        )

    def _load(self):
        from .project import (Project)
        from .sessions import load_registry
        state_path = (
            self.project_root / ".gsp" / "runs" / self.run_id / "state.json"
        )
        if not state_path.is_file():
            raise WorkflowError("WORKSPACE_GROUP_STATE_MISSING")
        state = read(state_path)
        return load_registry(Project(self.project_root), state, create=False)

    def _registry_path(self):
        return (
            self.project_root
            / ".gsp"
            / "runs"
            / self.run_id
            / "sessions"
            / "index.json"
        )

    def revision(self):
        return _lease_revision(self._load())

    def projection(self, workspace_root):
        registry = self._load()
        key = _lease_key(workspace_root)
        owner_id = (registry.get("workspace_leases") or {}).get(key)
        owner = next(
            (
                row
                for row in registry.get("sessions", [])
                if row.get("id") == owner_id
            ),
            None,
        )
        return {
            "schema": 1,
            "workspace_root": str(Path(workspace_root).resolve()),
            "source_writer_session_id": owner_id,
            "source_writer_binding_uid": (
                owner.get("binding_uid") if owner else None
            ),
            "source_writer_status": owner.get("status") if owner else None,
            "lease_revision": _lease_revision(registry),
            "host_observation": "unverified",
        }

    @staticmethod
    def _find_session(registry, session_id, binding_uid, work_uid):
        row = next(
            (
                item
                for item in registry.get("sessions", [])
                if item.get("id") == session_id
            ),
            None,
        )
        if row is None:
            raise WorkflowError("WORKSPACE_GROUP_SESSION_NOT_FOUND")
        if (
            row.get("binding_uid") != binding_uid
            or row.get("work_uid") != work_uid
            or row.get("write_mode") != "write"
            or row.get("status") not in {"active", "waiting"}
            or not row.get("claimed_paths")
        ):
            raise WorkflowError("WORKSPACE_GROUP_SOURCE_WRITER_INVALID")
        return row

    @staticmethod
    def _validate_session_record(
        record, *, session_id, binding_uid, work_uid, task_uid,
        controller_generation, cancel_generation,
    ):
        if not isinstance(record, dict):
            raise WorkflowError("WORKSPACE_GROUP_SESSION_RECORD_REQUIRED")
        if (
            record.get("id") != session_id
            or record.get("binding_uid") != binding_uid
            or record.get("work_uid") != work_uid
            or record.get("task") != task_uid
            or record.get("write_mode") != "write"
            or record.get("role") not in {"worker", "integration"}
            or record.get("status") not in {"active", "waiting"}
            or not record.get("claimed_paths")
            or record.get("controller_generation")
            != controller_generation
            or record.get("cancel_generation") != cancel_generation
        ):
            raise WorkflowError("WORKSPACE_GROUP_SOURCE_WRITER_INVALID")
        return record

    def preflight_claim(
        self,
        group,
        *,
        task_uid,
        source_writer_session_id=None,
        source_writer_binding_uid=None,
        source_writer_generation=None,
        source_writer_cancel_generation=None,
        session_record=None,
        registry=None,
    ):
        """Validate all known reuse fences without creating any authority."""
        if not isinstance(group, WorkspaceGroup):
            raise WorkflowError("WORKSPACE_GROUP_INVALID")
        group.validate()
        registry = self._load() if registry is None else registry
        controller = next(
            (
                item
                for item in registry.get("sessions", [])
                if item.get("binding_uid")
                == group.controller_binding_uid
            ),
            None,
        )
        parent = next(
            (
                item
                for item in registry.get("sessions", [])
                if item.get("binding_uid") == group.parent_binding_uid
            ),
            None,
        )
        if (
            controller is None
            or controller.get("role") != "controller"
            or registry.get("controller_session") != controller.get("id")
            or controller.get("status") not in {"active", "waiting"}
            or controller.get("work_uid") != group.work_uid
            or controller.get("controller_generation")
            != group.controller_generation
            or controller.get("cancel_generation")
            != group.cancel_generation
        ):
            raise WorkflowError("WORKSPACE_GROUP_CONTROLLER_STALE")
        if (
            parent is None
            or parent.get("role") not in {"controller", "integration"}
            or parent.get("status") not in {"active", "waiting"}
            or parent.get("work_uid") != group.work_uid
        ):
            raise WorkflowError("WORKSPACE_GROUP_PARENT_STALE")

        row = None
        if source_writer_session_id is not None:
            if session_record is not None:
                row = self._validate_session_record(
                    session_record,
                    session_id=source_writer_session_id,
                    binding_uid=source_writer_binding_uid,
                    work_uid=group.work_uid,
                    task_uid=task_uid,
                    controller_generation=group.controller_generation,
                    cancel_generation=group.cancel_generation,
                )
            else:
                row = self._find_session(
                    registry,
                    source_writer_session_id,
                    source_writer_binding_uid,
                    group.work_uid,
                )
            if (
                source_writer_generation != group.controller_generation
                or source_writer_cancel_generation
                != group.cancel_generation
                or row.get("task") != task_uid
                or row.get("controller_generation")
                != group.controller_generation
                or row.get("cancel_generation")
                != group.cancel_generation
            ):
                raise WorkflowError("WORKSPACE_GROUP_STALE_GENERATION")
            if (
                source_writer_binding_uid
                == group.controller_binding_uid
                or row.get("role") == "controller"
            ):
                raise WorkflowError(
                    "WORKSPACE_GROUP_CONTROLLER_WRITE_FORBIDDEN"
                )

        key = _lease_key(group.workspace_root)
        leases = registry.setdefault("workspace_leases", {})
        owner_id = leases.get(key)
        owner = next(
            (
                item
                for item in registry.get("sessions", [])
                if item.get("id") == owner_id
            ),
            None,
        )
        previous = []
        for candidate in registry.get("sessions", []):
            if (
                candidate.get("id") == source_writer_session_id
                or candidate.get("role") not in {"worker", "integration"}
                or candidate.get("write_mode") != "write"
                or not candidate.get("worktree")
                or not _same_path(
                    candidate["worktree"], group.workspace_root
                )
            ):
                continue
            if (
                candidate.get("id") != owner_id
                and not isinstance(candidate.get("workspace_group"), dict)
            ):
                continue
            candidate_task = candidate.get("task")
            accepted = (
                (registry.get("tasks") or {})
                .get(candidate_task, {})
                .get("acceptance_status")
                == "accepted"
            )
            ended = candidate.get("status") == "released"
            if candidate.get("id") == owner_id or not accepted or not ended:
                previous.append(candidate)
        if previous:
            raise WorkflowError("WORKSPACE_GROUP_PREVIOUS_TASK_UNACCEPTED")
        if owner_id and owner_id != source_writer_session_id:
            owner_binding = owner.get("binding_uid") if owner else None
            owner_accepted = (
                owner is not None
                and (registry.get("tasks") or {})
                .get(owner.get("task"), {})
                .get("acceptance_status")
                == "accepted"
            )
            owner_ended = (
                owner is not None and owner.get("status") == "released"
            )
            if owner_binding != group.controller_binding_uid and not (
                owner_accepted and owner_ended
            ):
                raise WorkflowError(
                    "WORKSPACE_GROUP_SOURCE_WRITER_CONFLICT " + str(owner_id)
                )
        return {
            "registry": registry,
            "row": row,
            "controller": controller,
            "parent": parent,
            "owner_id": owner_id,
            "owner": owner,
            "key": key,
            "revision": _lease_revision(registry),
        }

    def claim(
        self,
        group,
        *,
        task_uid,
        contract_digest,
        source_writer_session_id,
        source_writer_binding_uid,
        expected_lease_revision=None,
        session_record=None,
    ):
        from .workflow_locks import (locked)

        with locked(self.project_root):
            registry = self._load()
            current_revision = _lease_revision(registry)
            if (
                expected_lease_revision is not None
                and expected_lease_revision != current_revision
            ):
                raise WorkflowError(
                    "WORKSPACE_GROUP_LEASE_REVISION_MISMATCH"
                )
            checked = self.preflight_claim(
                group,
                task_uid=task_uid,
                source_writer_session_id=source_writer_session_id,
                source_writer_binding_uid=source_writer_binding_uid,
                source_writer_generation=group.controller_generation,
                source_writer_cancel_generation=group.cancel_generation,
                session_record=session_record,
                registry=registry,
            )
            row = checked["row"]
            key = checked["key"]
            leases = registry.setdefault("workspace_leases", {})
            owner_id = checked["owner_id"]
            metadata = {
                "group_uid": group.group_uid,
                "resource_request_uid": group.resource_request_uid,
                "task_uid": task_uid,
                "contract_digest": contract_digest,
                "controller_generation": group.controller_generation,
                "cancel_generation": group.cancel_generation,
                "claimed_at": now(),
            }
            existing = row.get("workspace_group")
            if owner_id == source_writer_session_id:
                if existing is not None:
                    comparable = {
                        key: existing.get(key) for key in metadata
                        if key != "claimed_at"
                    }
                    expected = {
                        key: value for key, value in metadata.items()
                        if key != "claimed_at"
                    }
                    if comparable != expected:
                        raise WorkflowError(
                            "WORKSPACE_GROUP_CLAIM_CHANGED"
                        )
                    return {
                        "lease_revision": current_revision,
                        "previous_session_id": None,
                        "idempotent": True,
                    }
            if session_record is not None:
                registry.setdefault("sessions", []).append(
                    dict(session_record)
                )
                row = registry["sessions"][-1]
            leases[key] = source_writer_session_id
            row["workspace_group"] = metadata
            row["updated_at"] = now()
            atomic(self._registry_path(), registry)
            return {
                "lease_revision": _lease_revision(registry),
                "previous_session_id": owner_id,
                "idempotent": False,
            }

    def release(
        self,
        group,
        *,
        task_uid,
        source_writer_session_id,
        expected_lease_revision=None,
    ):
        from .workflow_locks import (locked)

        with locked(self.project_root):
            registry = self._load()
            current_revision = _lease_revision(registry)
            if (
                expected_lease_revision is not None
                and expected_lease_revision != current_revision
            ):
                raise WorkflowError(
                    "WORKSPACE_GROUP_LEASE_REVISION_MISMATCH"
                )
            if (
                (registry.get("tasks") or {})
                .get(task_uid, {})
                .get("acceptance_status")
                != "accepted"
            ):
                raise WorkflowError("WORKSPACE_GROUP_TASK_NOT_ACCEPTED")
            key = _lease_key(group.workspace_root)
            leases = registry.setdefault("workspace_leases", {})
            if leases.get(key) != source_writer_session_id:
                raise WorkflowError(
                    "WORKSPACE_GROUP_SOURCE_WRITER_MISMATCH"
                )
            leases.pop(key)
            row = next(
                (
                    item
                    for item in registry.get("sessions", [])
                    if item.get("id") == source_writer_session_id
                ),
                None,
            )
            if row is not None and isinstance(
                row.get("workspace_group"), dict
            ):
                row["workspace_group"]["released_at"] = now()
            atomic(self._registry_path(), registry)
            return {
                "released": True,
                "lease_revision": _lease_revision(registry),
                "host_observation": "unverified",
            }


def claim_workspace_group(
    group,
    *,
    project_uid,
    work_uid,
    control_root,
    workspace_root,
    branch,
    task_uid,
    contract_digest,
    source_writer_session_id,
    source_writer_binding_uid,
    source_writer_generation,
    source_writer_cancel_generation,
    resource_registry,
    lease_authority,
    requested_mode="sequential",
    expected_lease_revision=None,
    unknown_inflight=False,
    session_record=None,
):
    """Persist the group root/branch claim and its single source writer."""
    if not isinstance(group, WorkspaceGroup):
        raise WorkflowError("WORKSPACE_GROUP_INVALID")
    group.validate()
    if source_writer_binding_uid != group.source_writer_binding_uid:
        raise WorkflowError("WORKSPACE_GROUP_SOURCE_WRITER_CHANGED")
    if (
        source_writer_generation != group.controller_generation
        or source_writer_cancel_generation != group.cancel_generation
    ):
        raise WorkflowError("WORKSPACE_GROUP_STALE_GENERATION")
    eligibility = evaluate_workspace_group(
        group,
        project_uid=project_uid,
        work_uid=work_uid,
        control_root=control_root,
        workspace_root=workspace_root,
        branch=branch,
        task_uid=task_uid,
        contract_digest=contract_digest,
        requested_mode=requested_mode,
        controller_binding_uid=group.controller_binding_uid,
        controller_generation=group.controller_generation,
        cancel_generation=group.cancel_generation,
        unknown_inflight=unknown_inflight,
    )
    if eligibility.mode == "pending_host":
        raise WorkflowError("WORKSPACE_GROUP_CREATE_REQUIRED")
    if not eligibility.eligible:
        raise WorkflowError(
            eligibility.blockers[0]
            if eligibility.blockers
            else "WORKSPACE_GROUP_BLOCKED"
        )
    try:
        inspection = WorktreeAdapter(
            control_root, expected_project_uid=project_uid
        ).inspect(workspace_root)
    except WorktreeAdapterError as exc:
        raise WorkflowError(str(exc)) from exc
    if inspection.get("project_uid") != group.project_uid:
        raise WorkflowError("WORKSPACE_GROUP_PROJECT_MISMATCH")
    if not _same_path(inspection["common_dir"], group.common_dir):
        raise WorkflowError("WORKSPACE_GROUP_COMMON_DIR_MISMATCH")
    if inspection.get("branch") != group.branch:
        raise WorkflowError("WORKSPACE_GROUP_BRANCH_MISMATCH")
    if not isinstance(resource_registry, ResourceRegistry):
        raise WorkflowError("WORKSPACE_GROUP_RESOURCE_AUTHORITY_REQUIRED")
    if not isinstance(lease_authority, SessionWorkspaceLeaseAuthority):
        raise WorkflowError("WORKSPACE_GROUP_LEASE_AUTHORITY_REQUIRED")
    # All fences that can be decided from durable state run before either the
    # resource intent or a session row is created.  The claim method repeats
    # the check under the project lock to close the race between preview and
    # commit without weakening replay safety.
    lease_authority.preflight_claim(
        group,
        task_uid=task_uid,
        source_writer_session_id=source_writer_session_id,
        source_writer_binding_uid=source_writer_binding_uid,
        source_writer_generation=source_writer_generation,
        source_writer_cancel_generation=source_writer_cancel_generation,
        session_record=session_record,
    )
    resource_owner = ResourceOwner(
        work_uid=group.work_uid,
        session_uid=group.group_uid,
        generation=1,
    )
    resources = [
        {
            "kind": "path",
            "identity": str(Path(workspace_root).resolve()),
            "disposition": "release",
        },
        {
            "kind": "branch",
            "identity": group.branch,
            "disposition": "release",
        },
    ]
    try:
        stored = resource_registry.reserve(
            group.resource_request_uid, resource_owner, resources
        )
    except ResourceRegistryError as exc:
        raise WorkflowError(
            "WORKSPACE_GROUP_RESOURCE_CONFLICT " + str(exc)
        ) from exc
    if stored.get("state") == "released":
        raise WorkflowError("WORKSPACE_GROUP_RESOURCE_RELEASED")
    # The deterministic root/branch intent stays reserved when source-writer
    # fencing fails.  A corrected retry resumes this same request instead of
    # consuming a released tombstone.
    lease = lease_authority.claim(
        group,
        task_uid=task_uid,
        contract_digest=contract_digest,
        source_writer_session_id=source_writer_session_id,
        source_writer_binding_uid=source_writer_binding_uid,
        expected_lease_revision=expected_lease_revision,
        session_record=session_record,
    )
    try:
        resource_registry.commit(
            group.resource_request_uid, resource_owner
        )
    except ResourceRegistryError as exc:
        raise WorkflowError(
            "WORKSPACE_GROUP_COMMIT_UNKNOWN " + str(exc)
        ) from exc
    claim_request_uid = digest_value(
        {
            "schema": 1,
            "group_uid": group.group_uid,
            "task_uid": task_uid,
            "contract_digest": contract_digest,
            "source_writer_binding_uid": source_writer_binding_uid,
        }
    )
    return WorkspaceClaimReceipt(
        group_uid=group.group_uid,
        task_uid=task_uid,
        contract_digest=contract_digest,
        claim_request_uid=claim_request_uid,
        resource_request_uid=group.resource_request_uid,
        source_writer_session_id=source_writer_session_id,
        source_writer_binding_uid=source_writer_binding_uid,
        controller_generation=group.controller_generation,
        cancel_generation=group.cancel_generation,
        lease_revision=lease["lease_revision"],
        state="committed",
    )


def release_workspace_group(
    group,
    *,
    task_uid,
    source_writer_session_id,
    expected_lease_revision,
    lease_authority,
):
    """Release only the source-writer lease; root/branch claims remain."""
    if not isinstance(group, WorkspaceGroup):
        raise WorkflowError("WORKSPACE_GROUP_INVALID")
    if not isinstance(lease_authority, SessionWorkspaceLeaseAuthority):
        raise WorkflowError("WORKSPACE_GROUP_LEASE_AUTHORITY_REQUIRED")
    return lease_authority.release(
        group,
        task_uid=task_uid,
        source_writer_session_id=source_writer_session_id,
        expected_lease_revision=expected_lease_revision,
    )


def workspace_group_projection(
    group,
    *,
    resource_registry,
    lease_authority,
):
    """Read both existing authorities without creating or repairing state."""
    if not isinstance(group, WorkspaceGroup):
        raise WorkflowError("WORKSPACE_GROUP_INVALID")
    if not isinstance(resource_registry, ResourceRegistry):
        raise WorkflowError("WORKSPACE_GROUP_RESOURCE_AUTHORITY_REQUIRED")
    if not isinstance(lease_authority, SessionWorkspaceLeaseAuthority):
        raise WorkflowError("WORKSPACE_GROUP_LEASE_AUTHORITY_REQUIRED")
    resources = resource_registry.get(group.resource_request_uid)
    leases = lease_authority.projection(group.workspace_root)
    return {
        "schema": 1,
        "group_uid": group.group_uid,
        "resource_request_uid": group.resource_request_uid,
        "resource_state": resources.get("state") if resources else None,
        "resources": resources.get("resources", []) if resources else [],
        **leases,
        "read_only": True,
        "host_observation": "unverified",
    }


__all__ = [
    "SessionWorkspaceLeaseAuthority",
    "WorkspaceClaimReceipt",
    "WorkspaceEligibility",
    "WorkspaceGroup",
    "build_workspace_group",
    "claim_workspace_group",
    "evaluate_workspace_group",
    "release_workspace_group",
    "workspace_group_projection",
]
