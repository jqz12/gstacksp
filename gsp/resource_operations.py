"""Business operations for native workspaces and durable evidence."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import os
import subprocess

from .codex_workspace import CodexCreateIntent, CodexWorkspaceAdapter
from .core import WorkflowError, read, digest
from .evidence_store import (
    collect_evidence,
    persist_git_objects,
    verify_git_reachability,
    verify_manifest,
)
from .identity_store import IdentityStore
from .git_operations import is_ancestor
from .orchestration_types import (
    RequestRef,
    SessionBinding,
    WorktreeRequest,
    digest_value,
)
from .resource_registry import (
    ResourceOwner,
    ResourceRegistry,
    ResourceRegistryError,
)
from .session_context import (
    _load_authoritative_binding,
    require_capability,
    require_decision_controller,
)
from .workflow_support import contract_hash, fingerprint


def _fail(code):
    raise WorkflowError(code)


def _binding(value):
    if isinstance(value, SessionBinding):
        return value
    if isinstance(value, Mapping):
        try:
            return SessionBinding.from_dict(value)
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowError("SESSION_BINDING_INVALID") from exc
    _fail("SESSION_BINDING_REQUIRED")


def _work_uid(state):
    work = state.get("work_uuid") or state.get("work_uid")
    if not isinstance(work, str) or not work:
        _fail("WORK_UID_MISSING")
    return work


def _authoritative(project, state, binding, capabilities):
    binding = _binding(binding)
    work_uid = _work_uid(state)
    store = IdentityStore(project.root)
    binding = _load_authoritative_binding(store, binding)
    if binding.work_uid != work_uid:
        _fail("WORK_UID_MISMATCH")
    if binding.project_uid != store.project_uid():
        _fail("PROJECT_UID_MISMATCH")
    for capability in capabilities:
        require_capability(binding, capability, work_uid)
    return binding


def _controller(project, state, binding):
    binding = _authoritative(project, state, binding, ("write", "plan"))
    require_decision_controller(IdentityStore(project.root), binding, _work_uid(state))
    return binding


def _payload(args):
    raw = args.get("payload") if isinstance(args, Mapping) else None
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, (str, Path)):
        _fail("RESOURCE_PAYLOAD_REQUIRED")
    value = read(raw)
    if not isinstance(value, Mapping):
        _fail("RESOURCE_PAYLOAD_INVALID")
    return dict(value)


def _request(payload, request_uid, operation_kind, state, contract, binding):
    raw = payload.get("request")
    if isinstance(raw, Mapping):
        try:
            value = RequestRef.from_dict(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowError("REQUEST_INVALID") from exc
    else:
        work_uid = _work_uid(state)
        input_digest = payload.get("input_digest") or digest_value(
            {"request_uid": request_uid, "payload": payload}
        )
        value = RequestRef(
            request_uid=request_uid,
            logical_key=payload.get("logical_key") or request_uid,
            input_digest=input_digest,
            project_uid=state.get("project_id"),
            work_uid=work_uid,
            attempt_uid=payload.get("attempt_uid"),
            operation_kind=operation_kind,
            controller_generation=binding.controller_generation,
            cancel_generation=binding.cancel_generation,
            contract_digest=contract,
        )
    if (
        value.request_uid != request_uid
        or value.operation_kind != operation_kind
        or value.project_uid != state.get("project_id")
        or value.work_uid != _work_uid(state)
        or value.controller_generation not in (None, binding.controller_generation)
        or value.cancel_generation not in (None, binding.cancel_generation)
        or (
            value.contract_digest is not None
            and value.contract_digest != contract
        )
    ):
        _fail("REQUEST_SCOPE_MISMATCH")
    return value


def _worktree_request(payload, request):
    raw = payload["payload"] if "payload" in payload else payload.get("worktree")
    if isinstance(raw, WorktreeRequest):
        value = raw
    elif isinstance(raw, Mapping):
        try:
            value = WorktreeRequest(
                request=RequestRef.from_dict(raw["request"]),
                owner_adapter=raw["owner_adapter"],
                source_root=raw["source_root"],
                base_sha=raw["base_sha"],
                branch=raw["branch"],
                path_hint=raw.get("path_hint"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowError("CODEX_WORKSPACE_REQUEST_INVALID") from exc
    else:
        _fail("CODEX_WORKSPACE_REQUEST_INVALID")
    if value.request.to_dict() != request.to_dict():
        _fail("REQUEST_SCOPE_MISMATCH")
    return value


def _manifest(payload):
    for key in ("manifest", "evidence", "evidence_manifest"):
        value = payload.get(key)
        if isinstance(value, Mapping) and "manifest" in value:
            return dict(value)
    _fail("EVIDENCE_MANIFEST_REQUIRED")


def _evidence_sources(project, payload, args):
    raw = (
        payload.get("sources")
        or payload.get("source")
        or args.get("source")
        or []
    )
    if isinstance(raw, (str, Path, Mapping)):
        raw = [raw]
    sources = []
    for item in raw:
        if isinstance(item, Mapping):
            source = dict(item)
        else:
            source = {"path": item, "required": True}
        path = Path(source.get("path") or source.get("source") or "")
        if not path.is_absolute():
            path = project.root / path
        source["path"] = path
        sources.append(source)
    if not sources:
        _fail("EVIDENCE_SOURCES_REQUIRED")
    return sources


def _evidence_input(project, state, task, sources):
    rows = []
    for source in sources:
        path = Path(source["path"]).resolve(strict=False)
        if not path.is_relative_to(project.root.resolve()):
            _fail("EVIDENCE_SOURCE_ESCAPE " + str(path))
        rows.append(
            {
                "relative": path.relative_to(project.root.resolve()).as_posix(),
                "required": source.get("required", True),
                "kind": source.get("kind", "auto"),
                "label": source.get("label") or source.get("logical_path"),
            }
        )
    return digest_value(
        {
            "work_uid": _work_uid(state),
            "task_uid": task,
            "sources": rows,
            "fingerprint": fingerprint(project.verification_root(state) if hasattr(project,'verification_root') else project.root),
        }
    )


def _resource_owner(binding):
    return ResourceOwner(
        work_uid=binding.work_uid,
        session_uid=binding.session.uid,
        generation=binding.controller_generation,
    )


def _resource_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ResourceRegistryError as exc:
        raise WorkflowError(str(exc)) from exc


def _reserve_create(project, request, binding, request_uid):
    registry = ResourceRegistry(project.root)
    owner = _resource_owner(binding)
    resources=[{"kind":"branch","identity":request.branch,"disposition":"release"}]
    if request.path_hint is not None:
        resources.append({'kind':'path','identity':request.path_hint,'disposition':'archive'})
    branch = _resource_call(
        registry.reserve,
        request_uid,
        owner,
        resources,
    )
    archive = None
    if request.path_hint is not None:
        archive=branch
    return {"intent": branch, "archive": archive}


def _release_create_if_unissued(project, adapter, request_uid, resource):
    """Release a pre-plan claim only while the host store proves no action."""
    try:
        intent = adapter.host_action(request_uid)
    except WorkflowError as exc:
        if str(exc) != "HOST_ACTION_PENDING_MISSING":
            raise
        intent = None
    result = adapter.host_action_result(request_uid)
    if intent is not None or result is not None:
        return False
    reservation = resource.get("intent")
    owner = reservation.get("owner") if isinstance(reservation, Mapping) else None
    if not isinstance(owner, Mapping):
        _fail("RESOURCE_CREATE_OWNER_REQUIRED")
    released = _resource_call(
        ResourceRegistry(project.root).release, request_uid, owner
    )
    if released.get("state") != "released":
        _fail("RESOURCE_CREATE_RELEASE_FAILED")
    return True


def _same_resource_path(left, right):
    try:
        return os.path.normcase(str(Path(left).resolve(strict=False))) == os.path.normcase(
            str(Path(right).resolve(strict=False))
        )
    except (OSError, TypeError, ValueError):
        return False


def _archive_claim_uid(request_uid, path):
    return digest_value(
        {"request_uid": request_uid, "path": path, "kind": "archive-claim"}
    )[:32]


def _commit_created_path(project, binding, request_uid, path, result):
    registry = ResourceRegistry(project.root)
    intent = _resource_call(registry.get, request_uid)
    if intent is None:
        _fail("RESOURCE_CREATE_INTENT_MISSING")
    owner = intent["owner"]
    if intent["state"] == "reserved":
        intent = _resource_call(registry.commit, request_uid, owner)
    hinted=any(row['kind']=='path' and _same_resource_path(row['identity'],path) for row in intent['resources'])
    archive_uid = request_uid if hinted else _archive_claim_uid(request_uid, path)
    archive = intent if hinted else _resource_call(registry.get, archive_uid)
    if archive is None:
        archive = _resource_call(
            registry.reserve,
            archive_uid,
            owner,
            [{"kind": "path", "identity": path, "disposition": "archive"}],
        )
    if archive["state"] == "reserved":
        archive = _resource_call(registry.commit, archive_uid, owner)
    return {
        **result,
        "resource_intent": intent,
        "archive_claim": archive,
        "archive_claim_request_uid": archive["request_uid"],
    }


def _commit_restored_path(project, binding, request_uid, path, result):
    registry = ResourceRegistry(project.root)
    existing = next(
        (
            claim for claim in _resource_call(
                registry.list_for_work, binding.work_uid
            )
            if claim["kind"] == "path"
            and claim["disposition"] == "archive"
            and _same_resource_path(claim["identity"], path)
        ),
        None,
    )
    if existing is not None:
        return {**result, "archive_claim": existing}
    owner = _resource_owner(binding)
    archive_uid = _archive_claim_uid(request_uid, path)
    archive = _resource_call(
        registry.reserve,
        archive_uid,
        owner,
        [{"kind": "path", "identity": path, "disposition": "archive"}],
    )
    archive = _resource_call(registry.commit, archive_uid, owner)
    return {
        **result,
        "archive_claim": archive,
        "archive_claim_request_uid": archive_uid,
    }


def _archive_state(project, state):
    current = state
    state_file = project.paths(state["id"])[0] / "state.json"
    if state_file.is_file():
        current = read(state_file)
        if (
            current.get("id") != state.get("id")
            or current.get("work_uuid") != state.get("work_uuid")
            or current.get("controller_generation") != state.get("controller_generation")
            or current.get("cancel_generation") != state.get("cancel_generation")
        ):
            _fail("STATE_CHANGED")
    if current.get("inflight"):
        _fail("RESOURCE_INFLIGHT_ACTIVE")
    return current


def _workspace_clean(path):
    if not Path(path).is_dir():
        _fail("CODEX_WORKSPACE_MISSING")
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkflowError("CODEX_WORKSPACE_GIT_UNVERIFIED") from exc
    if result.returncode:
        _fail("CODEX_WORKSPACE_GIT_UNVERIFIED")
    if result.stdout.strip():
        _fail("CODEX_WORKSPACE_DIRTY")


def _resource_path_overlap(left, right):
    left = os.path.normcase(str(Path(left).resolve(strict=False))).rstrip("\\/")
    right = os.path.normcase(str(Path(right).resolve(strict=False))).rstrip("\\/")
    return (
        left == right
        or left.startswith(right + os.sep)
        or right.startswith(left + os.sep)
    )


def _archive_resources_clear(project, binding, path):
    for claim in _resource_call(
        ResourceRegistry(project.root).list_for_work, binding.work_uid
    ):
        if claim["kind"] == "slot":
            _fail("RESOURCE_EXECUTION_SLOT_ACTIVE")
        if claim["kind"] not in {"path", "build_root"}:
            continue
        if (
            claim["kind"] == "path"
            and claim["disposition"] == "archive"
            and _same_resource_path(claim["identity"], path)
        ):
            continue
        if _resource_path_overlap(claim["identity"], path):
            _fail("RESOURCE_PATH_ACTIVE " + claim["identity"])


def _proof_contains_head(project, reachability, head):
    if not isinstance(head, str) or not head:
        return False
    for row in reachability.get("commits") or []:
        commit = row.get("commit")
        if row.get("reachable") is not True or not isinstance(commit, str):
            continue
        if commit.lower() == head.lower():
            return True
        result = is_ancestor(project.root, head, commit)
        if result is not None and result.returncode == 0:
            return True
    return False


def _release_archive_claims(project, path, work_uid):
    registry = ResourceRegistry(project.root)
    released = []
    for claim in _resource_call(registry.list_for_work, work_uid):
        if (
            claim["kind"] != "path"
            or claim["disposition"] != "archive"
            or not _same_resource_path(claim["identity"], path)
        ):
            continue
        record = _resource_call(registry.get, claim["request_uid"])
        if record is not None and record["state"] != "released":
            released.append(
                _resource_call(registry.release, claim["request_uid"], record["owner"])
            )
    return released


def _workspace(project, state, binding, args):
    action = args.get("action")
    if action not in {"create", "archive", "restore", "confirm", "status"}:
        _fail("NATIVE_WORKSPACE_ACTION_INVALID")
    request_uid = args.get("request_uid")
    if not isinstance(request_uid, str) or not request_uid:
        _fail("REQUEST_UID_REQUIRED")
    if action == "status":
        binding = _authoritative(project, state, binding, ())
        payload = _payload(args)
        adapter = CodexWorkspaceAdapter(project.root, state["project_id"])
        intent = adapter.host_action(request_uid)
        if intent.work_uid != binding.work_uid:
            _fail("WORK_UID_MISMATCH")
        result = adapter.host_action_result(request_uid)
        record = _resource_call(ResourceRegistry(project.root).get, request_uid)
        return {
            "schema": 1,
            "read_only": True,
            "request_uid": request_uid,
            "intent": intent.to_dict(),
            "result": result.to_dict() if result is not None else None,
            "resource": record,
        }
    binding = _controller(project, state, binding)
    payload = _payload(args)
    adapter = CodexWorkspaceAdapter(project.root, state["project_id"])
    owner = payload.get("owner") or binding.host
    if not isinstance(owner, str) or not owner:
        _fail("CODEX_WORKSPACE_NATIVE_OWNER_REQUIRED")
    if action == "create":
        request = _request(
            payload, request_uid, "native-worktree-create", state,
            payload.get("contract_digest") or contract_hash(
                project, state, project.paths(state["id"])[1]
            ),
            binding,
        )
        worktree = _worktree_request(payload, request)
        adapter.preview_create(worktree,task_uid=payload.get('task_uid') or args.get('task') or 'all',owner=owner)
        resource = _reserve_create(project, worktree, binding, request_uid)
        try:
            _controller(project, state, binding)
            intent = adapter.plan_create(
                worktree,
                task_uid=payload.get("task_uid") or args.get("task") or "all",
                owner=owner,
                actor_binding=binding,
            )
        except Exception as exc:
            try:
                _release_create_if_unissued(
                    project, adapter, request_uid, resource
                )
            except Exception as release_exc:
                raise WorkflowError(
                    "RESOURCE_CREATE_RECOVERY_UNKNOWN "
                    + str(exc)
                    + "; "
                    + str(release_exc)
                ) from exc
            raise
        return {**intent.to_dict(), "resource_intent": resource}
    if action == "archive":
        current_contract = contract_hash(
            project, state, project.paths(state["id"])[1]
        )
        request = _request(
            payload, request_uid, "native-worktree-archive", state,
            current_contract,
            binding,
        )
        workspace = payload.get("workspace") or payload.get("binding")
        if not isinstance(workspace, Mapping):
            _fail("CODEX_WORKSPACE_BINDING_REQUIRED")
        path = workspace.get("path")
        head = workspace.get("head")
        if not isinstance(path, (str, Path)):
            _fail("CODEX_WORKSPACE_PATH_REQUIRED")
        manifest = _manifest(payload)
        core_manifest = manifest.get("manifest") or {}
        manifest_owner = manifest.get("owner") or {}
        if (
            core_manifest.get("work_uid") != binding.work_uid
            or manifest_owner.get("work_uid") != binding.work_uid
        ):
            _fail("WORK_UID_MISMATCH")
        if core_manifest.get("contract_digest") != current_contract:
            _fail("EVIDENCE_CONTRACT_CHANGED")
        verification = verify_manifest(project.root, manifest)
        reachability = verify_git_reachability(project.root, manifest)
        if not verification.get("accepted") or not reachability.get("accepted"):
            _fail("EVIDENCE_ARCHIVE_PROOF_REQUIRED")
        if not _proof_contains_head(project, reachability, head):
            _fail("EVIDENCE_GIT_HEAD_ANCHOR_REQUIRED")
        evidence_digest = manifest.get("integrity_digest") or (
            verification.get("manifest_digest")
        )
        if len(str(evidence_digest or "")) not in {32, 64}:
            _fail("HOST_ACTION_EVIDENCE_REQUIRED")
        _archive_state(project, state)
        _archive_resources_clear(project, binding, path)
        _workspace_clean(path)
        return adapter.plan_archive(
            workspace,
            request=_worktree_request(payload, request),
            actor_binding=binding,
            owner=owner,
            evidence_digest=evidence_digest,
        ).to_dict()
    if action == "restore":
        request = _request(
            payload, request_uid, "native-worktree-restore", state,
            payload.get("contract_digest") or contract_hash(
                project, state, project.paths(state["id"])[1]
            ),
            binding,
        )
        archived = payload.get("archived")
        if not isinstance(archived, Mapping):
            _fail("HOST_ACTION_ARCHIVE_REQUIRED")
        return adapter.plan_restore(
            archived,
            request=_worktree_request(payload, request),
            actor_binding=binding,
            owner=owner,
        ).to_dict()
    if action == "confirm":
        receipt = payload.get("receipt")
        intent_key = payload.get("intent_key") or payload.get("host_action")
        if isinstance(intent_key, str):
            intent = adapter.host_action(intent_key)
        elif isinstance(intent_key, Mapping):
            intent = intent_key
        else:
            _fail("HOST_ACTION_INTENT_REQUIRED")
        intent_work = (
            intent.get("work_uid") if isinstance(intent, Mapping)
            else intent.work_uid
        )
        intent_action = (
            intent.get("action") if isinstance(intent, Mapping)
            else intent.action
        )
        if intent_work != binding.work_uid:
            _fail("WORK_UID_MISMATCH")
        if intent_action == "create_worktree":
            raw = payload.get("intent") or payload.get("create_intent")
            if not isinstance(raw, Mapping):
                _fail("CODEX_CREATE_INTENT_REQUIRED")
            raw = {
                key: raw.get(key)
                for key in {
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
            }
            create_intent = CodexCreateIntent.from_dict(raw)
            if create_intent.request.request.request_uid != request_uid:
                _fail("REQUEST_SCOPE_MISMATCH")
            pending = adapter.host_action(request_uid)
            if (
                pending.action != "create_worktree"
                or pending.request.to_dict() != create_intent.request.request.to_dict()
                or pending.owner != create_intent.owner
                or pending.task_uid != create_intent.task_uid
            ):
                _fail("HOST_ACTION_REQUEST_CHANGED")
            source_root = payload.get("source_root") or create_intent.request.path_hint
            result = adapter.verify_create(
                create_intent, receipt, source_root,
                expected_head=payload.get("expected_head"),
                allow_dirty=payload.get("allow_dirty") is True,
            )
            return _commit_created_path(
                project, binding, request_uid, result["path"], result
            )
        if intent_action == "archive_worktree":
            result = adapter.confirm_archive(intent, receipt)
            path = (
                result.get("binding", {}).get("path")
                or getattr(intent, "attachment_identity_key", None)
                or intent.get("attachment_identity_key")
            )
            released = _release_archive_claims(project, path, binding.work_uid)
            return {**result, "released_resource_claims": released}
        if intent_action == "restore_worktree":
            result = adapter.confirm_restore(intent, receipt)
            path = result.get("binding", {}).get("path")
            if not isinstance(path, str) or not path:
                _fail("CODEX_WORKSPACE_PATH_REQUIRED")
            return _commit_restored_path(
                project, binding, request_uid, path, result
            )
        _fail("HOST_ACTION_ACTION_MISMATCH")
    _fail("NATIVE_WORKSPACE_ACTION_INVALID")


def _evidence(project, state, binding, args):
    command = args.get("command")
    if command not in {"evidence-collect", "evidence-verify"}:
        _fail("RESOURCE_COMMAND_INVALID")
    if command == "evidence-verify":
        binding = _authoritative(project, state, binding, ())
    else:
        binding = _controller(project, state, binding)
    payload = _payload(args)
    request_uid = args.get("request_uid") or payload.get("request_uid")
    if not isinstance(request_uid, str) or not request_uid:
        _fail("REQUEST_UID_REQUIRED")
    task = args.get("task") or payload.get("task") or "all"
    if task != "all" and task not in {
        row.get("id") for row in state.get("tasks", [])
    }:
        _fail("TASK_NOT_FOUND " + str(task))
    if command == "evidence-verify":
        manifest = _manifest(payload)
        if manifest["manifest"].get("work_uid") != binding.work_uid:
            _fail("WORK_UID_MISMATCH")
        return verify_git_reachability(project.root, manifest)
    sources = _evidence_sources(project, payload, args)
    input_digest = payload.get("input_digest") or _evidence_input(
        project, state, task, sources
    )
    contract = payload.get("contract_digest") or contract_hash(
        project, state, project.paths(state["id"])[1]
    )
    request = _request(
        {**payload, "input_digest": input_digest},
        request_uid,
        "evidence-collect",
        state,
        contract,
        binding,
    )
    manifest = collect_evidence(
        project.root,
        _work_uid(state),
        task,
        sources,
        contract,
        input_digest,
        binding=binding,
        request=request,
    )
    verification = verify_manifest(project.root, manifest)
    if not verification.get("accepted"):
        _fail("EVIDENCE_VERIFY_FAILED " + ",".join(verification.get("errors", [])))
    commits = args.get("commit") or payload.get("commits") or []
    if isinstance(commits, str):
        commits = [commits]
    if commits:
        manifest = persist_git_objects(
            project.root, binding, manifest, commits, request
        )
        reachability = verify_git_reachability(project.root, manifest)
        if not reachability.get("accepted"):
            _fail("EVIDENCE_GIT_REACHABILITY_FAILED")
    return manifest


def handle_resource(project, state, args, *, binding):
    """Run one bounded resource business operation."""
    if not isinstance(args, Mapping):
        _fail("RESOURCE_ARGS_INVALID")
    command = args.get("command")
    try:
        if command == "native-workspace":
            return _workspace(project, state, binding, args), True
        if command in {"evidence-collect", "evidence-verify"}:
            return _evidence(project, state, binding, args), True
        _fail("RESOURCE_COMMAND_INVALID")
    except WorkflowError as exc:
        return {"accepted": False, "error": str(exc)}, False


def preserve_work_evidence(project, state, *, binding, request_uid, commits=()):
    """Preserve existing receipts, logs, results and review before cleanup."""
    binding = _controller(project, state, binding)
    if not isinstance(request_uid, str) or not request_uid:
        _fail("REQUEST_UID_REQUIRED")
    _archive_state(project, state)
    root = project.root.resolve()
    sources = []
    evidence_relative = Path(".gsp") / "runs" / state["id"] / "evidence"

    def add_source(path, label):
        raw = Path(path)
        if raw.is_symlink():
            _fail("EVIDENCE_CONTROLLED_LINK_REFUSED " + str(raw))
        path = raw.resolve(strict=True)
        if (
            not path.is_relative_to(root)
            or not path.is_file()
        ):
            _fail("EVIDENCE_CONTROLLED_FILE_REQUIRED " + str(path))
        sources.append(
            {
                "path": path,
                "label": label,
                "kind": "file",
                "required": True,
            }
        )

    for relative in state.get("evidence") or []:
        if not isinstance(relative, str):
            _fail("EVIDENCE_RECEIPT_PATH_INVALID")
        receipt_path = (root / relative).resolve(strict=False)
        raw_receipt = root / relative
        if (
            not receipt_path.is_relative_to(root)
            or not receipt_path.is_relative_to(root / evidence_relative)
            or receipt_path.name != "receipt.json"
            or not receipt_path.is_file()
            or raw_receipt.is_symlink()
        ):
            _fail("EVIDENCE_RECEIPT_REQUIRED " + str(relative))
        receipt = read(receipt_path)
        if not isinstance(receipt, Mapping):
            _fail("EVIDENCE_RECEIPT_INVALID " + str(relative))
        add_source(receipt_path, receipt_path.relative_to(root).as_posix())
        logs = receipt.get("logs")
        if not isinstance(logs, Mapping) or not logs:
            _fail("EVIDENCE_LOGS_REQUIRED " + str(relative))
        for name, expected in logs.items():
            if not isinstance(name, str) or Path(name).name != name:
                _fail("EVIDENCE_LOG_NAME_INVALID " + str(name))
            path = receipt_path.parent / name
            if path.is_symlink() or not path.is_file():
                _fail("EVIDENCE_LOG_REQUIRED " + str(path))
            from .core import digest
            if digest(path) != expected:
                _fail("EVIDENCE_LOG_CHANGED " + str(path))
            add_source(path, path.resolve().relative_to(root).as_posix())
        command_result = receipt.get("execution_result")
        if command_result:
            path = Path(command_result)
            if not path.is_absolute():
                path = root / path
            if path.is_symlink() or not path.is_file():
                _fail("EVIDENCE_COMMAND_RESULT_REQUIRED " + str(path))
            add_source(path, path.resolve().relative_to(root).as_posix())
    _, docs = project.paths(state["id"])
    for name in ("review.md", "decisions.md"):
        path = docs / name
        if path.is_file():
            add_source(path, path.relative_to(root).as_posix())
    if state.get("review") and not (docs / "review.md").is_file():
        _fail("EVIDENCE_REVIEW_REQUIRED")
    if not sources:
        _fail("EVIDENCE_SOURCES_REQUIRED")
    contract = contract_hash(project, state, project.paths(state["id"])[1])
    input_digest = digest_value(
        {
            "request_uid": request_uid,
            "work_uid": _work_uid(state),
            "evidence": state.get("evidence") or [],
            "commits": list(commits),
            "contract": contract,
            "source_digests": [(source['label'],digest(source['path'])) for source in sources],
        }
    )
    request = _request(
        {"input_digest": input_digest},
        request_uid,
        "evidence-collect",
        state,
        contract,
        binding,
    )
    manifest = collect_evidence(
        project.root,
        _work_uid(state),
        "all",
        sources,
        contract,
        input_digest,
        binding=binding,
        request=request,
    )
    if not verify_manifest(project.root, manifest).get("accepted"):
        _fail("EVIDENCE_VERIFY_FAILED")
    if commits:
        manifest = persist_git_objects(
            project.root, binding, manifest, commits, request
        )
        if not verify_git_reachability(project.root, manifest).get("accepted"):
            _fail("EVIDENCE_GIT_REACHABILITY_FAILED")
    return manifest


def ensure_work_evidence(project,state,*,binding):
    """Retain this work's evidence/commits once before owned disposal."""
    if not state.get('evidence'):
        return None
    from .evidence_store import load_manifest
    commits=sorted({topology.get('head') for topology in [state.get('git') or {},
        state.get('git_topology') or {},*[task.get('topology') or {} for task in state.get('tasks',[])]]
        if topology.get('head')})
    key=digest_value({'work':_work_uid(state),'evidence':state['evidence'],
        'contract':contract_hash(project,state,project.paths(state['id'])[1]),'commits':commits,
        'review':state.get('review')})
    saved=state.get('durable_evidence') or {}
    if saved.get('input_key')==key:
        envelope=load_manifest(project.root,_work_uid(state),saved['manifest_uid'])
        decision=verify_git_reachability(project.root,envelope) if commits else verify_manifest(project.root,envelope)
        if not decision.get('accepted'):
            _fail('EVIDENCE_DURABILITY_LOST')
        return saved
    uid=digest_value({'kind':'work-disposal-evidence','key':key,'binding':binding.binding_uid})
    envelope=preserve_work_evidence(project,state,binding=binding,request_uid=uid,commits=commits)
    state['durable_evidence']={'input_key':key,'manifest_uid':envelope['manifest']['uid']}
    if hasattr(project,'write'):
        project.write(state,'evidence-durable',uid,action_uid='evidence-durable:'+uid)
    return state['durable_evidence']


__all__ = ["handle_resource", "preserve_work_evidence","ensure_work_evidence"]
