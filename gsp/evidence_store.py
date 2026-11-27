"""Bounded, content-addressed evidence storage and Git reachability proofs."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import uuid

from .core import WorkflowError
from .identity_store import IdentityStore, IdentityStoreError
from .orchestration_types import (
    EvidenceManifest,
    RequestRef,
    SessionBinding,
    digest_value,
)


SCHEMA = 1
QUALITY_STATUS = "NOT_EVALUATED"
MAX_OBJECT_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_OBJECTS = 2048
MAX_SOURCE_ROOTS = 64
MAX_SOURCE_DEPTH = 64
MAX_COMMITS = 256
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
EVIDENCE_RELATIVE = Path(".gsp/evidence")
OBJECT_RELATIVE = EVIDENCE_RELATIVE / "objects" / "sha256"
MANIFEST_RELATIVE = EVIDENCE_RELATIVE / "manifests"
PROOF_RELATIVE = EVIDENCE_RELATIVE / "git-proofs"
STAGING_RELATIVE = EVIDENCE_RELATIVE / ".staging"
SKIP_SOURCE_DIRS = frozenset({".git", ".hg", ".svn"})
HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
COMMIT_SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
SAFE_UID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def _error(prefix, detail=""):
    message = prefix + ((" " + str(detail)) if detail else "")
    raise WorkflowError(message)


def _canonical(value):
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise WorkflowError("EVIDENCE_VALUE_INVALID") from exc


def _sha_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _safe_uid(value, name):
    if not isinstance(value, str) or SAFE_UID.fullmatch(value) is None:
        _error("EVIDENCE_UID_INVALID", name)
    return value


def _safe_digest(value, name):
    if not isinstance(value, str) or HEX_64.fullmatch(value.lower()) is None:
        _error("EVIDENCE_DIGEST_INVALID", name)
    return value.lower()


def _inside(root, path):
    try:
        Path(path).resolve(strict=False).relative_to(Path(root).resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _is_link(path):
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    if getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    ):
        return True
    checker = getattr(Path(path), "is_junction", None)
    return bool(checker and checker())


def _safe_path(root, raw, *, allow_missing=False):
    root = Path(root).resolve(strict=True)
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = Path(os.path.abspath(candidate))
    try:
        lexical = candidate.relative_to(root)
    except ValueError as exc:
        raise WorkflowError("EVIDENCE_SOURCE_ESCAPE " + str(candidate)) from exc
    current = root
    for part in lexical.parts:
        current = current / part
        if _is_link(current):
            _error("EVIDENCE_SOURCE_LINK_REFUSED", current)
        if not current.exists():
            if allow_missing and current == candidate:
                break
            _error("EVIDENCE_SOURCE_MISSING", current)
    resolved = candidate.resolve(strict=not allow_missing)
    if not _inside(root, resolved):
        _error("EVIDENCE_SOURCE_ESCAPE", resolved)
    return resolved


def _safe_label(value, name="logical path"):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        _error("EVIDENCE_SOURCE_LABEL_INVALID", name)
    normalized = value.replace("\\", "/").strip().strip("/")
    parts = normalized.split("/")
    if not normalized or any(part in {"", ".", ".."} for part in parts):
        _error("EVIDENCE_SOURCE_LABEL_INVALID", value)
    return "/".join(parts)


def _normalize_source_specs(root, sources):
    if isinstance(sources, (str, os.PathLike, dict)):
        values = [sources]
    else:
        try:
            values = list(sources)
        except TypeError as exc:
            raise WorkflowError("EVIDENCE_SOURCES_REQUIRED") from exc
    if not values:
        _error("EVIDENCE_SOURCES_REQUIRED")
    if len(values) > MAX_SOURCE_ROOTS:
        _error("EVIDENCE_SOURCE_BUDGET_EXCEEDED roots")
    specs = []
    for item in values:
        if isinstance(item, dict):
            if not {"path", "source"} & set(item):
                _error("EVIDENCE_SOURCE_SPEC_INVALID")
            path = item.get("path", item.get("source"))
            required = item.get("required", True)
            kind = item.get("kind", "auto")
            label = item.get("label", item.get("logical_path"))
        elif isinstance(item, (str, os.PathLike)):
            path, required, kind, label = item, True, "auto", None
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            path, label = item
            required, kind = True, "auto"
        else:
            _error("EVIDENCE_SOURCE_SPEC_INVALID")
        if not isinstance(path, (str, os.PathLike)):
            _error("EVIDENCE_SOURCE_SPEC_INVALID path")
        if not isinstance(required, bool):
            _error("EVIDENCE_SOURCE_SPEC_INVALID required")
        if kind not in {"auto", "file", "directory"}:
            _error("EVIDENCE_SOURCE_SPEC_INVALID kind")
        raw = Path(path)
        if not raw.is_absolute():
            raw = Path(root) / raw
        lexical = Path(os.path.abspath(raw))
        if not _inside(root, lexical):
            _error("EVIDENCE_SOURCE_ESCAPE", lexical)
        if label is not None:
            label = _safe_label(label)
        specs.append(
            {
                "path": lexical,
                "relative": lexical.relative_to(Path(root).resolve()).as_posix(),
                "required": required,
                "kind": kind,
                "label": label,
            }
        )
    return sorted(specs, key=lambda item: (item["relative"], item["label"] or ""))


def _source_spec_digest(specs):
    return digest_value(
        [
            {
                "relative": spec["relative"],
                "required": spec["required"],
                "kind": spec["kind"],
                "label": spec["label"],
            }
            for spec in specs
        ]
    )


def _source_excluded(relative):
    parts = relative.parts
    if any(part in SKIP_SOURCE_DIRS for part in parts):
        return True
    return len(parts) >= 2 and parts[:2] == (".gsp", "evidence")


def _walk_source(root, directory, logical_prefix, depth=0):
    if depth > MAX_SOURCE_DEPTH:
        _error("EVIDENCE_SOURCE_BUDGET_EXCEEDED depth", directory)
    found = []
    try:
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda entry: entry.name)
    except OSError as exc:
        raise WorkflowError("EVIDENCE_SOURCE_UNREADABLE " + str(directory)) from exc
    for entry in ordered:
        path = Path(entry.path)
        relative = path.relative_to(root)
        if _source_excluded(relative):
            continue
        if _is_link(path):
            _error("EVIDENCE_SOURCE_LINK_REFUSED", path)
        try:
            if entry.is_dir(follow_symlinks=False):
                found.extend(
                    _walk_source(
                        root,
                        path,
                        logical_prefix + "/" + entry.name,
                        depth + 1,
                    )
                )
            elif entry.is_file(follow_symlinks=False):
                found.append(
                    {
                        "source": path,
                        "logical_path": logical_prefix + "/" + entry.name,
                    }
                )
            else:
                _error("EVIDENCE_SOURCE_TYPE_REFUSED", path)
        except OSError as exc:
            raise WorkflowError("EVIDENCE_SOURCE_UNREADABLE " + str(path)) from exc
    return found


def _preflight_sources(root, specs):
    root = Path(root).resolve(strict=True)
    candidates = []
    roots = []
    for spec in specs:
        path = _safe_path(root, spec["path"], allow_missing=not spec["required"])
        relative = path.relative_to(root).as_posix()
        if not path.exists():
            roots.append(
                {
                    "path": relative,
                    "logical_path": spec["label"] or relative,
                    "kind": spec["kind"],
                    "required": spec["required"],
                    "status": "missing-optional",
                    "file_count": 0,
                }
            )
            continue
        if _is_link(path):
            _error("EVIDENCE_SOURCE_LINK_REFUSED", path)
        kind = spec["kind"] if spec["kind"] != "auto" else (
            "directory" if path.is_dir() else "file"
        )
        if kind == "file":
            if not path.is_file():
                _error("EVIDENCE_SOURCE_TYPE_REFUSED", path)
            logical = spec["label"] or relative
            rows = [{"source": path, "logical_path": logical}]
        elif kind == "directory":
            if not path.is_dir():
                _error("EVIDENCE_SOURCE_TYPE_REFUSED", path)
            logical = spec["label"] or relative
            rows = _walk_source(root, path, logical)
        else:
            _error("EVIDENCE_SOURCE_TYPE_REFUSED", path)
        roots.append(
            {
                "path": relative,
                "logical_path": spec["label"] or relative,
                "kind": kind,
                "required": spec["required"],
                "status": "collected",
                "file_count": len(rows),
            }
        )
        for row in rows:
            source = row["source"]
            logical_path = _safe_label(row["logical_path"])
            if _is_link(source) or not source.is_file():
                _error("EVIDENCE_SOURCE_TYPE_REFUSED", source)
            try:
                before = source.lstat()
                size = before.st_size
            except OSError as exc:
                raise WorkflowError("EVIDENCE_SOURCE_UNREADABLE " + str(source)) from exc
            if size > MAX_OBJECT_BYTES:
                _error("EVIDENCE_OBJECT_TOO_LARGE", source)
            if not os.access(source, os.R_OK):
                _error("EVIDENCE_SOURCE_UNREADABLE", source)
            candidates.append(
                {
                    "source": source,
                    "logical_path": logical_path,
                    "required": spec["required"],
                    "kind": "file",
                    "stat": before,
                }
            )
            if len(candidates) > MAX_OBJECTS:
                _error("EVIDENCE_SOURCE_BUDGET_EXCEEDED objects")
    if not candidates:
        _error("EVIDENCE_ORIGINALS_REQUIRED")
    if sum(item["stat"].st_size for item in candidates) > MAX_TOTAL_BYTES:
        _error("EVIDENCE_SOURCE_BUDGET_EXCEEDED bytes")
    logical = [item["logical_path"] for item in candidates]
    if len(logical) != len(set(logical)):
        _error("EVIDENCE_SOURCE_LOGICAL_PATH_DUPLICATE")
    return sorted(candidates, key=lambda item: item["logical_path"]), roots


def _store_path(root, relative, *, create=False):
    root = Path(root).resolve(strict=True)
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        _error("EVIDENCE_STORE_PATH_ESCAPE", relative)
    path = root / relative
    if not _inside(root, path):
        _error("EVIDENCE_STORE_PATH_ESCAPE", path)
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists() and _is_link(current):
            _error("EVIDENCE_STORE_PATH_ESCAPE", current)
        if create and not current.exists():
            current.mkdir()
    if path.exists() and _is_link(path):
        _error("EVIDENCE_STORE_PATH_ESCAPE", path)
    return path


def _object_relative(digest):
    return (OBJECT_RELATIVE / digest[:2] / digest).as_posix()


def _manifest_path(root, work_uid, manifest_uid):
    return _store_path(
        root,
        MANIFEST_RELATIVE / _safe_uid(work_uid, "work_uid") / (manifest_uid + ".json"),
    )


def _proof_path(root, work_uid, manifest_uid):
    return _store_path(
        root,
        PROOF_RELATIVE / _safe_uid(work_uid, "work_uid") / (manifest_uid + ".json"),
    )


def _copy_candidate(root, candidate, staging):
    source = candidate["source"]
    before = candidate["stat"]
    if _is_link(source):
        _error("EVIDENCE_SOURCE_LINK_REFUSED", source)
    temporary = Path(staging) / uuid.uuid4().hex
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            opened = os.fstat(reader.fileno())
            if not stat.S_ISREG(opened.st_mode):
                _error("EVIDENCE_SOURCE_TYPE_REFUSED", source)
            while True:
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_OBJECT_BYTES:
                    _error("EVIDENCE_OBJECT_TOO_LARGE", source)
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        after = source.lstat()
        signature = (
            before.st_size,
            getattr(before, "st_mtime_ns", None),
            getattr(before, "st_ino", None),
        )
        current = (
            after.st_size,
            getattr(after, "st_mtime_ns", None),
            getattr(after, "st_ino", None),
        )
        if signature != current or size != before.st_size:
            _error("EVIDENCE_SOURCE_CHANGED", source)
        return {
            "temporary": temporary,
            "logical_path": candidate["logical_path"],
            "required": candidate["required"],
            "kind": "file",
            "sha256": digest.hexdigest(),
            "size": size,
        }
    except WorkflowError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise WorkflowError("EVIDENCE_SOURCE_UNREADABLE " + str(source)) from exc


def _publish_blob(root, row):
    target = _store_path(root, _object_relative(row["sha256"]), create=True)
    temporary = row["temporary"]
    if target.exists():
        temporary.unlink(missing_ok=True)
        if (
            _is_link(target)
            or not target.is_file()
            or target.stat().st_size != row["size"]
            or _sha_bytes(target.read_bytes()) != row["sha256"]
        ):
            _error("EVIDENCE_OBJECT_CONFLICT", target)
        return target
    try:
        os.link(temporary, target)
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        if (
            _is_link(target)
            or not target.is_file()
            or target.stat().st_size != row["size"]
            or _sha_bytes(target.read_bytes()) != row["sha256"]
        ):
            _error("EVIDENCE_OBJECT_CONFLICT", target)
        return target
    except OSError as exc:
        raise WorkflowError("EVIDENCE_OBJECT_PUBLISH_FAILED " + str(target)) from exc
    finally:
        temporary.unlink(missing_ok=True)
    if (
        target.stat().st_size != row["size"]
        or _sha_bytes(target.read_bytes()) != row["sha256"]
    ):
        _error("EVIDENCE_OBJECT_WRITE_INCOMPLETE", target)
    return target


def _publish_json_once(root, path, value):
    root = Path(root).resolve(strict=True)
    path = Path(path)
    path = _store_path(root, path.relative_to(root), create=True)
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        _error("EVIDENCE_MANIFEST_TOO_LARGE", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        except OSError as exc:
            raise WorkflowError("EVIDENCE_MANIFEST_PUBLISH_FAILED " + str(path)) from exc
    finally:
        temporary.unlink(missing_ok=True)
    if not path.is_file() or path.is_symlink():
        _error("EVIDENCE_MANIFEST_PUBLISH_FAILED", path)
    return path


def _current_binding(store, binding, work_uid):
    if not isinstance(binding, SessionBinding):
        _error("SESSION_BINDING_REQUIRED")
    try:
        project = store.project_uid()
        work = store.resolve("work", work_uid, None, write=True)
        session = store.resolve(
            "session", binding.session.uid, None, write=True
        )
        immutable = store.read_session_binding(binding.binding_uid)
        state = store.read_session_state(binding.binding_uid)
    except (IdentityStoreError, AttributeError, TypeError) as exc:
        raise WorkflowError(str(exc)) from exc
    if (
        binding.project_uid != project
        or binding.work_uid != work.uid
        or work.project_uid != project
        or session != binding.session
    ):
        _error("WORK_UID_MISMATCH")
    canonical = digest_value(
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
    current_state = {
        "controller_generation": binding.controller_generation,
        "cancel_generation": binding.cancel_generation,
        "cancelled": binding.cancelled,
    }
    if immutable != canonical:
        _error("SESSION_CONTEXT_TAMPERED")
    if state != current_state:
        _error("SESSION_BINDING_STALE")
    if state["cancelled"]:
        _error("SESSION_BINDING_STALE")
    if not {"report", "write"} & set(binding.capabilities):
        _error("CAPABILITY_REQUIRED report")
    return project


def _request_values(
    root,
    work_uid,
    contract_digest,
    input_digest,
    binding,
    request,
):
    if (binding is None) != (request is None):
        _error("EVIDENCE_BINDING_REQUEST_REQUIRED")
    contract_digest = _safe_digest(contract_digest, "contract_digest")
    input_digest = _safe_digest(input_digest, "input_digest")
    if binding is None:
        return None, None
    if not isinstance(request, RequestRef):
        _error("REQUEST_REQUIRED")
    store = IdentityStore(root)
    project_uid = _current_binding(store, binding, work_uid)
    if (
        request.project_uid != project_uid
        or request.work_uid != work_uid
        or request.input_digest != input_digest
        or request.controller_generation != binding.controller_generation
        or request.cancel_generation != binding.cancel_generation
    ):
        _error("REQUEST_SCOPE_MISMATCH")
    if (
        request.contract_digest is not None
        and request.contract_digest != contract_digest
    ):
        _error("EVIDENCE_CONTRACT_CHANGED")
    owner = {
        "project_uid": project_uid,
        "work_uid": work_uid,
        "binding_uid": binding.binding_uid,
        "session_uid": binding.session.uid,
        "controller_generation": binding.controller_generation,
        "cancel_generation": binding.cancel_generation,
        "common_dir": binding.common_dir,
    }
    from .session_context import canonical_binding_digest
    owner['binding_digest']=canonical_binding_digest(binding)
    return owner, request.to_dict()


def _manifest_uid(project_uid, work_uid, task_uid, spec_digest, contract_digest,
                  input_digest, request):
    identity = {
        "schema": SCHEMA,
        "project_uid": project_uid,
        "work_uid": work_uid,
        "task_uid": task_uid,
    }
    if request is not None:
        identity["request_logical_key"] = request["logical_key"]
    else:
        identity.update(
            {
                "source_spec_digest": spec_digest,
                "contract_digest": contract_digest,
                "input_digest": input_digest,
            }
        )
    return digest_value(identity)


def _source_digest(objects):
    return digest_value(
        [
            {
                "logical_path": item["logical_path"],
                "sha256": item["sha256"],
                "size": item["size"],
            }
            for item in objects
        ]
    )


def _envelope_body(
    manifest,
    source_roots,
    source_spec_digest,
    source_digest,
    manifest_digest,
    owner,
    request,
    git_proof_digest,
):
    return {
        "schema": SCHEMA,
        "manifest": manifest.to_dict(),
        "source_roots": list(source_roots),
        "source_spec_digest": source_spec_digest,
        "source_digest": source_digest,
        "manifest_digest": manifest_digest,
        "owner": dict(owner) if owner is not None else None,
        "request": dict(request) if request is not None else None,
        "git_proof_digest": git_proof_digest,
        "quality_status": QUALITY_STATUS,
    }


def _envelope(
    manifest,
    source_roots,
    source_spec_digest,
    source_digest,
    owner,
    request,
    git_proof_digest=None,
):
    manifest_digest = digest_value(manifest.to_dict())
    body = _envelope_body(
        manifest,
        source_roots,
        source_spec_digest,
        source_digest,
        manifest_digest,
        owner,
        request,
        git_proof_digest,
    )
    return {**body, "integrity_digest": digest_value(body)}


def _request_from_envelope(value):
    if not isinstance(value, dict):
        _error("EVIDENCE_REQUEST_INVALID")
    expected = {
        "request_uid",
        "logical_key",
        "input_digest",
        "project_uid",
        "work_uid",
        "attempt_uid",
        "operation_kind",
        "controller_generation",
        "cancel_generation",
        "contract_uid",
        "contract_version",
        "contract_digest",
    }
    if set(value) != expected:
        _error("EVIDENCE_REQUEST_INVALID")
    return RequestRef.from_dict(value)


def _manifest_from_envelope(value):
    raw = value.get("manifest")
    if not isinstance(raw, dict):
        _error("EVIDENCE_MANIFEST_INVALID")
    expected = {
        "uid",
        "work_uid",
        "task_uid",
        "contract_digest",
        "input_digest",
        "objects",
        "git_refs",
    }
    if (
        set(raw) != expected
        or not isinstance(raw.get("objects"), list)
        or not isinstance(raw.get("git_refs"), list)
        or any(not isinstance(item, dict) for item in raw["objects"])
        or any(not isinstance(item, str) for item in raw["git_refs"])
    ):
        _error("EVIDENCE_MANIFEST_INVALID")
    _safe_digest(raw.get("uid"), "manifest_uid")
    _safe_uid(raw.get("work_uid"), "work_uid")
    _safe_uid(raw.get("task_uid"), "task_uid")
    _safe_digest(raw.get("contract_digest"), "contract_digest")
    _safe_digest(raw.get("input_digest"), "input_digest")
    manifest = EvidenceManifest(
        uid=raw["uid"],
        work_uid=raw["work_uid"],
        task_uid=raw["task_uid"],
        contract_digest=raw["contract_digest"],
        input_digest=raw["input_digest"],
        objects=tuple(dict(item) for item in raw["objects"]),
        git_refs=tuple(raw["git_refs"]),
    )
    return manifest


def _validate_envelope_shape(value):
    if not isinstance(value, dict):
        _error("EVIDENCE_MANIFEST_INVALID")
    expected = {
        "schema",
        "manifest",
        "source_roots",
        "source_spec_digest",
        "source_digest",
        "manifest_digest",
        "owner",
        "request",
        "git_proof_digest",
        "quality_status",
        "integrity_digest",
    }
    if set(value) != expected or value.get("schema") != SCHEMA:
        _error("EVIDENCE_MANIFEST_INVALID")
    body = {key: item for key, item in value.items() if key != "integrity_digest"}
    if value.get("integrity_digest") != digest_value(body):
        _error("EVIDENCE_MANIFEST_TAMPERED")
    manifest = _manifest_from_envelope(value)
    if value.get("manifest_digest") != digest_value(manifest.to_dict()):
        _error("EVIDENCE_MANIFEST_TAMPERED")
    if value.get("quality_status") != QUALITY_STATUS:
        _error("EVIDENCE_QUALITY_CLAIM_UNSUPPORTED")
    if not isinstance(value.get("source_roots"), list):
        _error("EVIDENCE_MANIFEST_INVALID")
    for row in value["source_roots"]:
        if (
            not isinstance(row, dict)
            or set(row) != {
                "path",
                "logical_path",
                "kind",
                "required",
                "status",
                "file_count",
            }
            or row.get("kind") not in {"file", "directory"}
            or type(row.get("required")) is not bool
            or row.get("status") not in {"collected", "missing-optional"}
            or type(row.get("file_count")) is not int
            or not 0 <= row["file_count"] <= MAX_OBJECTS
            or not isinstance(row.get("path"), str)
            or Path(row["path"]).is_absolute()
            or ".." in Path(row["path"]).parts
            or (
                row["status"] == "missing-optional"
                and row["required"]
            )
        ):
            _error("EVIDENCE_SOURCE_ROOT_INVALID")
        _safe_label(row.get("logical_path"))
    _safe_digest(value.get("source_spec_digest"), "source_spec_digest")
    _safe_digest(value.get("source_digest"), "source_digest")
    owner = value.get("owner")
    request = value.get("request")
    if owner is None and request is not None or owner is not None and request is None:
        _error("EVIDENCE_BINDING_REQUEST_REQUIRED")
    if owner is not None:
        owner_fields={
            "project_uid",
            "work_uid",
            "binding_uid",
            "session_uid",
            "controller_generation",
            "cancel_generation",
            "common_dir",
        }
        if not isinstance(owner,dict) or set(owner) not in (owner_fields,owner_fields|{'binding_digest'}):
            _error("EVIDENCE_OWNER_INVALID")
        if 'binding_digest' in owner:
            _safe_digest(owner['binding_digest'],'binding_digest')
        if (
            not isinstance(owner["project_uid"], str)
            or not owner["project_uid"].strip()
        ):
            _error("EVIDENCE_OWNER_INVALID")
        _safe_uid(owner["work_uid"], "work_uid")
        _safe_uid(owner["binding_uid"], "binding_uid")
        _safe_uid(owner["session_uid"], "session_uid")
        if (
            type(owner["controller_generation"]) is not int
            or owner["controller_generation"] < 1
            or type(owner["cancel_generation"]) is not int
            or owner["cancel_generation"] < 1
            or not isinstance(owner["common_dir"], str)
            or not owner["common_dir"]
        ):
            _error("EVIDENCE_OWNER_INVALID")
        _request_from_envelope(request)
    for row in _manifest_from_envelope(value).objects:
        if (
            set(row) != {
                "logical_path",
                "kind",
                "sha256",
                "size",
                "object_path",
                "required",
            }
            or row.get("kind") != "file"
            or type(row.get("required")) is not bool
            or type(row.get("size")) is not int
            or not 0 <= row["size"] <= MAX_OBJECT_BYTES
        ):
            _error("EVIDENCE_OBJECT_INVALID")
        _safe_label(row.get("logical_path"))
        _safe_digest(row.get("sha256"), "object_sha256")
        if row.get("object_path") != _object_relative(row["sha256"]):
            _error("EVIDENCE_OBJECT_INVALID")
    return value


def _read_json_file(path, error_code):
    path = Path(path)
    if _is_link(path) or not path.is_file():
        _error(error_code, path)
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            _error(error_code, path)
        return json.loads(path.read_text(encoding="utf-8"))
    except WorkflowError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise WorkflowError(error_code + " " + str(path)) from exc


def _normalize_manifest(value):
    if isinstance(value, dict):
        try:
            normalized = json.loads(_canonical(value))
        except (TypeError, ValueError) as exc:
            raise WorkflowError("EVIDENCE_MANIFEST_INVALID") from exc
        return _validate_envelope_shape(normalized)
    _error("EVIDENCE_MANIFEST_INVALID")


def _theoretical_uid(
    project_uid,
    work_uid,
    task_uid,
    spec_digest,
    contract_digest,
    input_digest,
    request,
):
    return _manifest_uid(
        project_uid,
        work_uid,
        task_uid,
        spec_digest,
        contract_digest,
        input_digest,
        request,
    )


def collect_evidence(
    root,
    work_uid,
    task_uid,
    sources,
    contract_digest,
    input_digest,
    *,
    binding=None,
    request=None,
):
    """Collect bounded originals and publish one immutable manifest envelope."""
    root = Path(root).resolve(strict=True)
    work_uid = _safe_uid(work_uid, "work_uid")
    task_uid = _safe_uid(task_uid, "task_uid")
    contract_digest = _safe_digest(contract_digest, "contract_digest")
    input_digest = _safe_digest(input_digest, "input_digest")
    specs = _normalize_source_specs(root, sources)
    spec_digest = _source_spec_digest(specs)
    owner, request_value = _request_values(
        root,
        work_uid,
        contract_digest,
        input_digest,
        binding,
        request,
    )
    project_uid = owner["project_uid"] if owner else "unbound"
    manifest_uid = _theoretical_uid(
        project_uid,
        work_uid,
        task_uid,
        spec_digest,
        contract_digest,
        input_digest,
        request_value,
    )
    manifest_path = _manifest_path(root, work_uid, manifest_uid)
    if manifest_path.is_file():
        existing = _normalize_manifest(
            _read_json_file(manifest_path, "EVIDENCE_MANIFEST_INVALID")
        )
        core = _manifest_from_envelope(existing)
        if (
            existing["manifest"]["uid"] != manifest_uid
            or existing["owner"] != owner
            or existing["request"] != request_value
            or existing["source_spec_digest"] != spec_digest
            or core.contract_digest != contract_digest
            or core.input_digest != input_digest
        ):
            _error("REQUEST_INPUT_CHANGED")
        return existing

    candidates, source_roots = _preflight_sources(root, specs)
    staging_root = root / STAGING_RELATIVE
    staging_root.mkdir(parents=True, exist_ok=True)
    if _is_link(staging_root) or not _inside(root, staging_root):
        _error("EVIDENCE_STORE_PATH_ESCAPE", staging_root)
    staging = Path(tempfile.mkdtemp(prefix="collect-", dir=staging_root))
    copied = []
    try:
        for candidate in candidates:
            copied.append(_copy_candidate(root, candidate, staging))
        objects = []
        for row in copied:
            target = _publish_blob(root, row)
            objects.append(
                {
                    "logical_path": row["logical_path"],
                    "kind": row["kind"],
                    "sha256": row["sha256"],
                    "size": row["size"],
                    "object_path": target.relative_to(root).as_posix(),
                    "required": row["required"],
                }
            )
        objects.sort(key=lambda item: item["logical_path"])
        source_digest = _source_digest(objects)
        manifest = EvidenceManifest(
            uid=manifest_uid,
            work_uid=work_uid,
            task_uid=task_uid,
            contract_digest=contract_digest,
            input_digest=input_digest,
            objects=tuple(objects),
            git_refs=(),
        )
        envelope = _normalize_manifest(
            _envelope(
                manifest,
                tuple(source_roots),
                spec_digest,
                source_digest,
                owner,
                request_value,
            )
        )
        if binding is not None:
            _current_binding(IdentityStore(root),binding,work_uid)
        _publish_json_once(root, manifest_path, envelope)
        published = _normalize_manifest(
            _read_json_file(manifest_path, "EVIDENCE_MANIFEST_INVALID")
        )
        if published != envelope:
            _error("EVIDENCE_MANIFEST_CONFLICT")
        return published
    finally:
        if staging.exists() and staging.resolve().is_relative_to(staging_root.resolve()):
            shutil.rmtree(staging, ignore_errors=True)


def _current_owner(store, owner, request):
    try:
        project = store.project_uid()
        work = store.resolve("work", owner["work_uid"], None, write=True)
        session = store.resolve("session", owner["session_uid"], None, write=True)
        state = store.read_session_state(owner["binding_uid"])
        immutable=store.read_session_binding(owner['binding_uid'])
    except (IdentityStoreError, KeyError, TypeError) as exc:
        raise WorkflowError(str(exc)) from exc
    if project != owner["project_uid"] or work.uid != owner["work_uid"]:
        _error("WORK_UID_MISMATCH")
    if session.uid != owner["session_uid"]:
        _error("SESSION_IDENTITY_MISMATCH")
    if state is None or immutable is None:
        _error("SESSION_BINDING_STALE")
    pinned=owner.get('binding_digest')
    if pinned is not None and pinned!=immutable:
        _error('SESSION_CONTEXT_TAMPERED')
    current=(not state['cancelled'] and
             state['controller_generation']==owner['controller_generation'] and
             state['cancel_generation']==owner['cancel_generation'])
    if (not current and (pinned is None
            or state['controller_generation']<owner['controller_generation']
            or state['cancel_generation']<owner['cancel_generation'])):
        _error("SESSION_BINDING_STALE")
    if (
        request.project_uid != project
        or request.work_uid != owner["work_uid"]
        or request.controller_generation != owner["controller_generation"]
        or request.cancel_generation != owner["cancel_generation"]
    ):
        _error("REQUEST_SCOPE_MISMATCH")
    return 'current' if current else 'historical'


def _verify_object(root, row, total):
    digest = row.get("sha256")
    size = row.get("size")
    if (
        not isinstance(digest, str)
        or HEX_64.fullmatch(digest) is None
        or type(size) is not int
        or size < 0
        or size > MAX_OBJECT_BYTES
    ):
        return None, "EVIDENCE_OBJECT_INVALID"
    expected_path = _object_relative(digest)
    path = Path(root) / row.get("object_path", "")
    if (
        row.get("object_path") != expected_path
        or not _inside(root, path)
        or _is_link(path)
        or not path.is_file()
    ):
        return None, "EVIDENCE_OBJECT_MISSING"
    if path.stat().st_size != size:
        return None, "EVIDENCE_OBJECT_SIZE_CHANGED"
    actual = _sha_bytes(path.read_bytes())
    if actual != digest:
        return None, "EVIDENCE_OBJECT_TAMPERED"
    if total + size > MAX_TOTAL_BYTES:
        return None, "EVIDENCE_SOURCE_BUDGET_EXCEEDED bytes"
    return {
        "logical_path": row.get("logical_path"),
        "kind": row.get("kind"),
        "sha256": digest,
        "size": size,
        "stored_path": expected_path,
        "required": row.get("required", True),
    }, None


def _verify_loaded(root, envelope, *, require_authority=True):
    errors = []
    authority_status='unbound'
    manifest = _manifest_from_envelope(envelope)
    persisted_path = _manifest_path(root, manifest.work_uid, manifest.uid)
    try:
        persisted = _normalize_manifest(
            _read_json_file(persisted_path, "EVIDENCE_MANIFEST_NOT_PUBLISHED")
        )
    except WorkflowError as exc:
        return {"accepted": False, "errors": [str(exc)]}
    proof = None
    git_proof_digest = envelope.get("git_proof_digest")
    if git_proof_digest is None:
        if persisted != envelope:
            return {"accepted": False, "errors": ["EVIDENCE_MANIFEST_CONFLICT"]}
        if manifest.git_refs:
            return {"accepted": False, "errors": ["EVIDENCE_GIT_PROOF_REQUIRED"]}
    else:
        _safe_digest(git_proof_digest, "git_proof_digest")
        proof_path = _proof_path(root, manifest.work_uid, manifest.uid)
        proof = _validate_proof(
            _read_json_file(proof_path, "EVIDENCE_GIT_PROOF_MISSING"),
            envelope,
        )
        if proof.get("proof_digest") != git_proof_digest:
            errors.append("EVIDENCE_GIT_PROOF_CHANGED")
        base = dict(envelope)
        base["manifest"] = dict(base["manifest"])
        base["git_proof_digest"] = None
        base["manifest"]["git_refs"] = []
        base["manifest_digest"] = digest_value(base["manifest"])
        base_body = {key: value for key, value in base.items() if key != "integrity_digest"}
        base["integrity_digest"] = digest_value(base_body)
        if persisted != base:
            errors.append("EVIDENCE_MANIFEST_CONFLICT")
        expected_refs = [row["ref"] for row in proof["commits"]]
        if list(manifest.git_refs) != expected_refs:
            errors.append("EVIDENCE_GIT_REF_CHANGED")
    owner = envelope.get("owner")
    request_value = envelope.get("request")
    if owner is None:
        if require_authority:
            errors.append("EVIDENCE_AUTHORITY_REQUIRED")
    else:
        request_ref = _request_from_envelope(request_value)
        if (
            owner["work_uid"] != manifest.work_uid
            or owner["project_uid"] != request_ref.project_uid
            or request_ref.work_uid != manifest.work_uid
            or request_ref.input_digest != manifest.input_digest
            or (
                request_ref.contract_digest is not None
                and request_ref.contract_digest != manifest.contract_digest
            )
        ):
            errors.append("REQUEST_SCOPE_MISMATCH")
        try:
            authority_status=_current_owner(IdentityStore(root), owner, request_ref)
        except WorkflowError as exc:
            errors.append(str(exc))
    if manifest.uid != _manifest_uid(
        owner["project_uid"] if owner else "unbound",
        manifest.work_uid,
        manifest.task_uid,
        envelope["source_spec_digest"],
        manifest.contract_digest,
        manifest.input_digest,
        request_value,
    ):
        errors.append("EVIDENCE_MANIFEST_UID_INVALID")
    total = 0
    verified_objects = []
    logical = []
    required_originals = 0
    for row in manifest.objects:
        if not isinstance(row, dict):
            errors.append("EVIDENCE_OBJECT_INVALID")
            continue
        logical.append(row.get("logical_path"))
        required_originals += int(row.get("required") is True)
        result, problem = _verify_object(root, row, total)
        if problem:
            errors.append(problem)
            continue
        total += result["size"]
        verified_objects.append(result)
    if not verified_objects or required_originals == 0:
        errors.append("EVIDENCE_ORIGINALS_REQUIRED")
    if len(logical) != len(set(logical)):
        errors.append("EVIDENCE_SOURCE_LOGICAL_PATH_DUPLICATE")
    if _source_digest(list(manifest.objects)) != envelope["source_digest"]:
        errors.append("EVIDENCE_SOURCE_DIGEST_CHANGED")
    return {
        "schema": SCHEMA,
        "accepted": not errors,
        "errors": errors,
        "manifest_uid": manifest.uid,
        "manifest_path": persisted_path.relative_to(root).as_posix(),
        "manifest_digest": digest_value(manifest.to_dict()),
        "source_digest": envelope["source_digest"],
        "objects": verified_objects,
        "git_refs": list(manifest.git_refs),
        "git_proof_digest": git_proof_digest,
        "quality_status": QUALITY_STATUS,
        'authority_status':authority_status,
    }


def load_manifest(root,work_uid,manifest_uid):
    """Read one canonical manifest and optional Git proof without projections."""
    root=Path(root).resolve(strict=True)
    _safe_uid(work_uid,'work_uid')
    _safe_uid(manifest_uid,'manifest_uid')
    envelope=_normalize_manifest(_read_json_file(_manifest_path(root,work_uid,manifest_uid),
                                                'EVIDENCE_MANIFEST_NOT_PUBLISHED'))
    proof_path=_proof_path(root,work_uid,manifest_uid)
    if proof_path.is_file():
        envelope=_augment_with_proof(envelope,_read_json_file(proof_path,'EVIDENCE_GIT_PROOF_MISSING'))
    return envelope


def verify_manifest(root, manifest):
    """Verify one published raw-evidence manifest without asserting quality."""
    try:
        root = Path(root).resolve(strict=True)
        envelope = _normalize_manifest(manifest)
        return _verify_loaded(root, envelope)
    except (WorkflowError, OSError, TypeError, ValueError, KeyError) as exc:
        return {
            "schema": SCHEMA,
            "accepted": False,
            "errors": [str(exc)],
            "quality_status": QUALITY_STATUS,
        }


def _git(root, *args, input_text=None):
    command = ["git", "-C", str(root), *args]
    try:
        result = subprocess.run(
            command,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            ),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkflowError("EVIDENCE_GIT_UNAVAILABLE") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        _error("EVIDENCE_GIT_FAILED", detail)
    return (result.stdout or "").strip()


def _common_dir(root):
    raw = _git(root, "rev-parse", "--git-common-dir")
    if not raw:
        _error("EVIDENCE_GIT_COMMON_DIR_REQUIRED")
    path = Path(raw)
    if not path.is_absolute():
        path = Path(root) / path
    path = path.resolve(strict=True)
    if not path.is_dir():
        _error("EVIDENCE_GIT_COMMON_DIR_REQUIRED")
    return path


def _normalize_commits(commits):
    if isinstance(commits, (str, bytes)):
        values = [commits]
    else:
        try:
            values = list(commits)
        except TypeError as exc:
            raise WorkflowError("EVIDENCE_GIT_COMMITS_REQUIRED") from exc
    if not values or len(values) > MAX_COMMITS:
        _error("EVIDENCE_GIT_COMMITS_REQUIRED")
    result = []
    for value in values:
        if not isinstance(value, str) or COMMIT_SHA.fullmatch(value) is None:
            _error("EVIDENCE_GIT_COMMIT_INVALID", value)
        result.append(value.lower())
    if len(result) != len(set(result)):
        _error("EVIDENCE_GIT_COMMIT_DUPLICATE")
    return sorted(result)


def _resolve_commit(root, commit):
    resolved = _git(
        root, "rev-parse", "--verify", "--end-of-options", commit + "^{commit}"
    ).lower()
    if not COMMIT_SHA.fullmatch(resolved):
        _error("EVIDENCE_GIT_COMMIT_INVALID", commit)
    return resolved


def _ref_name(work_uid, manifest_uid, commit):
    return "refs/gsp/evidence/{}/{}/{}".format(
        _safe_uid(work_uid, "work_uid"),
        _safe_digest(manifest_uid, "manifest_uid"),
        commit,
    )


def _read_ref(root, ref):
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", ref],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkflowError("EVIDENCE_GIT_UNAVAILABLE") from exc
    return result.stdout.strip().lower() if result.returncode == 0 else None


def _proof_commits(root, work_uid, manifest_uid, commits):
    rows = []
    for commit in commits:
        resolved = _resolve_commit(root, commit)
        rows.append(
            {
                "commit": resolved,
                "ref": _ref_name(work_uid, manifest_uid, resolved),
            }
        )
    return rows


def _validate_binding_owner(envelope, binding, request):
    owner = envelope.get("owner")
    stored_request = envelope.get("request")
    if owner is None or stored_request is None:
        _error("EVIDENCE_AUTHORITY_REQUIRED")
    if (
        owner["binding_uid"] != binding.binding_uid
        or owner["session_uid"] != binding.session.uid
        or owner["work_uid"] != binding.work_uid
        or owner["controller_generation"] != binding.controller_generation
        or owner["cancel_generation"] != binding.cancel_generation
        or stored_request != request.to_dict()
    ):
        _error("REQUEST_SCOPE_MISMATCH")


def _existing_proof_matches(path, rows, envelope):
    if not path.exists():
        return None
    proof = _read_json_file(path, "EVIDENCE_GIT_PROOF_INVALID")
    proof = _validate_proof(proof, envelope)
    if (
        proof.get("manifest_uid") != envelope["manifest"]["uid"]
        or proof.get("work_uid") != envelope["manifest"]["work_uid"]
        or proof.get("commits") != rows
    ):
        _error("REQUEST_INPUT_CHANGED")
    return proof


def _validate_proof(proof, envelope):
    manifest = envelope["manifest"]
    owner = envelope.get("owner") or {}
    request = envelope.get("request") or {}
    expected = {
        "schema",
        "manifest_uid",
        "work_uid",
        "task_uid",
        "binding_uid",
        "request_uid",
        "common_dir",
        "commits",
        "proof_digest",
    }
    if not isinstance(proof, dict) or set(proof) != expected:
        _error("EVIDENCE_GIT_PROOF_INVALID")
    body = {key: value for key, value in proof.items() if key != "proof_digest"}
    if proof.get("proof_digest") != digest_value(body):
        _error("EVIDENCE_GIT_PROOF_TAMPERED")
    if (
        proof.get("schema") != SCHEMA
        or proof.get("manifest_uid") != manifest.get("uid")
        or proof.get("work_uid") != manifest.get("work_uid")
        or proof.get("task_uid") != manifest.get("task_uid")
        or proof.get("binding_uid") != owner.get("binding_uid")
        or proof.get("request_uid") != request.get("request_uid")
        or not isinstance(proof.get("common_dir"), str)
        or not proof["common_dir"]
        or not isinstance(proof.get("commits"), list)
        or not 1 <= len(proof["commits"]) <= MAX_COMMITS
    ):
        _error("EVIDENCE_GIT_PROOF_INVALID")
    commits = []
    for row in proof["commits"]:
        if not isinstance(row, dict) or set(row) != {"commit", "ref"}:
            _error("EVIDENCE_GIT_PROOF_INVALID")
        commit = row.get("commit")
        if not isinstance(commit, str) or COMMIT_SHA.fullmatch(commit) is None:
            _error("EVIDENCE_GIT_PROOF_INVALID")
        expected_ref = _ref_name(
            manifest["work_uid"], manifest["uid"], commit.lower()
        )
        if row.get("ref") != expected_ref:
            _error("EVIDENCE_GIT_PROOF_INVALID")
        commits.append(commit.lower())
    if len(commits) != len(set(commits)):
        _error("EVIDENCE_GIT_PROOF_INVALID")
    return proof


def _augment_with_proof(envelope, proof):
    refs = [row["ref"] for row in proof["commits"]]
    body = dict(envelope)
    body["manifest"] = dict(body["manifest"])
    body["manifest"]["git_refs"] = refs
    body["manifest_digest"] = digest_value(body["manifest"])
    body["git_proof_digest"] = proof["proof_digest"]
    raw = {key: value for key, value in body.items() if key != "integrity_digest"}
    body["integrity_digest"] = digest_value(raw)
    return _normalize_manifest(body)


def persist_git_objects(root, binding, manifest, commits, request):
    """Retain commits under GSP-owned refs and publish a separate Git proof."""
    root = Path(root).resolve(strict=True)
    envelope = _normalize_manifest(manifest)
    if not isinstance(binding, SessionBinding) or not isinstance(request, RequestRef):
        _error("EVIDENCE_BINDING_REQUEST_REQUIRED")
    owner = envelope.get("owner")
    if owner is None:
        _error("EVIDENCE_AUTHORITY_REQUIRED")
    core = _manifest_from_envelope(envelope)
    store = IdentityStore(root)
    _current_binding(store, binding, core.work_uid)
    _validate_binding_owner(envelope, binding, request)
    verification = verify_manifest(root, envelope)
    if not verification.get("accepted"):
        _error("EVIDENCE_MANIFEST_INVALID", verification.get("errors"))
    common = _common_dir(root)
    try:
        binding_common = Path(binding.common_dir).resolve(strict=True)
    except OSError as exc:
        raise WorkflowError("EVIDENCE_GIT_BINDING_MISMATCH") from exc
    if binding_common != common:
        _error("EVIDENCE_GIT_BINDING_MISMATCH")
    if _safe_digest(core.input_digest, "input_digest") != request.input_digest:
        _error("REQUEST_INPUT_CHANGED")
    rows = _proof_commits(root, core.work_uid, core.uid, _normalize_commits(commits))
    path = _proof_path(root, core.work_uid, core.uid)
    existing = _existing_proof_matches(path, rows, envelope)
    if existing is not None:
        return _augment_with_proof(envelope, existing)
    pending = []
    for row in rows:
        current = _read_ref(root, row["ref"])
        if current is not None and current != row["commit"]:
            _error("EVIDENCE_GIT_REF_CONFLICT", row["ref"])
        if current is None:
            pending.append(row)
    if pending:
        for row in pending:
            try:
                _git(root, "update-ref", row["ref"], row["commit"], "")
            except WorkflowError:
                if _read_ref(root, row["ref"]) != row["commit"]:
                    raise
    for row in rows:
        if _read_ref(root, row["ref"]) != row["commit"]:
            _error("EVIDENCE_GIT_REF_UNVERIFIED", row["ref"])
    proof = {
        "schema": SCHEMA,
        "manifest_uid": core.uid,
        "work_uid": core.work_uid,
        "task_uid": core.task_uid,
        "binding_uid": owner["binding_uid"],
        "request_uid": request.request_uid,
        "common_dir": os.path.normcase(str(common)),
        "commits": rows,
    }
    proof["proof_digest"] = digest_value(proof)
    _publish_json_once(root, path, proof)
    stored = _read_json_file(path, "EVIDENCE_GIT_PROOF_INVALID")
    if stored != proof:
        _error("EVIDENCE_GIT_PROOF_CONFLICT")
    return _augment_with_proof(envelope, proof)


def verify_git_reachability(root, manifest):
    """Verify GSP refs retain the declared commits; this is not a landing proof."""
    try:
        root = Path(root).resolve(strict=True)
        envelope = _normalize_manifest(manifest)
        raw = _verify_loaded(root, envelope)
        authority_status = raw.get("authority_status", "unbound")
        errors = list(raw.get("errors") or [])
        proof_digest = envelope.get("git_proof_digest")
        if proof_digest is None:
            errors.append("EVIDENCE_GIT_PROOF_REQUIRED")
            proof = None
        else:
            proof = _validate_proof(
                _read_json_file(
                    _proof_path(
                        root,
                        envelope["manifest"]["work_uid"],
                        envelope["manifest"]["uid"],
                    ),
                    "EVIDENCE_GIT_PROOF_MISSING",
                ),
                envelope,
            )
            if proof.get("proof_digest") != proof_digest:
                errors.append("EVIDENCE_GIT_PROOF_CHANGED")
        rows = []
        common = _common_dir(root)
        if proof is not None:
            if proof["common_dir"] != os.path.normcase(str(common)):
                errors.append("EVIDENCE_GIT_COMMON_DIR_CHANGED")
            for row in proof["commits"]:
                actual = _read_ref(root, row["ref"])
                if actual != row["commit"]:
                    errors.append("EVIDENCE_GIT_COMMIT_UNREACHABLE")
                rows.append(
                    {
                        "commit": row["commit"],
                        "ref": row["ref"],
                        "reachable": actual == row["commit"],
                    }
                )
        return {
            "schema": SCHEMA,
            "accepted": not errors,
            "errors": errors,
            "manifest_uid": envelope["manifest"]["uid"],
            "proof_digest": proof_digest,
            "commits": rows,
            "landing": "NOT_PROVEN",
            "quality_status": QUALITY_STATUS,
            "authority_status": authority_status,
        }
    except (WorkflowError, OSError, TypeError, ValueError, KeyError) as exc:
        return {
            "schema": SCHEMA,
            "accepted": False,
            "errors": [str(exc)],
            "commits": [],
            "landing": "NOT_PROVEN",
            "quality_status": QUALITY_STATUS,
            "authority_status": "unbound",
        }
