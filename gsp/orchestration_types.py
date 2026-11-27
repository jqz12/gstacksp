"""Stable data contracts shared by orchestration subsystems.

This module deliberately has no runtime imports.  Serialization is explicit so
state-file compatibility does not depend on dataclass internals.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Mapping, Optional, Protocol


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def digest_value(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return digest_text(encoded)


@dataclass(frozen=True)
class _Serializable:
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]):
        return cls(**dict(value))


@dataclass(frozen=True)
class ObjectRef(_Serializable):
    uid: str
    kind: str
    project_uid: str
    canonical_id: str
    scope_uid: Optional[str] = None


@dataclass(frozen=True)
class RequestRef(_Serializable):
    request_uid: str
    logical_key: str
    input_digest: str
    project_uid: str
    work_uid: Optional[str]
    attempt_uid: Optional[str]
    operation_kind: str
    controller_generation: Optional[int] = None
    cancel_generation: Optional[int] = None
    contract_uid: Optional[str] = None
    contract_version: Optional[int] = None
    contract_digest: Optional[str] = None


@dataclass(frozen=True)
class SessionBinding(_Serializable):
    binding_uid: str = ""
    session: ObjectRef = None
    work_uid: str = ""
    kind: str = "main"
    host: str = "unknown"
    host_thread_id: Optional[str] = None
    host_state: str = "unverified"
    controller_generation: int = 1
    cancel_generation: int = 1
    capabilities: frozenset[str] = frozenset()
    cancelled: bool = False
    created_at: str = ""
    project_uid: str = ""
    common_dir: str = ""
    handle_path: str = ""

    def to_dict(self) -> dict:
        value = asdict(self)
        value["capabilities"] = sorted(self.capabilities)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]):
        session = value["session"]
        if isinstance(session, Mapping):
            session = ObjectRef.from_dict(session)
        capabilities = value.get("capabilities") or ()
        if isinstance(capabilities, str):
            raise ValueError("capabilities must be an iterable of strings")
        return cls(
            binding_uid=value["binding_uid"],
            session=session,
            work_uid=value["work_uid"],
            kind=value["kind"],
            host=value["host"],
            host_thread_id=value.get("host_thread_id"),
            host_state=value["host_state"],
            controller_generation=value["controller_generation"],
            cancel_generation=value["cancel_generation"],
            capabilities=frozenset(capabilities),
            cancelled=bool(value["cancelled"]),
            created_at=value["created_at"],
            project_uid=value["project_uid"],
            common_dir=value["common_dir"],
            handle_path=value["handle_path"],
        )


@dataclass(frozen=True)
class ResourceClaim(_Serializable):
    uid: str
    kind: str
    scope: str
    canonical_key: str
    generation: int
    owner_work_uid: str
    owner_session_uid: str
    adapter: str
    state: str


@dataclass(frozen=True)
class OperationRecord(_Serializable):
    operation_uid: str
    request: RequestRef
    phase: str
    expected_state_digest: str
    result_ref: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class TaskContract(_Serializable):
    contract_uid: str
    version: int
    content_digest: str
    project_uid: str
    work_uid: str
    task_uid: str
    dispatch_uid: Optional[str]
    attempt_uid: Optional[str]
    base_sha: Optional[str]
    scope: tuple[str, ...]
    interfaces: tuple[str, ...]
    acceptance: tuple[str, ...]
    model_alias: str
    effort: str
    controller_generation: int
    cancel_generation: int


@dataclass(frozen=True)
class EventEnvelope(_Serializable):
    event_uid: str
    kind: str
    project_uid: str
    work_uid: Optional[str]
    task_uid: Optional[str]
    dispatch_uid: Optional[str]
    attempt_uid: Optional[str]
    contract_uid: Optional[str]
    contract_version: Optional[int]
    contract_digest: Optional[str]
    controller_generation: int
    cancel_generation: int
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class BudgetReservation(_Serializable):
    uid: str
    work_uid: str
    request_uid: str
    reserved_limits: Mapping[str, Any]
    usage_state: str


@dataclass(frozen=True)
class UsageRecord(_Serializable):
    response_id: str
    model_alias: str
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    cached_input: Optional[int]
    reasoning_tokens: Optional[int]
    cost: Optional[str]
    status: str


@dataclass(frozen=True)
class DocumentEdit(_Serializable):
    target_uid: str
    field: str
    before_digest: Optional[str]
    new_value: Optional[str]
    operation: str


@dataclass(frozen=True)
class EvidenceManifest(_Serializable):
    uid: str
    work_uid: str
    task_uid: str
    contract_digest: str
    input_digest: str
    objects: tuple[Mapping[str, Any], ...]
    git_refs: tuple[str, ...]


@dataclass(frozen=True)
class WorktreeRequest(_Serializable):
    request: RequestRef
    owner_adapter: str
    source_root: str
    base_sha: str
    branch: str
    path_hint: Optional[str]


@dataclass(frozen=True)
class AdapterResult(_Serializable):
    state: str
    resource_uid: Optional[str]
    path: Optional[str]
    host_identity: Optional[str]
    reason: Optional[str]
    pending_action: Optional[Mapping[str, Any]]


@dataclass(frozen=True)
class LandingRequest(_Serializable):
    request: RequestRef
    binding: SessionBinding
    target_ref: str
    expected_target_sha: str
    candidate_sha: str
    evidence_uid: str


@dataclass(frozen=True)
class LandingResult(_Serializable):
    state: str
    actual_sha: Optional[str]
    proof_uid: Optional[str]


@dataclass(frozen=True)
class WorkerRef(_Serializable):
    uid: str
    session_uid: str
    attempt_uid: str
    native_session_id: Optional[str]
    process_identity: Optional[str]
    workspace: str
    model_alias: str
    state: str


@dataclass(frozen=True)
class IssueRef(_Serializable):
    uid: str
    work_uid: str
    task_uid: str
    acceptance_key: str
    root_cause_key: str
    signature: str
    repair_count: int
    owner: str
    state: str


class OpenIssueProvider(Protocol):
    def list_open_issues(self, work_uid: str, task_uid: str) -> list[IssueRef]:
        """Return the unresolved issues for one work/task pair."""


@dataclass(frozen=True)
class ExecutionDecision(_Serializable):
    mode: str
    reason: str
    independent_review_required: bool
    model_alias: str
    effort: str
    helper_depth: int
    slot_limit: int


@dataclass(frozen=True)
class MigrationManifest(_Serializable):
    uid: str
    schema: int
    source_digest: str
    project_uid: str
    allocator_incarnation: Optional[str]
    source_counts: Mapping[str, int]
    classification_manifest: tuple[Mapping[str, Any], ...]
    objects: tuple[Mapping[str, Any], ...]
    aliases: tuple[Mapping[str, Any], ...]
    resources: tuple[Mapping[str, Any], ...]
    blockers: tuple[Mapping[str, Any], ...]
    backup_policy: Mapping[str, Any]
