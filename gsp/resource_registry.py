"""Transactional project-wide exclusion for filesystem, Git and worker slots.

The registry shares the orchestration IdentityStore SQLite file.  Reads are
strictly read-only; schema creation is limited to successful write requests.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Iterable, Mapping

from .identity_store import IdentityStore, IdentityStoreError


RESOURCE_SCHEMA_VERSION = 1
EXECUTION_SLOT_COUNT = 3
EXECUTION_SLOT_PREFIX = "project-execution-slot:"
RESOURCE_KINDS = frozenset({"path", "branch", "build_root", "slot"})
RESOURCE_DISPOSITIONS = frozenset({"delete", "release", "archive"})
ACTIVE_STATES = frozenset({"reserved", "committed"})


class ResourceRegistryError(Exception):
    """Stable resource registry failure with a machine-readable prefix."""


class ResourceConflictError(ResourceRegistryError):
    """One or more requested resources are already held."""


@dataclass(frozen=True)
class ResourceOwner:
    work_uid: str
    session_uid: str
    generation: int


RESOURCE_SCHEMA_SQL = (
    """CREATE TABLE IF NOT EXISTS resource_requests (
        request_uid TEXT PRIMARY KEY,
        request_digest TEXT NOT NULL,
        project_uid TEXT NOT NULL,
        owner_work_uid TEXT NOT NULL,
        owner_session_uid TEXT NOT NULL,
        owner_generation INTEGER NOT NULL CHECK(owner_generation > 0),
        state TEXT NOT NULL CHECK(state IN ('reserved','committed','released')),
        resources_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS resource_claims (
        kind TEXT NOT NULL CHECK(kind IN ('path','branch','build_root')),
        identity TEXT NOT NULL,
        request_uid TEXT NOT NULL,
        project_uid TEXT NOT NULL,
        owner_work_uid TEXT NOT NULL,
        owner_session_uid TEXT NOT NULL,
        owner_generation INTEGER NOT NULL CHECK(owner_generation > 0),
        state TEXT NOT NULL CHECK(state IN ('reserved','committed')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(kind, identity),
        FOREIGN KEY(request_uid) REFERENCES resource_requests(request_uid)
    )""",
    """CREATE INDEX IF NOT EXISTS resource_claims_by_request
        ON resource_claims(request_uid)""",
    """CREATE TRIGGER IF NOT EXISTS resource_request_identity_immutable
        BEFORE UPDATE OF request_uid, request_digest, project_uid,
            owner_work_uid, owner_session_uid, owner_generation,
            resources_json, created_at ON resource_requests
        BEGIN SELECT RAISE(ABORT, 'RESOURCE_REQUEST_IDENTITY_IMMUTABLE'); END""",
    """CREATE TRIGGER IF NOT EXISTS resource_request_no_delete
        BEFORE DELETE ON resource_requests
        BEGIN SELECT RAISE(ABORT, 'RESOURCE_REQUEST_NO_DELETE'); END""",
    """CREATE TRIGGER IF NOT EXISTS resource_claim_identity_immutable
        BEFORE UPDATE OF kind, identity, request_uid, project_uid,
            owner_work_uid, owner_session_uid, owner_generation, created_at
            ON resource_claims
        BEGIN SELECT RAISE(ABORT, 'RESOURCE_CLAIM_IDENTITY_IMMUTABLE'); END""",
    """CREATE TABLE IF NOT EXISTS execution_slot_requests (
        request_uid TEXT PRIMARY KEY,
        request_digest TEXT NOT NULL,
        project_uid TEXT NOT NULL,
        owner_work_uid TEXT NOT NULL,
        owner_session_uid TEXT NOT NULL,
        owner_generation INTEGER NOT NULL CHECK(owner_generation > 0),
        slot_number INTEGER NOT NULL CHECK(slot_number BETWEEN 1 AND 3),
        state TEXT NOT NULL CHECK(state IN ('reserved','committed','released')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS execution_slot_claims (
        slot_number INTEGER PRIMARY KEY CHECK(slot_number BETWEEN 1 AND 3),
        request_uid TEXT NOT NULL UNIQUE,
        project_uid TEXT NOT NULL,
        owner_work_uid TEXT NOT NULL,
        owner_session_uid TEXT NOT NULL,
        owner_generation INTEGER NOT NULL CHECK(owner_generation > 0),
        state TEXT NOT NULL CHECK(state IN ('reserved','committed')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(request_uid) REFERENCES execution_slot_requests(request_uid)
    )""",
    """CREATE TRIGGER IF NOT EXISTS execution_slot_request_identity_immutable
        BEFORE UPDATE OF request_uid, request_digest, project_uid,
            owner_work_uid, owner_session_uid, owner_generation, slot_number,
            created_at ON execution_slot_requests
        BEGIN SELECT RAISE(ABORT, 'EXECUTION_SLOT_REQUEST_IDENTITY_IMMUTABLE'); END""",
    """CREATE TRIGGER IF NOT EXISTS execution_slot_request_no_delete
        BEFORE DELETE ON execution_slot_requests
        BEGIN SELECT RAISE(ABORT, 'EXECUTION_SLOT_REQUEST_NO_DELETE'); END""",
    """CREATE TRIGGER IF NOT EXISTS execution_slot_claim_identity_immutable
        BEFORE UPDATE OF slot_number, request_uid, project_uid,
            owner_work_uid, owner_session_uid, owner_generation, created_at
            ON execution_slot_claims
        BEGIN SELECT RAISE(ABORT, 'EXECUTION_SLOT_CLAIM_IDENTITY_IMMUTABLE'); END""",
)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(value):
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_owner(owner) -> ResourceOwner:
    if isinstance(owner, ResourceOwner):
        result = owner
    elif isinstance(owner, Mapping):
        result = ResourceOwner(
            work_uid=owner.get("work_uid"),
            session_uid=owner.get("session_uid"),
            generation=owner.get("generation"),
        )
    else:
        raise ResourceRegistryError("RESOURCE_OWNER_REQUIRED")
    for value in (result.work_uid, result.session_uid):
        if not isinstance(value, str) or not value.strip():
            raise ResourceRegistryError("RESOURCE_OWNER_INVALID")
    if (
        isinstance(result.generation, bool)
        or not isinstance(result.generation, int)
        or result.generation < 1
    ):
        raise ResourceRegistryError("RESOURCE_OWNER_INVALID")
    return result


def _owner_dict(owner: ResourceOwner):
    return {
        "work_uid": owner.work_uid,
        "session_uid": owner.session_uid,
        "generation": owner.generation,
    }


class ResourceRegistry:
    """Reserve batches of project resources in the IdentityStore database."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.identity = IdentityStore(self.root)
        self.path = self.identity.path

    def _canonical_path(self, value):
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            raise ResourceRegistryError("RESOURCE_IDENTITY_INVALID")
        candidate = Path(os.path.expanduser(value))
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            raise ResourceRegistryError("RESOURCE_IDENTITY_INVALID") from exc
        return os.path.normcase(str(resolved))

    @staticmethod
    def _canonical_branch(value):
        if not isinstance(value, str):
            raise ResourceRegistryError("RESOURCE_IDENTITY_INVALID")
        result = value.strip()
        if result.startswith("refs/heads/"):
            result = result[len("refs/heads/"):]
        if (
            not result
            or result.startswith("-")
            or result.endswith((".", "/"))
            or result.endswith(".lock")
            or result == "@"
            or any(ord(char) < 32 for char in result)
            or any(
                char in result
                for char in (" ", "\\", "..", "~", "^", ":", "?", "*", "[")
            )
            or "//" in result
        ):
            raise ResourceRegistryError("RESOURCE_IDENTITY_INVALID")
        return result

    @staticmethod
    def _canonical_slot(value):
        if not isinstance(value, str):
            raise ResourceRegistryError("RESOURCE_IDENTITY_INVALID")
        result = value.strip()
        if not result.startswith(EXECUTION_SLOT_PREFIX):
            raise ResourceRegistryError("RESOURCE_IDENTITY_INVALID")
        number = result[len(EXECUTION_SLOT_PREFIX):]
        if (
            not number.isdigit()
            or int(number) < 1
            or int(number) > EXECUTION_SLOT_COUNT
        ):
            raise ResourceRegistryError("RESOURCE_IDENTITY_INVALID")
        return EXECUTION_SLOT_PREFIX + str(int(number))

    def _normalize_resources(self, resources: Iterable):
        if isinstance(resources, (str, bytes, Mapping)):
            raise ResourceRegistryError("RESOURCE_BATCH_INVALID")
        try:
            values = list(resources)
        except TypeError as exc:
            raise ResourceRegistryError("RESOURCE_BATCH_INVALID") from exc
        if not values:
            raise ResourceRegistryError("RESOURCE_BATCH_EMPTY")

        normalized = []
        seen = set()
        for resource in values:
            if isinstance(resource, Mapping):
                kind = resource.get("kind")
                identity = resource.get("identity")
                disposition = resource.get("disposition", "release")
            else:
                try:
                    values_in_resource = tuple(resource)
                except (TypeError, ValueError) as exc:
                    raise ResourceRegistryError("RESOURCE_BATCH_INVALID") from exc
                if len(values_in_resource) == 2:
                    kind, identity = values_in_resource
                    disposition = "release"
                elif len(values_in_resource) == 3:
                    kind, identity, disposition = values_in_resource
                else:
                    raise ResourceRegistryError("RESOURCE_BATCH_INVALID")
            if kind not in RESOURCE_KINDS:
                raise ResourceRegistryError("RESOURCE_KIND_INVALID")
            if disposition not in RESOURCE_DISPOSITIONS:
                raise ResourceRegistryError("RESOURCE_DISPOSITION_INVALID")
            if kind == "branch":
                canonical = self._canonical_branch(identity)
            elif kind == "slot":
                canonical = self._canonical_slot(identity)
            else:
                canonical = self._canonical_path(identity)
            key = (kind, canonical)
            if key in seen:
                raise ResourceRegistryError(
                    "RESOURCE_DUPLICATE " + kind + " " + canonical
                )
            seen.add(key)
            normalized.append({
                "kind": kind,
                "identity": canonical,
                "disposition": disposition,
            })
        return tuple(
            sorted(normalized, key=lambda row: (
                row["kind"], row["identity"], row["disposition"]))
        )

    @staticmethod
    def slot_identity(number):
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or number > EXECUTION_SLOT_COUNT
        ):
            raise ResourceRegistryError("RESOURCE_SLOT_INVALID")
        return EXECUTION_SLOT_PREFIX + str(number)

    def _open(self, readonly=False):
        if not self.path.is_file():
            raise ResourceRegistryError("RESOURCE_AUTHORITY_MISSING")
        try:
            if readonly:
                db = sqlite3.connect(
                    self.path.as_uri() + "?mode=ro",
                    uri=True,
                    timeout=30,
                )
            else:
                db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=30000")
            problem = IdentityStore._schema_problem(db)
            if problem is not None:
                raise ResourceRegistryError(
                    "RESOURCE_AUTHORITY_CORRUPT " + problem
                )
            stored = db.execute(
                "SELECT value FROM metadata WHERE key='project_uid'"
            ).fetchone()
            expected = self.identity._identity_file_project_uid()
            if stored is None or stored["value"] != expected:
                raise ResourceRegistryError("RESOURCE_PROJECT_MISMATCH")
            return db
        except ResourceRegistryError:
            if "db" in locals():
                db.close()
            raise
        except IdentityStoreError as exc:
            if "db" in locals():
                db.close()
            raise ResourceRegistryError(
                "RESOURCE_AUTHORITY_INVALID " + str(exc)
            ) from exc
        except sqlite3.Error as exc:
            if "db" in locals():
                db.close()
            raise ResourceRegistryError("RESOURCE_AUTHORITY_INVALID") from exc

    @staticmethod
    def _tables(db):
        return {
            row["name"]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    @staticmethod
    def _ensure_schema(db):
        for statement in RESOURCE_SCHEMA_SQL:
            db.execute(statement)

    @staticmethod
    def _request_digest(project_uid, owner, resources, scheduler=None):
        payload = {
            "schema": RESOURCE_SCHEMA_VERSION,
            "project_uid": project_uid,
            "owner": _owner_dict(owner),
            "resources": list(resources),
        }
        if scheduler is not None:
            payload["scheduler"] = scheduler
        return _digest(payload)

    @staticmethod
    def _normalize_scheduler(token):
        if token is None:
            return None
        if (
            not isinstance(token, dict)
            or type(token.get("revision")) is not int
            or token["revision"] < 0
            or not isinstance(token.get("cursor"), dict)
            or not isinstance(token["cursor"].get("work_uid"), str)
            or not token["cursor"]["work_uid"].strip()
        ):
            raise ResourceRegistryError("SCHEDULER_TOKEN_INVALID")
        try:
            return json.loads(
                json.dumps(
                    token,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
        except (TypeError, ValueError) as exc:
            raise ResourceRegistryError("SCHEDULER_TOKEN_INVALID") from exc

    @staticmethod
    def _slot_request_digest(project_uid, owner):
        return _digest(
            {
                "schema": RESOURCE_SCHEMA_VERSION,
                "project_uid": project_uid,
                "owner": _owner_dict(owner),
                "kind": "slot",
            }
        )

    def _slot_requests(self, requests):
        if isinstance(requests, (str, bytes, Mapping)):
            raise ResourceRegistryError("RESOURCE_SLOT_BATCH_INVALID")
        try:
            values = list(requests)
        except TypeError as exc:
            raise ResourceRegistryError("RESOURCE_SLOT_BATCH_INVALID") from exc
        if len(values) > EXECUTION_SLOT_COUNT:
            raise ResourceRegistryError("RESOURCE_SLOTS_EXHAUSTED")
        normalized = []
        seen = set()
        for item in values:
            if isinstance(item, Mapping):
                request_uid = item.get("request_uid")
                owner = item.get("owner")
            else:
                try:
                    request_uid, owner = tuple(item)
                except (TypeError, ValueError) as exc:
                    raise ResourceRegistryError(
                        "RESOURCE_SLOT_BATCH_INVALID"
                    ) from exc
            if (
                not isinstance(request_uid, str)
                or not request_uid.strip()
            ):
                raise ResourceRegistryError(
                    "RESOURCE_SLOT_REQUEST_UID_INVALID"
                )
            if request_uid in seen:
                raise ResourceRegistryError(
                    "RESOURCE_SLOT_REQUEST_DUPLICATE " + request_uid
                )
            seen.add(request_uid)
            normalized.append(
                (request_uid, _normalize_owner(owner))
            )
        return tuple(normalized)

    def _snapshot(self, row):
        try:
            raw_resources = json.loads(row["resources_json"])
        except (TypeError, ValueError) as exc:
            raise ResourceRegistryError(
                "RESOURCE_AUTHORITY_CORRUPT invalid request resources"
            ) from exc
        # Native execution/review grants can own a slot without filesystem
        # resources. Public filesystem reservation still rejects empty batches.
        resources = list(self._normalize_resources(raw_resources)) if raw_resources else []
        return {
            "schema": RESOURCE_SCHEMA_VERSION,
            "request_uid": row["request_uid"],
            "state": row["state"],
            "owner": {
                "work_uid": row["owner_work_uid"],
                "session_uid": row["owner_session_uid"],
                "generation": row["owner_generation"],
            },
            "resources": resources,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _slot_snapshot(self, row):
        number = row["slot_number"]
        return {
            "schema": RESOURCE_SCHEMA_VERSION,
            "request_uid": row["request_uid"],
            "state": row["state"],
            "owner": {
                "work_uid": row["owner_work_uid"],
                "session_uid": row["owner_session_uid"],
                "generation": row["owner_generation"],
            },
            "resources": [
                {
                    "kind": "slot",
                    "identity": self.slot_identity(number),
                    "disposition": "release",
                }
            ],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _load_request(db, request_uid, owner):
        if not isinstance(request_uid, str) or not request_uid.strip():
            raise ResourceRegistryError("RESOURCE_REQUEST_UID_INVALID")
        row = db.execute(
            "SELECT * FROM resource_requests WHERE request_uid=?",
            (request_uid,),
        ).fetchone()
        if row is None:
            raise ResourceRegistryError("RESOURCE_REQUEST_NOT_FOUND")
        actual_owner = ResourceOwner(
            work_uid=row["owner_work_uid"],
            session_uid=row["owner_session_uid"],
            generation=row["owner_generation"],
        )
        if actual_owner != owner:
            raise ResourceRegistryError("RESOURCE_OWNER_MISMATCH")
        return row

    @staticmethod
    def _load_slot_request(db, request_uid, owner):
        if not isinstance(request_uid, str) or not request_uid.strip():
            raise ResourceRegistryError(
                "RESOURCE_SLOT_REQUEST_UID_INVALID"
            )
        row = db.execute(
            "SELECT * FROM execution_slot_requests WHERE request_uid=?",
            (request_uid,),
        ).fetchone()
        if row is None:
            raise ResourceRegistryError("RESOURCE_SLOT_REQUEST_NOT_FOUND")
        actual_owner = ResourceOwner(
            work_uid=row["owner_work_uid"],
            session_uid=row["owner_session_uid"],
            generation=row["owner_generation"],
        )
        if actual_owner != owner:
            raise ResourceRegistryError("RESOURCE_OWNER_MISMATCH")
        return row

    def _reserve_with_slots(
        self, request_uid, owner, resources, slot_requests, scheduler=None,
    ):
        if not isinstance(request_uid, str) or not request_uid.strip():
            raise ResourceRegistryError("RESOURCE_REQUEST_UID_INVALID")
        scheduler = self._normalize_scheduler(scheduler)
        with closing(self._open()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_schema(db)
                project_uid = db.execute(
                    "SELECT value FROM metadata WHERE key='project_uid'"
                ).fetchone()["value"]
                request_digest = self._request_digest(
                    project_uid, owner, resources, scheduler
                )
                existing = db.execute(
                    "SELECT * FROM resource_requests WHERE request_uid=?",
                    (request_uid,),
                ).fetchone()
                if existing is not None:
                    legacy_resources = tuple(
                        {
                            "kind": resource["kind"],
                            "identity": resource["identity"],
                        }
                        for resource in resources
                    )
                    legacy_digest = self._request_digest(
                        project_uid, owner, legacy_resources, scheduler
                    )
                    if existing["request_digest"] not in {
                        request_digest, legacy_digest
                    }:
                        raise ResourceRegistryError(
                            "RESOURCE_REQUEST_INPUT_CHANGED"
                        )
                    if (
                        slot_requests
                        and existing["state"] == "released"
                    ):
                        raise ResourceRegistryError(
                            "RESOURCE_REQUEST_RELEASED"
                        )
                else:
                    for resource in resources:
                        conflicts = self._conflicting_rows(db, resource)
                        conflict = conflicts[0] if conflicts else None
                        if conflict is not None:
                            raise ResourceConflictError(
                                "RESOURCE_CONFLICT "
                                + resource["kind"]
                                + " "
                                + resource["identity"]
                            )

                existing_slots = {}
                new_slot_requests = []
                for slot_uid, slot_owner in slot_requests:
                    slot_row = db.execute(
                        "SELECT * FROM execution_slot_requests "
                        "WHERE request_uid=?",
                        (slot_uid,),
                    ).fetchone()
                    if slot_row is None:
                        new_slot_requests.append((slot_uid, slot_owner))
                        continue
                    if slot_row["request_digest"] != (
                        self._slot_request_digest(project_uid, slot_owner)
                    ):
                        raise ResourceRegistryError(
                            "RESOURCE_SLOT_REQUEST_INPUT_CHANGED"
                        )
                    if slot_row["state"] == "released":
                        raise ResourceRegistryError(
                            "RESOURCE_SLOT_REQUEST_RELEASED"
                        )
                    slot_claim = db.execute(
                        "SELECT * FROM execution_slot_claims "
                        "WHERE request_uid=? AND slot_number=?",
                        (slot_uid, slot_row["slot_number"]),
                    ).fetchone()
                    if (
                        slot_claim is None
                        or slot_claim["state"] not in ACTIVE_STATES
                        or any(
                            slot_claim[field] != slot_row[field]
                            for field in (
                                "project_uid", "owner_work_uid",
                                "owner_session_uid", "owner_generation",
                            )
                        )
                    ):
                        raise ResourceRegistryError(
                            "RESOURCE_SLOT_CLAIMS_INCOMPLETE"
                        )
                    existing_slots[slot_uid] = slot_row

                if existing_slots and new_slot_requests:
                    raise ResourceRegistryError(
                        "RESOURCE_SLOT_SET_INCOMPLETE"
                    )
                if slot_requests and existing is None and existing_slots:
                    raise ResourceRegistryError(
                        "RESOURCE_SLOT_SET_INCOMPLETE"
                    )
                if slot_requests and existing is not None:
                    if new_slot_requests or not existing_slots:
                        raise ResourceRegistryError(
                            "RESOURCE_SLOT_SET_INCOMPLETE"
                        )
                    expected_claims = {
                        (resource["kind"], resource["identity"])
                        for resource in resources
                    }
                    actual_claims = {
                        (row["kind"], row["identity"])
                        for row in db.execute(
                            "SELECT kind,identity FROM resource_claims "
                            "WHERE request_uid=?",
                            (request_uid,),
                        ).fetchall()
                    }
                    if actual_claims != expected_claims:
                        raise ResourceRegistryError(
                            "RESOURCE_REQUEST_CLAIMS_INCOMPLETE"
                        )

                created_at = _now()
                if existing is None:
                    resources_json = json.dumps(
                        resources,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    db.execute(
                        "INSERT INTO resource_requests("
                        "request_uid,request_digest,project_uid,owner_work_uid,"
                        "owner_session_uid,owner_generation,state,resources_json,"
                        "created_at,updated_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            request_uid,
                            request_digest,
                            project_uid,
                            owner.work_uid,
                            owner.session_uid,
                            owner.generation,
                            "reserved",
                            resources_json,
                            created_at,
                            created_at,
                        ),
                    )
                    db.executemany(
                        "INSERT INTO resource_claims("
                        "kind,identity,request_uid,project_uid,owner_work_uid,"
                        "owner_session_uid,owner_generation,state,created_at,"
                        "updated_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                        [
                            (
                                resource["kind"],
                                resource["identity"],
                                request_uid,
                                project_uid,
                                owner.work_uid,
                                owner.session_uid,
                                owner.generation,
                                "reserved",
                                created_at,
                                created_at,
                            )
                            for resource in resources
                        ],
                    )

                if new_slot_requests:
                    used = {
                        row["slot_number"]
                        for row in db.execute(
                            "SELECT slot_number FROM execution_slot_claims"
                        ).fetchall()
                    }
                    free = [
                        number
                        for number in range(1, EXECUTION_SLOT_COUNT + 1)
                        if number not in used
                    ]
                    if len(free) < len(new_slot_requests):
                        raise ResourceConflictError(
                            "RESOURCE_SLOTS_EXHAUSTED"
                        )
                    for (slot_uid, slot_owner), number in zip(
                        new_slot_requests, free
                    ):
                        slot_digest = self._slot_request_digest(
                            project_uid, slot_owner
                        )
                        db.execute(
                            "INSERT INTO execution_slot_requests("
                            "request_uid,request_digest,project_uid,"
                            "owner_work_uid,owner_session_uid,owner_generation,"
                            "slot_number,state,created_at,updated_at"
                            ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (
                                slot_uid,
                                slot_digest,
                                project_uid,
                                slot_owner.work_uid,
                                slot_owner.session_uid,
                                slot_owner.generation,
                                number,
                                "reserved",
                                created_at,
                                created_at,
                            ),
                        )
                        db.execute(
                            "INSERT INTO execution_slot_claims("
                            "slot_number,request_uid,project_uid,"
                            "owner_work_uid,owner_session_uid,owner_generation,"
                            "state,created_at,updated_at"
                            ") VALUES(?,?,?,?,?,?,?,?,?)",
                            (
                                number,
                                slot_uid,
                                project_uid,
                                slot_owner.work_uid,
                                slot_owner.session_uid,
                                slot_owner.generation,
                                "reserved",
                                created_at,
                                created_at,
                            ),
                        )

                row = db.execute(
                    "SELECT * FROM resource_requests WHERE request_uid=?",
                    (request_uid,),
                ).fetchone()
                slot_rows = {
                    slot_uid: db.execute(
                        "SELECT * FROM execution_slot_requests "
                        "WHERE request_uid=?",
                        (slot_uid,),
                    ).fetchone()
                    for slot_uid, _owner in slot_requests
                }
                if scheduler is not None and existing is None:
                    self._advance_scheduler(db,scheduler)
                db.commit()
                return self._snapshot(row), {
                    uid: self._slot_snapshot(slot_row)
                    for uid, slot_row in slot_rows.items()
                }
            except ResourceRegistryError:
                db.rollback()
                raise
            except sqlite3.Error as exc:
                db.rollback()
                raise ResourceRegistryError(
                    "RESOURCE_RESERVE_FAILED"
                ) from exc

    def reserve(self, request_uid, owner, resources):
        owner = _normalize_owner(owner)
        resources = self._normalize_resources(resources)
        if any(resource["kind"] == "slot" for resource in resources):
            raise ResourceRegistryError(
                "RESOURCE_SLOT_REQUIRES_EXECUTION_BATCH"
            )
        snapshot, _slots = self._reserve_with_slots(
            request_uid, owner, resources, (),
        )
        return snapshot

    def reserve_execution_batch(
        self, request_uid, owner, resources, slot_requests, *, scheduler=None,
    ):
        """Atomically reserve workspace resources and worker execution slots."""
        owner = _normalize_owner(owner)
        resources = self._normalize_resources(resources)
        if any(resource["kind"] == "slot" for resource in resources):
            raise ResourceRegistryError(
                "RESOURCE_SLOT_REQUIRES_EXECUTION_BATCH"
            )
        slots = self._slot_requests(slot_requests)
        resources_snapshot, slots_snapshot = self._reserve_with_slots(
            request_uid, owner, resources, slots, scheduler=scheduler,
        )
        return {
            "resources": resources_snapshot,
            "slots": slots_snapshot,
        }

    def reserve_execution_slot(self,request_uid,owner):
        """A native review/legacy execution consumes capacity without a tree."""
        owner=_normalize_owner(owner)
        _resource,slots=self._reserve_with_slots(request_uid,owner,(),((request_uid,owner),))
        return slots[request_uid]

    @staticmethod
    def _advance_scheduler(db,token):
        token=ResourceRegistry._normalize_scheduler(token)
        if token is None:
            raise ResourceRegistryError('SCHEDULER_TOKEN_INVALID')
        db.execute('CREATE TABLE IF NOT EXISTS scheduling_cursor ('
                   'singleton INTEGER PRIMARY KEY CHECK(singleton=1),'
                   'revision INTEGER NOT NULL,cursor_json TEXT NOT NULL)')
        row=db.execute('SELECT revision FROM scheduling_cursor WHERE singleton=1').fetchone()
        revision=row['revision'] if row else 0
        if revision!=token['revision']:
            raise ResourceRegistryError('SCHEDULER_VERSION_CHANGED')
        db.execute('INSERT INTO scheduling_cursor(singleton,revision,cursor_json) VALUES(1,?,?) '
                   'ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision,cursor_json=excluded.cursor_json',
                   (revision+1,json.dumps(token['cursor'],sort_keys=True)))

    def scheduler_snapshot(self):
        """No DDL/mkdir: cursor and all active flat execution slots."""
        with closing(self._open(readonly=True)) as db:
            tables=self._tables(db)
            occupied=(db.execute('SELECT COUNT(*) FROM execution_slot_claims').fetchone()[0]
                      if 'execution_slot_claims' in tables else 0)
            row=(db.execute('SELECT revision,cursor_json FROM scheduling_cursor WHERE singleton=1').fetchone()
                 if 'scheduling_cursor' in tables else None)
            try:
                return {'revision':row['revision'] if row else 0,
                        'cursor':json.loads(row['cursor_json']) if row else None,'occupied':occupied}
            except (ValueError,TypeError) as exc:
                raise ResourceRegistryError('SCHEDULER_AUTHORITY_INVALID') from exc

    def commit(self, request_uid, owner):
        owner = _normalize_owner(owner)
        with closing(self._open()) as db:
            if not {"resource_requests", "resource_claims"}.issubset(
                self._tables(db)
            ):
                raise ResourceRegistryError("RESOURCE_REQUEST_NOT_FOUND")
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._load_request(db, request_uid, owner)
                if row["state"] == "released":
                    raise ResourceRegistryError("RESOURCE_REQUEST_RELEASED")
                if row["state"] == "reserved":
                    updated_at = _now()
                    db.execute(
                        "UPDATE resource_claims SET state='committed',updated_at=? "
                        "WHERE request_uid=?",
                        (updated_at, request_uid),
                    )
                    db.execute(
                        "UPDATE resource_requests SET state='committed',updated_at=? "
                        "WHERE request_uid=?",
                        (updated_at, request_uid),
                    )
                    row = db.execute(
                        "SELECT * FROM resource_requests WHERE request_uid=?",
                        (request_uid,),
                    ).fetchone()
                db.commit()
                return self._snapshot(row)
            except ResourceRegistryError:
                db.rollback()
                raise
            except sqlite3.Error as exc:
                db.rollback()
                raise ResourceRegistryError(
                    "RESOURCE_COMMIT_FAILED"
                ) from exc

    def release(self, request_uid, owner):
        owner = _normalize_owner(owner)
        with closing(self._open()) as db:
            if not {"resource_requests", "resource_claims"}.issubset(
                self._tables(db)
            ):
                raise ResourceRegistryError("RESOURCE_REQUEST_NOT_FOUND")
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._load_request(db, request_uid, owner)
                if row["state"] != "released":
                    updated_at = _now()
                    db.execute(
                        "DELETE FROM resource_claims WHERE request_uid=?",
                        (request_uid,),
                    )
                    db.execute(
                        "UPDATE resource_requests SET state='released',updated_at=? "
                        "WHERE request_uid=?",
                        (updated_at, request_uid),
                    )
                    row = db.execute(
                        "SELECT * FROM resource_requests WHERE request_uid=?",
                        (request_uid,),
                    ).fetchone()
                db.commit()
                return self._snapshot(row)
            except ResourceRegistryError:
                db.rollback()
                raise
            except sqlite3.Error as exc:
                db.rollback()
                raise ResourceRegistryError(
                    "RESOURCE_RELEASE_FAILED"
                ) from exc

    def release_execution_slot(
        self, request_uid, owner, identity=None,
    ):
        """Release one worker slot without releasing its workspace claims."""
        owner = _normalize_owner(owner)
        expected_identity = (
            self._canonical_slot(identity)
            if identity is not None else None
        )
        with closing(self._open()) as db:
            required = {
                "execution_slot_requests", "execution_slot_claims",
            }
            if not required.issubset(self._tables(db)):
                raise ResourceRegistryError(
                    "RESOURCE_SLOT_REQUEST_NOT_FOUND"
                )
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._load_slot_request(db, request_uid, owner)
                actual_identity = self.slot_identity(row["slot_number"])
                if (
                    expected_identity is not None
                    and expected_identity != actual_identity
                ):
                    raise ResourceRegistryError(
                        "RESOURCE_SLOT_IDENTITY_MISMATCH"
                    )
                if row["state"] != "released":
                    claim = db.execute(
                        "SELECT * FROM execution_slot_claims "
                        "WHERE request_uid=? AND slot_number=?",
                        (request_uid, row["slot_number"]),
                    ).fetchone()
                    if claim is None:
                        raise ResourceRegistryError(
                            "RESOURCE_SLOT_AUTHORITY_CORRUPT"
                        )
                    updated_at = _now()
                    db.execute(
                        "DELETE FROM execution_slot_claims "
                        "WHERE request_uid=? AND slot_number=?",
                        (request_uid, row["slot_number"]),
                    )
                    db.execute(
                        "UPDATE execution_slot_requests "
                        "SET state='released',updated_at=? "
                        "WHERE request_uid=?",
                        (updated_at, request_uid),
                    )
                    row = db.execute(
                        "SELECT * FROM execution_slot_requests "
                        "WHERE request_uid=?",
                        (request_uid,),
                    ).fetchone()
                db.commit()
                return self._slot_snapshot(row)
            except ResourceRegistryError:
                db.rollback()
                raise
            except sqlite3.Error as exc:
                db.rollback()
                raise ResourceRegistryError(
                    "RESOURCE_SLOT_RELEASE_FAILED"
                ) from exc

    def get_slot(self, request_uid):
        with closing(self._open(readonly=True)) as db:
            tables = self._tables(db)
            if "execution_slot_requests" not in tables:
                return None
            if "execution_slot_claims" not in tables:
                raise ResourceRegistryError(
                    "RESOURCE_AUTHORITY_CORRUPT "
                    "incomplete execution slot schema"
                )
            row = db.execute(
                "SELECT * FROM execution_slot_requests "
                "WHERE request_uid=?",
                (request_uid,),
            ).fetchone()
            return None if row is None else self._slot_snapshot(row)

    def get(self, request_uid):
        with closing(self._open(readonly=True)) as db:
            tables = self._tables(db)
            if "resource_requests" not in tables:
                return None
            if "resource_claims" not in tables:
                raise ResourceRegistryError(
                    "RESOURCE_AUTHORITY_CORRUPT incomplete resource schema"
                )
            row = db.execute(
                "SELECT * FROM resource_requests WHERE request_uid=?",
                (request_uid,),
            ).fetchone()
            return None if row is None else self._snapshot(row)

    def list_for_work(self, work_uid):
        """Return active claims, including partially released request cohorts."""
        if not isinstance(work_uid, str) or not work_uid.strip():
            raise ResourceRegistryError("RESOURCE_WORK_UID_INVALID")
        with closing(self._open(readonly=True)) as db:
            tables = self._tables(db)
            resource_tables = {"resource_requests", "resource_claims"}
            slot_tables = {
                "execution_slot_requests", "execution_slot_claims",
            }
            if not tables.intersection(resource_tables | slot_tables):
                return []
            if (
                tables.intersection(resource_tables)
                and not resource_tables.issubset(tables)
            ) or (
                tables.intersection(slot_tables)
                and not slot_tables.issubset(tables)
            ):
                raise ResourceRegistryError(
                    "RESOURCE_AUTHORITY_CORRUPT incomplete resource schema"
                )
            result = []
            if resource_tables.issubset(tables):
                rows = db.execute(
                    "SELECT c.*,r.state request_state,r.resources_json "
                    "FROM resource_claims c "
                    "JOIN resource_requests r "
                    "ON c.request_uid=r.request_uid "
                    "WHERE c.owner_work_uid=? "
                    "AND c.state IN ('reserved','committed') "
                    "AND r.state IN ('reserved','committed') "
                    "ORDER BY c.created_at,c.rowid",
                    (work_uid,),
                ).fetchall()
                for claim in rows:
                    try:
                        requested = self._normalize_resources(
                            json.loads(claim["resources_json"])
                        )
                    except (TypeError, ValueError) as exc:
                        raise ResourceRegistryError(
                            "RESOURCE_AUTHORITY_CORRUPT "
                            "invalid request resources"
                        ) from exc
                    disposition = next(
                        (
                            item["disposition"] for item in requested
                            if (
                                item["kind"] == claim["kind"]
                                and item["identity"] == claim["identity"]
                            )
                        ),
                        "release",
                    )
                    result.append({
                        "request_uid": claim["request_uid"],
                        "request_state": claim["request_state"],
                        "owner": {
                            "work_uid": claim["owner_work_uid"],
                            "session_uid": claim["owner_session_uid"],
                            "generation": claim["owner_generation"],
                        },
                        "kind": claim["kind"],
                        "identity": claim["identity"],
                        "disposition": disposition,
                        "created_at": claim["created_at"],
                        "updated_at": claim["updated_at"],
                    })
            if slot_tables.issubset(tables):
                slots = db.execute(
                    "SELECT c.*,r.state request_state "
                    "FROM execution_slot_claims c "
                    "JOIN execution_slot_requests r "
                    "ON c.request_uid=r.request_uid "
                    "WHERE c.owner_work_uid=? "
                    "AND c.state IN ('reserved','committed') "
                    "AND r.state IN ('reserved','committed') "
                    "ORDER BY c.created_at,c.slot_number",
                    (work_uid,),
                ).fetchall()
                for claim in slots:
                    result.append({
                        "request_uid": claim["request_uid"],
                        "request_state": claim["request_state"],
                        "owner": {
                            "work_uid": claim["owner_work_uid"],
                            "session_uid": claim["owner_session_uid"],
                            "generation": claim["owner_generation"],
                        },
                        "kind": "slot",
                        "identity": self.slot_identity(
                            claim["slot_number"]
                        ),
                        "disposition": "release",
                        "created_at": claim["created_at"],
                        "updated_at": claim["updated_at"],
                    })
            return result

    @staticmethod
    def _conflicting_rows(db, resource, exclude_request=None):
        tables = ResourceRegistry._tables(db)
        rows = []
        if "resource_claims" in tables:
            rows.extend(db.execute(
                "SELECT * FROM resource_claims WHERE request_uid!=?",
                (exclude_request or '',),
            ).fetchall())
        if "execution_slot_claims" in tables:
            for row in db.execute(
                "SELECT * FROM execution_slot_claims WHERE request_uid!=?",
                (exclude_request or '',),
            ).fetchall():
                rows.append({
                    "kind": "slot",
                    "identity": (
                        EXECUTION_SLOT_PREFIX + str(row["slot_number"])
                    ),
                    "request_uid": row["request_uid"],
                    "state": row["state"],
                    "owner_work_uid": row["owner_work_uid"],
                    "owner_session_uid": row["owner_session_uid"],
                    "owner_generation": row["owner_generation"],
                })
        result = []
        for row in rows:
            if resource['kind'] == 'slot' or row['kind'] == 'slot':
                overlap = (
                    resource['kind'] == row['kind']
                    and resource['identity'] == row['identity']
                )
            elif resource['kind'] == 'branch' or row['kind'] == 'branch':
                overlap = (
                    resource['kind'] == row['kind']
                    and resource['identity'] == row['identity']
                )
            else:
                try:
                    overlap = os.path.commonpath([resource['identity'], row['identity']]) in {
                        resource['identity'], row['identity']}
                except ValueError:
                    overlap = False
            if overlap:
                result.append(row)
        return result

    def conflicts(self, resources, exclude_request=None):
        normalized = self._normalize_resources(resources)
        with closing(self._open(readonly=True)) as db:
            tables = self._tables(db)
            if not tables.intersection(
                {"resource_requests", "resource_claims"}
            ):
                return []
            if not {"resource_requests", "resource_claims"}.issubset(tables):
                raise ResourceRegistryError(
                    "RESOURCE_AUTHORITY_CORRUPT incomplete resource schema"
                )
            result = []
            for resource in normalized:
                rows = self._conflicting_rows(db, resource, exclude_request)
                result.extend(
                    {
                        "kind": row["kind"],
                        "identity": row["identity"],
                        "request_uid": row["request_uid"],
                        "state": row["state"],
                        "owner": {
                            "work_uid": row["owner_work_uid"],
                            "session_uid": row["owner_session_uid"],
                            "generation": row["owner_generation"],
                        },
                    }
                    for row in rows
                )
            return result
