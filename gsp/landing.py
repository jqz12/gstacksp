"""Bounded Git landing: durable intent, candidate proof and CAS publication.

This module is a business API.  It never reads a plan, a ledger, a task
contract or a workspace projection.  A caller supplies one immutable
``LandingRequest``; this module persists that intent under the project control
root and performs one bounded local Git transition.

Callback protocol
-----------------
``check_gate(context)`` and ``cancel_check(context)`` receive one mapping with
stable keys: ``root``, ``phase``, ``request_uid``, ``work_uid``,
``binding_uid``, ``controller_generation``, ``cancel_generation``,
``target_ref``, ``expected_target_sha``, ``candidate_sha``, ``final_sha``,
``evidence_uid`` and ``input_digest``.  ``check_gate`` must return ``True`` or
a mapping with ``accepted`` exactly ``True``; every other value, a missing
callback, or an exception blocks the operation.  ``cancel_check`` returns
truthy when cancellation was requested.

``verify_candidate(context)`` is called after the real final candidate (the
source commit for a fast-forward, or a constructed merge commit for diverged
history) exists.  It must return a mapping with ``accepted`` exactly ``True``,
``candidate_sha`` equal to ``context['candidate_sha']`` and ``target_ref``
equal to ``context['target_ref']``.  A bare ``True`` is not evidence.

Records live at ``<root>/.gsp/landing/<sha256(request_uid)>.json`` so they are
independent of any one worktree.  The same request UID and complete input
digest replay the same record; any changed input fails closed.
"""
from __future__ import annotations

from contextlib import contextmanager, closing
from dataclasses import replace
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
import time
from typing import Any, Callable, Mapping, Optional

from .core import WorkflowError, atomic, now, read, sha
from .identity_store import IdentityStore, IdentityStoreError
from .orchestration_types import (
    LandingRequest,
    RequestRef,
    SessionBinding,
    digest_value,
)
from .resource_registry import (
    ResourceConflictError,
    ResourceOwner,
    ResourceRegistry,
    ResourceRegistryError,
)
from .session_context import binding_state, canonical_binding_digest


SCHEMA = 1
RECORD_DIRECTORY = Path(".gsp") / "landing"
LOCK_STALE_SECONDS = 600

STATE_PREPARING = "preparing"
STATE_PREPARED = "prepared"
STATE_PENDING = "pending"
STATE_LANDED = "landed"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({STATE_LANDED, STATE_FAILED, STATE_CANCELLED})
RETRYABLE_STATES = frozenset({STATE_PREPARING, STATE_PENDING})


def _fail(code, detail=""):
    message = (code + (" " + str(detail) if detail else "")).strip()
    raise WorkflowError(message)


def _text(value, code):
    if not isinstance(value, str) or not value.strip():
        _fail(code)
    return value.strip()


_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def _commit_text(value, code):
    value = _text(value, code)
    if _SHA.fullmatch(value) is None:
        _fail(code, value)
    return value


def _canonical_path(value):
    try:
        return str(Path(value).resolve())
    except (OSError, TypeError) as exc:
        raise WorkflowError("LANDING_PATH_INVALID") from exc


def _record_name(uid):
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()


def _record_path(root, uid):
    return Path(root) / RECORD_DIRECTORY / (_record_name(uid) + ".json")


@contextmanager
def _record_lock(root, uid):
    """Serialize one landing record without touching the Git index."""
    path = Path(root) / RECORD_DIRECTORY / (_record_name(uid) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600,
        )
    except FileExistsError:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0
        if age < LOCK_STALE_SECONDS:
            _fail("LANDING_BUSY", uid)
        try:
            path.unlink()
            descriptor = os.open(
                str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600,
            )
        except OSError as exc:
            _fail("LANDING_BUSY", uid)
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.close(descriptor)
        yield path
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def _load_record(root, uid):
    path = _record_path(root, uid)
    if not path.is_file():
        return None
    try:
        record = read(path)
    except WorkflowError as exc:
        raise WorkflowError("LANDING_RECORD_CORRUPT " + uid) from exc
    if (
        not isinstance(record, dict)
        or record.get("schema") != SCHEMA
        or record.get("uid") != uid
        or record.get("state") not in {
            STATE_PREPARING, STATE_PREPARED, STATE_PENDING,
            STATE_LANDED, STATE_FAILED, STATE_CANCELLED,
        }
    ):
        _fail("LANDING_RECORD_CORRUPT", uid)
    return record


def _save_record(root, record):
    record["updated_at"] = now()
    record["result"] = {
        "state": record["state"],
        "actual_sha": record.get("actual_sha"),
        "proof_uid": record.get("proof_uid"),
    }
    atomic(_record_path(root, record["uid"]), record)
    return record


def _require_root(root):
    try:
        path = Path(root).resolve()
    except (OSError, TypeError) as exc:
        raise WorkflowError("LANDING_ROOT_INVALID") from exc
    if not path.is_dir():
        _fail("LANDING_ROOT_INVALID", str(path))
    return path


def _require_store(root):
    store = IdentityStore(root)
    if not store.path.is_file():
        _fail("LANDING_AUTHORITY_MISSING", str(store.path))
    return store


def _require_authority(store, binding, request):
    if not isinstance(binding, SessionBinding):
        _fail("SESSION_BINDING_REQUIRED")
    if binding.session is None or not getattr(binding.session, "uid", ""):
        _fail("SESSION_BINDING_REQUIRED")
    if not isinstance(request, RequestRef):
        _fail("LANDING_REQUEST_REQUIRED")
    try:
        immutable = store.read_session_binding(binding.binding_uid)
        state = store.read_session_state(binding.binding_uid)
        project_uid = store.project_uid()
    except IdentityStoreError as exc:
        raise WorkflowError(str(exc)) from exc
    if immutable is None or immutable != canonical_binding_digest(binding):
        _fail("SESSION_CONTEXT_TAMPERED")
    if state is None or state != binding_state(binding):
        _fail("SESSION_BINDING_STALE")
    if state["cancelled"]:
        _fail("SESSION_BINDING_STALE", "cancelled binding")
    if binding.project_uid != project_uid or request.project_uid != project_uid:
        _fail("SESSION_PROJECT_MISMATCH")
    if request.work_uid is None or binding.work_uid != request.work_uid:
        _fail("WORK_UID_MISMATCH")
    if "write" not in binding.capabilities:
        _fail("CAPABILITY_REQUIRED", "write")
    if (
        request.controller_generation is not None
        and request.controller_generation != state["controller_generation"]
    ):
        _fail("CONTROL_VERSION_CHANGED")
    if (
        request.cancel_generation is not None
        and request.cancel_generation != state["cancel_generation"]
    ):
        _fail("CONTROL_VERSION_CHANGED")
    try:
        owner = store.read_work_controller(binding.work_uid)
        session = store.resolve("session", binding.session.uid, None, write=False)
    except IdentityStoreError as exc:
        raise WorkflowError(str(exc)) from exc
    if owner is not None and owner["binding_uid"] != binding.binding_uid:
        _fail("CONTROLLER_LEASE_HELD", owner["binding_uid"])
    if session.uid != binding.session.uid or session.project_uid != project_uid:
        _fail("SESSION_BINDING_STALE", "session identity changed")


def _operation_actor(root, record, binding=None):
    """Fence each call while preserving the original immutable landing intent.

    An explicit current controller can continue the same work's intent after a
    handoff. Its authority is checked afresh, including terminal-result replay;
    the original resource owner remains the owner of the original reservation.
    """
    store = _require_store(root)
    original = SessionBinding.from_dict(record['input']['binding'])
    request = RequestRef.from_dict(record['input']['request'])
    actor = original if binding is None else binding
    if not isinstance(actor, SessionBinding):
        _fail('SESSION_BINDING_REQUIRED')
    effective = replace(request, controller_generation=actor.controller_generation,
                        cancel_generation=actor.cancel_generation)
    _require_authority(store, actor, effective)
    if actor.binding_uid != original.binding_uid:
        from .session_context import require_capability, require_decision_controller
        require_capability(actor, 'accept', request.work_uid)
        require_decision_controller(store, actor, request.work_uid)
        if str(actor.common_dir) != str(original.common_dir):
            _fail('LANDING_WORKSPACE_MISMATCH')
    return actor, effective


def _git(path, *args, binary=False, env=None, timeout=60):
    command = ["git", *[str(arg) for arg in args]]
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    if env:
        environment.update(env)
    try:
        return subprocess.run(
            command,
            cwd=str(path),
            capture_output=True,
            text=not binary,
            encoding=None if binary else "utf-8",
            errors=None if binary else "replace",
            timeout=timeout,
            env=environment,
            creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            ),
        )
    except FileNotFoundError as exc:
        raise WorkflowError("LANDING_GIT_UNAVAILABLE") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorkflowError("LANDING_GIT_TIMEOUT") from exc


def _git_ok(path, *args, code="LANDING_GIT_FAILED", **kwargs):
    result = _git(path, *args, **kwargs)
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        _fail(code, detail[:500])
    return result


def _repo_identity(path):
    result = _git(
        path,
        "rev-parse",
        "--show-toplevel",
        "--git-common-dir",
        "--git-dir",
        "--verify",
        "HEAD",
    )
    if result.returncode:
        _fail(
            "LANDING_WORKSPACE_INVALID",
            (result.stderr or result.stdout or "").strip()[:500],
        )
    lines = result.stdout.splitlines()
    if len(lines) < 4:
        _fail("LANDING_WORKSPACE_INVALID")
    top = Path(lines[0].strip()).resolve()
    common = Path(lines[1].strip())
    if not common.is_absolute():
        common = (Path(path) / common).resolve()
    else:
        common = common.resolve()
    git_dir = Path(lines[2].strip())
    if not git_dir.is_absolute():
        git_dir = (Path(path) / git_dir).resolve()
    else:
        git_dir = git_dir.resolve()
    return {
        "toplevel": top,
        "common_dir": common,
        "git_dir": git_dir,
        "head": lines[3].strip(),
    }


def _same_repository(root_identity, workspace_identity):
    return root_identity["common_dir"] == workspace_identity["common_dir"]


def _worktrees(path):
    result = _git_ok(path, "worktree", "list", "--porcelain")
    entries = []
    current = {}
    for line in result.stdout.splitlines() + [""]:
        if not line:
            if current:
                entries.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = str(Path(value).resolve())
        elif key in {"HEAD", "branch"}:
            current[key] = value
        elif key in {"bare", "detached", "locked", "prunable"}:
            current[key] = value or True
    return entries


def _target_branch(target_ref):
    return target_ref[len("refs/heads/"):]


def _validate_target(path, target_ref, expected_sha):
    target_ref = _text(target_ref, "LANDING_TARGET_INVALID")
    if not target_ref.startswith("refs/heads/") or len(target_ref) <= len(
        "refs/heads/"
    ):
        _fail("LANDING_TARGET_INVALID", target_ref)
    checked = _git(path, "check-ref-format", target_ref)
    if checked.returncode:
        _fail("LANDING_TARGET_INVALID", target_ref)
    expected_sha = _commit_text(expected_sha, "LANDING_EXPECTED_INVALID")
    actual = _git(path, "rev-parse", "--verify", "--quiet", target_ref)
    if actual.returncode:
        _fail("LANDING_TARGET_MISSING", target_ref)
    actual_sha = actual.stdout.strip()
    object_type = _git(path, "cat-file", "-t", actual_sha)
    if object_type.returncode or object_type.stdout.strip() != "commit":
        _fail("LANDING_TARGET_INVALID", target_ref)
    return target_ref, expected_sha, actual_sha


def _require_commit(path, value, code):
    value = _commit_text(value, code)
    result = _git(path, "cat-file", "-t", value)
    if result.returncode or result.stdout.strip() != "commit":
        _fail(code, value)
    return value


def _is_ancestor(path, ancestor, descendant):
    result = _git(path, "merge-base", "--is-ancestor", ancestor, descendant)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    _fail(
        "LANDING_GIT_FAILED",
        (result.stderr or result.stdout or "").strip()[:500],
    )


_IDENTITY_LINE = re.compile(
    r"^(author|committer) (.*) <([^<>]*)> (\d+) ([+-]\d{4})$"
)


def _commit_identity(path, commit_sha):
    values = {}
    result = _git_ok(path, "cat-file", "-p", commit_sha)
    for line in result.stdout.splitlines():
        match = _IDENTITY_LINE.match(line)
        if match:
            values[match.group(1)] = {
                "name": match.group(2),
                "email": match.group(3),
                "date": match.group(4) + " " + match.group(5),
            }
    fallback = {
        "name": "GSP Landing",
        "email": "gsp-landing@localhost",
        "date": "946684800 +0000",
    }
    return {
        "GIT_AUTHOR_NAME": values.get("author", fallback)["name"],
        "GIT_AUTHOR_EMAIL": values.get("author", fallback)["email"],
        "GIT_AUTHOR_DATE": values.get("author", fallback)["date"],
        "GIT_COMMITTER_NAME": values.get("committer", fallback)["name"],
        "GIT_COMMITTER_EMAIL": values.get("committer", fallback)["email"],
        "GIT_COMMITTER_DATE": values.get("committer", fallback)["date"],
    }


def _merge_candidate(path, expected_sha, candidate_sha, request_uid, target_ref):
    result = _git(
        path,
        "merge-tree",
        "--write-tree",
        expected_sha,
        candidate_sha,
    )
    if result.returncode:
        if result.returncode == 1 or "CONFLICT" in result.stdout:
            _fail("LANDING_CANDIDATE_CONFLICT", candidate_sha)
        detail = (result.stderr or result.stdout or "").strip()
        _fail("LANDING_GIT_UNSUPPORTED", detail[:500])
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines or _SHA.fullmatch(lines[0]) is None:
        _fail("LANDING_GIT_UNSUPPORTED", "merge-tree returned no tree")
    tree = lines[0]
    message = (
        "GSP landing merge " + candidate_sha[:12]
        + " into " + target_ref
        + "\n\nrequest " + request_uid + "\n"
    )
    created = _git(
        path,
        "commit-tree",
        tree,
        "-p",
        expected_sha,
        "-p",
        candidate_sha,
        "-m",
        message,
        env=_commit_identity(path, expected_sha),
    )
    if created.returncode:
        detail = (created.stderr or created.stdout or "").strip()
        _fail("LANDING_GIT_FAILED", detail[:500])
    final_sha = created.stdout.strip()
    _commit_text(final_sha, "LANDING_GIT_FAILED")
    return final_sha


def _final_candidate(path, expected_sha, candidate_sha, request_uid, target_ref):
    candidate_sha = _require_commit(
        path, candidate_sha, "LANDING_CANDIDATE_INVALID"
    )
    if _is_ancestor(path, expected_sha, candidate_sha):
        return candidate_sha, True
    return (
        _merge_candidate(
            path, expected_sha, candidate_sha, request_uid, target_ref,
        ),
        False,
    )


def _context(
    root,
    phase,
    request_uid,
    request,
    binding_uid,
    controller_generation,
    cancel_generation,
    target_ref,
    expected_sha,
    source_candidate_sha,
    final_sha,
    evidence_uid,
    input_digest,
):
    return {
        "schema": SCHEMA,
        "root": str(root),
        "phase": phase,
        "request_uid": request_uid,
        "logical_key": request.logical_key,
        "work_uid": request.work_uid,
        "binding_uid": binding_uid,
        "controller_generation": controller_generation,
        "cancel_generation": cancel_generation,
        "target_ref": target_ref,
        "expected_target_sha": expected_sha,
        "source_candidate_sha": source_candidate_sha,
        "candidate_sha": final_sha,
        "final_sha": final_sha,
        "evidence_uid": evidence_uid,
        "input_digest": input_digest,
    }


def _gate(check_gate, context):
    if not callable(check_gate):
        _fail("LANDING_GATE_UNAVAILABLE")
    try:
        decision = check_gate(context)
    except WorkflowError:
        raise
    except Exception as exc:  # noqa: BLE001 - callback boundary
        raise WorkflowError("LANDING_GATE_ERROR " + type(exc).__name__) from exc
    if decision is True:
        return {"schema": SCHEMA, "accepted": True, "decision": True}
    if not isinstance(decision, Mapping) or decision.get("accepted") is not True:
        _fail("LANDING_GATE_BLOCKED")
    payload = dict(decision)
    payload.setdefault("schema", SCHEMA)
    try:
        digest_value(payload)
    except (TypeError, ValueError) as exc:
        raise WorkflowError("LANDING_GATE_BLOCKED") from exc
    return payload


def _verification(verify_candidate, context, expected_sha, evidence_uid):
    if not callable(verify_candidate):
        _fail("LANDING_VERIFICATION_UNAVAILABLE")
    try:
        proof = verify_candidate(context)
    except WorkflowError:
        raise
    except Exception as exc:  # noqa: BLE001 - callback boundary
        raise WorkflowError(
            "LANDING_VERIFICATION_ERROR " + type(exc).__name__
        ) from exc
    if (
        not isinstance(proof, Mapping)
        or proof.get("accepted") is not True
        or proof.get("candidate_sha") != context["candidate_sha"]
        or proof.get("target_ref") != context["target_ref"]
    ):
        _fail("LANDING_VERIFICATION_REJECTED")
    if (
        proof.get("expected_target_sha") is not None
        and proof.get("expected_target_sha") != expected_sha
    ):
        _fail("LANDING_VERIFICATION_REJECTED")
    if (
        evidence_uid
        and proof.get("evidence_uid") is not None
        and proof.get("evidence_uid") != evidence_uid
    ):
        _fail("LANDING_VERIFICATION_REJECTED")
    payload = dict(proof)
    payload.setdefault("schema", SCHEMA)
    payload.setdefault("evidence_uid", evidence_uid)
    try:
        digest_value(payload)
    except (TypeError, ValueError) as exc:
        raise WorkflowError("LANDING_VERIFICATION_REJECTED") from exc
    return payload


def _cancelled(cancel_check, context):
    if cancel_check is None:
        return False
    if not callable(cancel_check):
        _fail("LANDING_CANCEL_CHECK_INVALID")
    try:
        return bool(cancel_check(context))
    except WorkflowError:
        raise
    except Exception as exc:  # noqa: BLE001 - callback boundary
        raise WorkflowError(
            "LANDING_CANCEL_CHECK_ERROR " + type(exc).__name__
        ) from exc


def _input(request, workspace):
    return {
        "request": request.request.to_dict(),
        "binding": request.binding.to_dict(),
        "target_ref": request.target_ref,
        "expected_target_sha": request.expected_target_sha,
        "candidate_sha": request.candidate_sha,
        "evidence_uid": request.evidence_uid,
        "workspace": workspace,
    }


def _reserve(root, record):
    registry = ResourceRegistry(root)
    owner = ResourceOwner(
        record["work_uid"],
        record["session_uid"],
        record["controller_generation"],
    )
    try:
        registry.reserve(
            record["resource_request_uid"],
            owner,
            [{"kind": "branch", "identity": record["target_ref"]}],
        )
    except ResourceConflictError as exc:
        raise WorkflowError("LANDING_TARGET_BUSY " + record["target_ref"]) from exc
    except ResourceRegistryError as exc:
        raise WorkflowError(str(exc)) from exc


def _release(root, record):
    if not record.get("resource_request_uid"):
        return None
    registry = ResourceRegistry(root)
    owner = ResourceOwner(
        record["work_uid"],
        record["session_uid"],
        record["controller_generation"],
    )
    try:
        registry.release(record["resource_request_uid"], owner)
    except Exception as exc:  # noqa: BLE001 - release is best effort
        return str(exc)
    return None


def _mark_pending(root, record, code, pending_action=None):
    record["state"] = STATE_PENDING
    record["last_error"] = code
    record["pending_action"] = pending_action
    _save_record(root, record)


def _mark_failed(root, record, code):
    record["state"] = STATE_FAILED
    record["last_error"] = code
    record["pending_action"] = None
    record["release_error"] = _release(root, record)
    _save_record(root, record)


def _mark_cancelled(root, record, phase):
    record["state"] = STATE_CANCELLED
    record["phase"] = phase
    record["pending_action"] = None
    record["last_error"] = None
    record["release_error"] = _release(root, record)
    _save_record(root, record)
    return record


def _proof(record, method, checkout_path=None):
    proof = {
        "schema": SCHEMA,
        "request_uid": record["uid"],
        "target_ref": record["target_ref"],
        "expected_target_sha": record["expected_target_sha"],
        "candidate_sha": record["candidate_sha"],
        "final_sha": record["final_sha"],
        "actual_sha": record["actual_sha"],
        "method": method,
        "checkout_path": checkout_path,
        "recorded_at": now(),
    }
    proof["uid"] = digest_value(proof)
    return proof


def _mark_landed(root, record, method, checkout_path=None):
    record["state"] = STATE_LANDED
    record["phase"] = "landed"
    record["pending_action"] = None
    record["last_error"] = None
    record["proof"] = _proof(record, method, checkout_path)
    record["proof_uid"] = record["proof"]["uid"]
    record["release_error"] = _release(root, record)
    _save_record(root, record)
    return record


def _record_from_request(root, request, workspace):
    source = request.request
    snapshot = _input(request, workspace)
    input_digest = digest_value(snapshot)
    return {
        "schema": SCHEMA,
        "uid": source.request_uid,
        "request_uid": source.request_uid,
        "logical_key": source.logical_key,
        "operation_kind": source.operation_kind,
        "input_digest": input_digest,
        "input": snapshot,
        "project_uid": source.project_uid,
        "work_uid": source.work_uid,
        "session_uid": request.binding.session.uid,
        "binding_uid": request.binding.binding_uid,
        "controller_generation": request.binding.controller_generation,
        "cancel_generation": request.binding.cancel_generation,
        "binding": request.binding.to_dict(),
        "target_ref": request.target_ref,
        "expected_target_sha": request.expected_target_sha,
        "candidate_sha": request.candidate_sha,
        "evidence_uid": request.evidence_uid,
        "workspace": workspace,
        "resource_request_uid": "landing:" + source.request_uid,
        "state": STATE_PREPARING,
        "phase": "intent",
        "final_sha": None,
        "fast_forward": None,
        "verification": None,
        "verification_digest": None,
        "gate": None,
        "actual_sha": None,
        "proof": None,
        "proof_uid": None,
        "pending_action": None,
        "last_error": None,
        "attempts": 0,
        "created_at": now(),
        "updated_at": now(),
    }


def _prepare_locked(
    root, record, check_gate, verify_candidate, cancel_check,
):
    record["attempts"] = int(record.get("attempts") or 0) + 1
    request = None
    try:
        request = LandingRequest(
            request=RequestRef.from_dict(record["input"]["request"]),
            binding=SessionBinding.from_dict(record["input"]["binding"]),
            target_ref=record["target_ref"],
            expected_target_sha=record["expected_target_sha"],
            candidate_sha=record["candidate_sha"],
            evidence_uid=record["evidence_uid"],
        )
        store = _require_store(root)
        _require_authority(store, request.binding, request.request)
        context = _context(
            root, "prepare", record["uid"], request.request,
            record["binding_uid"], record["controller_generation"],
            record["cancel_generation"], record["target_ref"],
            record["expected_target_sha"], record["candidate_sha"],
            record.get("final_sha"), record["evidence_uid"],
            record["input_digest"],
        )
        if _cancelled(cancel_check, context):
            return _mark_cancelled(root, record, "cancelled-before-prepare")
        root_identity = _repo_identity(root)
        workspace = Path(record["workspace"])
        workspace_identity = _repo_identity(workspace)
        if not _same_repository(root_identity, workspace_identity):
            _fail("LANDING_WORKSPACE_MISMATCH", record["workspace"])
        binding_common = str(request.binding.common_dir or "")
        if not binding_common or binding_common.startswith("non-git:"):
            _fail("LANDING_WORKSPACE_MISMATCH", binding_common)
        try:
            binding_common_path = Path(binding_common).resolve()
        except OSError as exc:
            raise WorkflowError("LANDING_WORKSPACE_MISMATCH") from exc
        if binding_common_path != workspace_identity["common_dir"]:
            _fail("LANDING_WORKSPACE_MISMATCH", binding_common)
        _reserve(root, record)
        target_ref, expected_sha, actual_sha = _validate_target(
            workspace, record["target_ref"], record["expected_target_sha"],
        )
        if actual_sha != expected_sha:
            _fail(
                "LANDING_TARGET_MISMATCH",
                target_ref + " " + actual_sha,
            )
        final_sha, fast_forward = _final_candidate(
            workspace, expected_sha, record["candidate_sha"],
            record["uid"], target_ref,
        )
        record["final_sha"] = final_sha
        record["fast_forward"] = fast_forward
        context = _context(
            root, "prepare-verify", record["uid"], request.request,
            record["binding_uid"], record["controller_generation"],
            record["cancel_generation"], target_ref, expected_sha,
            record["candidate_sha"], final_sha, record["evidence_uid"],
            record["input_digest"],
        )
        verification = _verification(
            verify_candidate, context, expected_sha, record["evidence_uid"],
        )
        record["verification"] = verification
        record["verification_digest"] = digest_value(verification)
        gate = _gate(check_gate, _context(
            root, "prepare", record["uid"], request.request,
            record["binding_uid"], record["controller_generation"],
            record["cancel_generation"], target_ref, expected_sha,
            record["candidate_sha"], final_sha, record["evidence_uid"],
            record["input_digest"],
        ))
        record["gate"] = gate
        record["state"] = STATE_PREPARED
        record["phase"] = "verified"
        record["pending_action"] = None
        record["last_error"] = None
        _save_record(root, record)
        return record
    except WorkflowError as exc:
        code = str(exc)
        if code.startswith(("LANDING_GATE_UNAVAILABLE", "LANDING_GATE_BLOCKED",
                            "LANDING_GATE_ERROR", "LANDING_VERIFICATION_UNAVAILABLE",
                            "LANDING_VERIFICATION_ERROR", "LANDING_TARGET_BUSY",
                            "LANDING_GIT_TIMEOUT", "LANDING_GIT_FAILED")):
            action = "gate"
            if code.startswith("LANDING_VERIFICATION"):
                action = "verification"
            elif code.startswith("LANDING_TARGET_BUSY"):
                action = "resource"
            _mark_pending(root, record, code, {
                "kind": action, "request_uid": record["uid"],
            })
        else:
            _mark_failed(root, record, code)
        raise


def prepare_landing(
    root,
    request,
    *,
    workspace=None,
    check_gate=None,
    verify_candidate=None,
    cancel_check=None,
):
    """Persist one landing intent and verify its real final Git candidate."""
    if not isinstance(request, LandingRequest):
        _fail("LANDING_REQUEST_REQUIRED")
    root = _require_root(root)
    _require_store(root)
    _require_authority(  # authorize before creating any record or directory
        IdentityStore(root), request.binding, request.request,
    )
    workspace_path = _canonical_path(
        workspace if workspace is not None else root
    )
    record = _record_from_request(root, request, workspace_path)
    uid = record["uid"]
    with _record_lock(root, uid):
        existing = _load_record(root, uid)
        if existing is not None:
            if existing["input_digest"] != record["input_digest"]:
                _fail("LANDING_REQUEST_INPUT_CHANGED", uid)
            if existing["state"] in {STATE_PREPARED, STATE_LANDED}:
                return existing
            if existing["state"] == STATE_CANCELLED:
                return existing
            if existing["state"] == STATE_FAILED:
                return existing
            record = existing
        else:
            _save_record(root, record)
        return _prepare_locked(
            root, record, check_gate, verify_candidate, cancel_check,
        )


def _land_context(root, record, phase, binding=None):
    actor, request = _operation_actor(root, record, binding)
    return _context(
        root, phase, record["uid"], request, actor.binding_uid,
        actor.controller_generation, actor.cancel_generation,
        record["target_ref"], record["expected_target_sha"],
        record["candidate_sha"], record["final_sha"],
        record["evidence_uid"], record["input_digest"],
    )


def _checked_out_worktree(workspace, target_ref):
    matches = [
        entry for entry in _worktrees(workspace)
        if entry.get("branch") == target_ref
    ]
    if len(matches) > 1:
        _fail("LANDING_TARGET_CHECKED_OUT_TWICE", target_ref)
    return matches[0] if matches else None


def _clean_checked_out(entry, expected_sha):
    path = Path(entry["path"])
    branch = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch.returncode or branch.stdout.strip() != _target_branch(
        entry["branch"]
    ):
        _fail("LANDING_WORKTREE_CHANGED", str(path))
    head = _git(path, "rev-parse", "--verify", "HEAD")
    if head.returncode or head.stdout.strip() != expected_sha:
        _fail("LANDING_WORKTREE_CHANGED", str(path))
    cached = _git(path, "diff", "--cached", "--quiet")
    if cached.returncode:
        _fail("LANDING_WORKTREE_DIRTY", str(path) + " index")
    unstaged = _git(path, "diff", "--quiet")
    if unstaged.returncode:
        _fail("LANDING_WORKTREE_DIRTY", str(path) + " worktree")
    status = _git(path, "status", "--porcelain", "-uall")
    if status.returncode or status.stdout.strip():
        _fail("LANDING_WORKTREE_DIRTY", str(path))
    return path


def _publish(workspace, record):
    target_ref = record["target_ref"]
    expected_sha = record["expected_target_sha"]
    final_sha = record["final_sha"]
    actual = _git(workspace, "rev-parse", "--verify", "--quiet", target_ref)
    actual_sha = actual.stdout.strip() if actual.returncode == 0 else None
    if actual_sha == final_sha:
        return "already-published", None
    if actual_sha != expected_sha:
        _fail("LANDING_TARGET_ADVANCED", str(actual_sha))
    if not _is_ancestor(workspace, expected_sha, final_sha):
        _fail("LANDING_CANDIDATE_INVALID", final_sha)
    entry = _checked_out_worktree(workspace, target_ref)
    if entry is None:
        updated = _git(
            workspace,
            "update-ref",
            "--no-deref",
            "-m",
            "gsp-landing " + record["uid"],
            target_ref,
            final_sha,
            expected_sha,
        )
        if updated.returncode:
            detail = (updated.stderr or updated.stdout or "").strip()
            _fail("LANDING_TARGET_CHANGED", detail[:500])
        verify = _git(workspace, "rev-parse", "--verify", target_ref)
        if verify.returncode or verify.stdout.strip() != final_sha:
            _fail("LANDING_PUBLISH_UNCONFIRMED", target_ref)
        return "update-ref", None
    path = _clean_checked_out(entry, expected_sha)
    merged = _git(path, "merge", "--ff-only", "--no-edit", final_sha)
    head = _git(path, "rev-parse", "--verify", "HEAD")
    ref = _git(workspace, "rev-parse", "--verify", target_ref)
    if (
        head.returncode == 0
        and head.stdout.strip() == final_sha
        and ref.returncode == 0
        and ref.stdout.strip() == final_sha
    ):
        return "fast-forward", str(path)
    detail = (merged.stderr or merged.stdout or "").strip()
    _fail("LANDING_FAST_FORWARD_FAILED", detail[:500])


def _land_locked(root, record, check_gate, cancel_check, binding=None):
    actor, _request = _operation_actor(root, record, binding)
    if record["state"] == STATE_LANDED:
        return record
    if record["state"] == STATE_CANCELLED:
        return record
    if record["state"] != STATE_PREPARED:
        _fail("LANDING_NOT_PREPARED", record["uid"] + " " + record["state"])
    record["attempts"] = int(record.get("attempts") or 0) + 1
    try:
        context = _land_context(root, record, "land", actor)
        if _cancelled(cancel_check, context):
            return _mark_cancelled(root, record, "cancelled-before-land")
        gate = _gate(check_gate, context)
        record["gate"] = gate
        workspace = Path(record["workspace"])
        root_identity = _repo_identity(root)
        workspace_identity = _repo_identity(workspace)
        if not _same_repository(root_identity, workspace_identity):
            _fail("LANDING_WORKSPACE_MISMATCH", record["workspace"])
        method, checkout_path = _publish(workspace, record)
        record["actual_sha"] = record["final_sha"]
        return _mark_landed(root, record, method, checkout_path)
    except WorkflowError as exc:
        code = str(exc)
        if code.startswith((
            "LANDING_GATE_UNAVAILABLE", "LANDING_GATE_BLOCKED",
            "LANDING_GATE_ERROR", "LANDING_WORKTREE_DIRTY",
            "LANDING_WORKTREE_CHANGED", "LANDING_TARGET_CHANGED",
            "LANDING_PUBLISH_UNCONFIRMED", "LANDING_GIT_TIMEOUT",
            "LANDING_GIT_FAILED",
        )):
            _mark_pending(root, record, code, {
                "kind": "gate" if code.startswith("LANDING_GATE") else "git",
                "request_uid": record["uid"],
            })
        else:
            _mark_failed(root, record, code)
        raise


def _prepared_uid(prepared_or_uid):
    if isinstance(prepared_or_uid, str):
        return _text(prepared_or_uid, "LANDING_RECORD_REQUIRED")
    if isinstance(prepared_or_uid, Mapping):
        value = prepared_or_uid.get("uid") or prepared_or_uid.get(
            "request_uid"
        )
        return _text(value, "LANDING_RECORD_REQUIRED")
    value = getattr(prepared_or_uid, "uid", None)
    return _text(value, "LANDING_RECORD_REQUIRED")


def land(root, prepared_or_uid, *, check_gate=None, cancel_check=None, binding=None):
    """Publish a verified prepared landing with a commit-point recheck."""
    root = _require_root(root)
    uid = _prepared_uid(prepared_or_uid)
    with _record_lock(root, uid):
        record = _load_record(root, uid)
        if record is None:
            _fail("LANDING_RECORD_NOT_FOUND", uid)
        if isinstance(prepared_or_uid, Mapping):
            supplied = prepared_or_uid.get("input_digest")
            if supplied is not None and supplied != record["input_digest"]:
                _fail("LANDING_REQUEST_INPUT_CHANGED", uid)
        return _land_locked(root, record, check_gate, cancel_check, binding)


def recover_landing(root, uid, *, check_gate=None, cancel_check=None, binding=None):
    """Resolve an interrupted landing from durable intent and Git truth."""
    root = _require_root(root)
    uid = _text(uid, "LANDING_RECORD_REQUIRED")
    with _record_lock(root, uid):
        record = _load_record(root, uid)
        if record is None:
            _fail("LANDING_RECORD_NOT_FOUND", uid)
        actor, _request = _operation_actor(root, record, binding)
        if record["state"] in TERMINAL_STATES:
            return record
        workspace = Path(record["workspace"])
        try:
            _require_store(root)
            if record.get("final_sha"):
                actual = _git(
                    workspace, "rev-parse", "--verify", "--quiet",
                    record["target_ref"],
                )
                actual_sha = (
                    actual.stdout.strip() if actual.returncode == 0 else None
                )
                record["actual_sha"] = actual_sha
                if actual_sha == record["final_sha"]:
                    return _mark_landed(
                        root, record, "recovered-existing-effect",
                    )
                if actual_sha not in {
                    None, record["expected_target_sha"],
                }:
                    _mark_failed(
                        root, record,
                        "LANDING_TARGET_ADVANCED " + str(actual_sha),
                    )
                    return record
                _prepare_target_recheck(workspace, record)
                # A retryable dirty/worktree/gate failure records ``pending``.
                # The durable candidate was already verified, so recovery may
                # re-enter the commit-point checks without rebuilding it.
                record["state"] = STATE_PREPARED
                return _land_locked(root, record, check_gate, cancel_check, actor)
            return _mark_pending(
                root, record, "LANDING_VERIFICATION_UNAVAILABLE",
                {"kind": "verification", "request_uid": record["uid"]},
            )
        except WorkflowError as exc:
            code = str(exc)
            if code.startswith((
                "LANDING_GATE_UNAVAILABLE", "LANDING_GATE_BLOCKED",
                "LANDING_GATE_ERROR",
            )):
                _mark_pending(root, record, code, {
                    "kind": "gate", "request_uid": record["uid"],
                })
            elif code.startswith("LANDING_NOT_PREPARED"):
                return record
            else:
                _mark_failed(root, record, code)
            raise


def record_delivery_push(root, uid, remote, *, binding, pushed=False, error=None):
    """Keep the optional push stage on the original durable landing record."""
    root = _require_root(root)
    with _record_lock(root, uid):
        record = _load_record(root, uid)
        if record is None:
            _fail('LANDING_RECORD_NOT_FOUND', uid)
        _operation_actor(root, record, binding)
        identity = {'remote': remote, 'target_ref': record['target_ref'],
                    'candidate_sha': record['final_sha']}
        old = record.get('delivery_push')
        if old and any(old.get(key) != value for key, value in identity.items()):
            _fail('LANDING_PUSH_INPUT_CHANGED')
        if old and old.get('state') == 'pushed':
            return old
        if pushed and record['state'] != STATE_LANDED:
            _fail('LANDING_NOT_LANDED')
        record['delivery_push'] = {**identity, 'state': 'pushed' if pushed else 'pending',
                                   'error': error}
        _save_record(root, record)
        return record['delivery_push']


def _prepare_target_recheck(workspace, record):
    """Fail closed when recovery sees a ref that moved off the intent."""
    _validate_target(
        workspace, record["target_ref"], record["expected_target_sha"],
    )


def load_landing(root, uid):
    """Read one landing record without creating directories or Git state."""
    root = _require_root(root)
    uid = _text(uid, "LANDING_RECORD_REQUIRED")
    record = _load_record(root, uid)
    if record is None:
        _fail("LANDING_RECORD_NOT_FOUND", uid)
    return record


__all__ = [
    "SCHEMA",
    "STATE_PREPARING",
    "STATE_PREPARED",
    "STATE_PENDING",
    "STATE_LANDED",
    "STATE_FAILED",
    "STATE_CANCELLED",
    "prepare_landing",
    "land",
    "recover_landing",
    "load_landing",
]
