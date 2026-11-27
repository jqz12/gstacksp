"""Authoritative local allocation of project-wide orchestration identities."""
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from .orchestration_types import EventEnvelope, ObjectRef, OperationRecord, RequestRef, digest_value


SCHEMA_VERSION = 1
IDENTITY_SPECS = {
    "work": ("GSP-", 6),
    "todo": ("TODO-", 6),
    "session": ("S-", 6),
    "dispatch": ("D-", 6),
    "task": ("T", 2),
}
REQUIRED_COLUMNS = {
    "metadata": {"key", "value"},
    "sequences": {"kind", "scope_uid", "minimum_width", "next_value"},
    "objects": {
        "uid",
        "kind",
        "project_uid",
        "canonical_id",
        "scope_uid",
        "created_at",
    },
    "aliases": {"alias", "object_kind", "scope_uid", "object_uid", "created_at"},
    "requests": {
        "request_uid", "logical_key", "input_digest", "project_uid",
        "work_uid", "attempt_uid", "operation_kind", "object_kind",
        "scope_uid", "result_uid", "state", "created_at",
    },
    "operations": {
        "operation_uid", "request_uid", "logical_key", "input_digest",
        "project_uid", "work_uid", "attempt_uid", "operation_kind",
        "controller_generation", "cancel_generation", "contract_uid",
        "contract_version", "contract_digest", "phase",
        "expected_state_digest", "state_path", "new_state_digest",
        "result_ref", "error", "created_at", "updated_at",
    },
    "events": {
        "event_uid", "operation_uid", "kind", "project_uid", "work_uid",
        "task_uid", "dispatch_uid", "attempt_uid", "contract_uid",
        "contract_version", "contract_digest", "controller_generation",
        "cancel_generation", "payload_json", "envelope_json", "semantic_key",
        "envelope_digest", "created_at",
    },
}
CORE_TABLE_SQL = {
    "metadata": """CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    "sequences": """CREATE TABLE IF NOT EXISTS sequences (
        kind TEXT NOT NULL,
        scope_uid TEXT NOT NULL,
        minimum_width INTEGER NOT NULL,
        next_value INTEGER NOT NULL CHECK(next_value >= 1),
        PRIMARY KEY(kind, scope_uid)
    )""",
    "objects": """CREATE TABLE IF NOT EXISTS objects (
        uid TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        project_uid TEXT NOT NULL,
        canonical_id TEXT NOT NULL,
        scope_uid TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(kind, scope_uid, canonical_id)
    )""",
    "aliases": """CREATE TABLE IF NOT EXISTS aliases (
        alias TEXT NOT NULL,
        object_kind TEXT NOT NULL,
        scope_uid TEXT NOT NULL,
        object_uid TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(alias, object_kind, scope_uid),
        FOREIGN KEY(object_uid) REFERENCES objects(uid)
    )""",
    "requests": """CREATE TABLE IF NOT EXISTS requests (
        request_uid TEXT PRIMARY KEY,
        logical_key TEXT NOT NULL,
        input_digest TEXT NOT NULL,
        project_uid TEXT NOT NULL,
        work_uid TEXT NOT NULL,
        attempt_uid TEXT NOT NULL,
        operation_kind TEXT NOT NULL,
        object_kind TEXT NOT NULL,
        scope_uid TEXT NOT NULL,
        result_uid TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(project_uid, logical_key),
        FOREIGN KEY(result_uid) REFERENCES objects(uid)
    )""",
    "operations": """CREATE TABLE IF NOT EXISTS operations (
        operation_uid TEXT PRIMARY KEY,
        request_uid TEXT NOT NULL UNIQUE,
        logical_key TEXT NOT NULL,
        input_digest TEXT NOT NULL,
        project_uid TEXT NOT NULL,
        work_uid TEXT NOT NULL,
        attempt_uid TEXT NOT NULL,
        operation_kind TEXT NOT NULL,
        controller_generation INTEGER,
        cancel_generation INTEGER,
        contract_uid TEXT,
        contract_version INTEGER,
        contract_digest TEXT,
        phase TEXT NOT NULL CHECK(phase IN (
            'intent','prepared','file-written','committed','failed'
        )),
        expected_state_digest TEXT NOT NULL,
        state_path TEXT NOT NULL,
        new_state_digest TEXT NOT NULL,
        result_ref TEXT,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(project_uid, logical_key)
    )""",
    "events": """CREATE TABLE IF NOT EXISTS events (
        event_uid TEXT PRIMARY KEY,
        operation_uid TEXT NOT NULL,
        kind TEXT NOT NULL,
        project_uid TEXT NOT NULL,
        work_uid TEXT,
        task_uid TEXT,
        dispatch_uid TEXT,
        attempt_uid TEXT,
        contract_uid TEXT,
        contract_version INTEGER,
        contract_digest TEXT,
        controller_generation INTEGER,
        cancel_generation INTEGER,
        payload_json TEXT NOT NULL,
        envelope_json TEXT NOT NULL,
        semantic_key TEXT NOT NULL,
        envelope_digest TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(operation_uid, semantic_key),
        FOREIGN KEY(operation_uid) REFERENCES operations(operation_uid)
    )""",
}

OPERATION_TARGET_GUARD = "operations_targets_immutable"
OPERATION_TARGET_METADATA_KEY = "operations_target_guard"
CANDIDATE_TABLE_SQL = """CREATE TABLE IF NOT EXISTS operation_candidates (
    operation_uid TEXT PRIMARY KEY,
    state_path TEXT NOT NULL,
    semantic_digest TEXT NOT NULL,
    candidate_text TEXT NOT NULL,
    candidate_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(operation_uid) REFERENCES operations(operation_uid)
)"""
GUARD_SQL = {
    "metadata_identity_immutable": """CREATE TRIGGER IF NOT EXISTS metadata_identity_immutable
        BEFORE UPDATE ON metadata
        WHEN OLD.key IN ('schema_version', 'incarnation', 'project_uid')
        BEGIN SELECT RAISE(ABORT, 'IDENTITY_METADATA_IMMUTABLE'); END""",
    "metadata_identity_no_delete": """CREATE TRIGGER IF NOT EXISTS metadata_identity_no_delete
        BEFORE DELETE ON metadata
        WHEN OLD.key IN ('schema_version', 'incarnation', 'project_uid')
        BEGIN SELECT RAISE(ABORT, 'IDENTITY_METADATA_NO_DELETE'); END""",
    "objects_immutable": """CREATE TRIGGER IF NOT EXISTS objects_immutable
        BEFORE UPDATE OF uid, kind, project_uid, canonical_id, scope_uid, created_at ON objects
        BEGIN SELECT RAISE(ABORT, 'IDENTITY_IMMUTABLE'); END""",
    "objects_no_delete": """CREATE TRIGGER IF NOT EXISTS objects_no_delete
        BEFORE DELETE ON objects
        BEGIN SELECT RAISE(ABORT, 'IDENTITY_NO_DELETE'); END""",
    "sequences_no_delete": """CREATE TRIGGER IF NOT EXISTS sequences_no_delete
        BEFORE DELETE ON sequences
        BEGIN SELECT RAISE(ABORT, 'IDENTITY_SEQUENCE_NO_DELETE'); END""",
    "sequences_never_rewind": """CREATE TRIGGER IF NOT EXISTS sequences_never_rewind
        BEFORE UPDATE OF next_value ON sequences
        WHEN NEW.next_value < OLD.next_value
        BEGIN SELECT RAISE(ABORT, 'IDENTITY_SEQUENCE_REWIND'); END""",
    "aliases_immutable": """CREATE TRIGGER IF NOT EXISTS aliases_immutable
        BEFORE UPDATE OF alias, object_kind, scope_uid, object_uid, created_at ON aliases
        BEGIN SELECT RAISE(ABORT, 'IDENTITY_ALIAS_IMMUTABLE'); END""",
    "aliases_no_delete": """CREATE TRIGGER IF NOT EXISTS aliases_no_delete
        BEFORE DELETE ON aliases
        BEGIN SELECT RAISE(ABORT, 'IDENTITY_ALIAS_NO_DELETE'); END""",
    "requests_immutable": """CREATE TRIGGER IF NOT EXISTS requests_immutable
        BEFORE UPDATE ON requests
        BEGIN SELECT RAISE(ABORT, 'IDENTITY_REQUEST_IMMUTABLE'); END""",
    "requests_no_delete": """CREATE TRIGGER IF NOT EXISTS requests_no_delete
        BEFORE DELETE ON requests
        BEGIN SELECT RAISE(ABORT, 'IDENTITY_REQUEST_NO_DELETE'); END""",
    "operations_immutable": """CREATE TRIGGER IF NOT EXISTS operations_immutable
        BEFORE UPDATE OF operation_uid, request_uid, logical_key,
            input_digest, project_uid, work_uid, attempt_uid,
            operation_kind, controller_generation, cancel_generation,
            contract_uid, contract_version, contract_digest,
            expected_state_digest, created_at ON operations
        BEGIN SELECT RAISE(ABORT, 'OPERATION_IMMUTABLE'); END""",
    "operations_no_delete": """CREATE TRIGGER IF NOT EXISTS operations_no_delete
        BEFORE DELETE ON operations
        BEGIN SELECT RAISE(ABORT, 'OPERATION_NO_DELETE'); END""",
    "operations_no_rewind": """CREATE TRIGGER IF NOT EXISTS operations_no_rewind
        BEFORE UPDATE OF phase ON operations
        WHEN OLD.phase = 'committed' AND NEW.phase != 'committed'
        BEGIN SELECT RAISE(ABORT, 'OPERATION_PHASE_REWIND'); END""",
    OPERATION_TARGET_GUARD: """CREATE TRIGGER IF NOT EXISTS operations_targets_immutable
        BEFORE UPDATE OF state_path, new_state_digest ON operations
        WHEN NOT (
            OLD.phase = 'intent'
            AND OLD.state_path = ''
            AND OLD.new_state_digest = ''
            AND NEW.phase = 'prepared'
            AND NEW.state_path != ''
            AND NEW.new_state_digest != ''
            AND length(NEW.new_state_digest) = 64
            AND NEW.new_state_digest = lower(NEW.new_state_digest)
            AND NEW.new_state_digest NOT GLOB '*[^0-9a-f]*'
            AND instr('/' || replace(NEW.state_path, char(92), '/') || '/', '/../') = 0
            AND instr(NEW.state_path, ':') = 0
            AND substr(NEW.state_path, 1, 1) NOT IN ('/', '\\')
        )
        BEGIN SELECT RAISE(ABORT, 'OPERATION_TARGET_IMMUTABLE'); END""",
    "events_immutable": """CREATE TRIGGER IF NOT EXISTS events_immutable
        BEFORE UPDATE ON events
        BEGIN SELECT RAISE(ABORT, 'EVENT_IMMUTABLE'); END""",
    "events_no_delete": """CREATE TRIGGER IF NOT EXISTS events_no_delete
        BEFORE DELETE ON events
        BEGIN SELECT RAISE(ABORT, 'EVENT_NO_DELETE'); END""",
}
REQUIRED_GUARDS = set(GUARD_SQL) - {OPERATION_TARGET_GUARD}
CANDIDATE_GUARD_SQL = {
    "operation_candidates_immutable": """CREATE TRIGGER IF NOT EXISTS operation_candidates_immutable
        BEFORE UPDATE ON operation_candidates
        BEGIN SELECT RAISE(ABORT, 'CANDIDATE_IMMUTABLE'); END""",
    "operation_candidates_no_delete": """CREATE TRIGGER IF NOT EXISTS operation_candidates_no_delete
        BEFORE DELETE ON operation_candidates
        BEGIN SELECT RAISE(ABORT, 'CANDIDATE_NO_DELETE'); END""",
}
CANDIDATE_TABLE_COLUMNS = (
    ("operation_uid", "TEXT", 0, 1),
    ("state_path", "TEXT", 1, 0),
    ("semantic_digest", "TEXT", 1, 0),
    ("candidate_text", "TEXT", 1, 0),
    ("candidate_digest", "TEXT", 1, 0),
    ("created_at", "TEXT", 1, 0),
)
CANDIDATE_FOREIGN_KEY = (
    "operations",
    "operation_uid",
    "operation_uid",
    "NO ACTION",
    "NO ACTION",
    "NONE",
)
OPERATION_PHASES = {"intent", "prepared", "file-written", "committed", "failed"}
MIGRATION_PENDING_RELATIVE = ".gsp/migrations/pending.json"
MIGRATION_STAGE_SCHEMA = 1


def _normalize_schema_sql(value):
    if not isinstance(value, str):
        return None
    # SQL keywords/formatting are insensitive to case, string constants are
    # not. Never fold CHECK/DEFAULT/RAISE literals or their inner whitespace.
    parts = re.split(r"('(?:''|[^'])*')", value)
    result = []
    for index, part in enumerate(parts):
        if index % 2:
            result.append(part)
        else:
            part = re.sub(r"\bIF\s+NOT\s+EXISTS\b", "", part, flags=re.IGNORECASE)
            result.append(" ".join(part.lower().split()))
    return " ".join(result)


def _table_shape(db, table):
    # Table names come exclusively from the owning DDL, never from a caller.
    columns = tuple(tuple(row) for row in db.execute("PRAGMA table_info(" + table + ")"))
    foreign_keys = tuple(tuple(row) for row in db.execute("PRAGMA foreign_key_list(" + table + ")"))
    unique_indexes = []
    for index in db.execute("PRAGMA index_list(" + table + ")"):
        if index["unique"]:
            name = index["name"].replace("'", "''")
            parts = tuple(tuple(row)[1:] for row in db.execute("PRAGMA index_xinfo('" + name + "')"))
            unique_indexes.append((index["origin"], index["partial"], parts))
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return (columns, foreign_keys, tuple(sorted(unique_indexes)), _normalize_schema_sql(sql["sql"]))


@lru_cache(maxsize=1)
def _core_schema_shapes():
    # The same DDL creates authorities and attests them. Reference databases
    # are private/in-memory; an authority read never repairs or migrates it.
    profiles = {table: set() for table in CORE_TABLE_SQL}
    for legacy_requests in (False, True):
        with closing(sqlite3.connect(":memory:")) as reference:
            reference.row_factory = sqlite3.Row
            for table, sql in CORE_TABLE_SQL.items():
                if table == "requests" and legacy_requests:
                    sql = re.sub(r"\s*object_kind TEXT NOT NULL,", "", sql, count=1)
                    sql = re.sub(r"\s*scope_uid TEXT NOT NULL,", "", sql, count=1)
                reference.execute(sql)
            if legacy_requests:
                for column in ("object_kind", "scope_uid"):
                    reference.execute("ALTER TABLE requests ADD COLUMN " + column + " TEXT NOT NULL DEFAULT ''")
            for table in CORE_TABLE_SQL:
                profiles[table].add(_table_shape(reference, table))
    return {table: frozenset(shapes) for table, shapes in profiles.items()}


class IdentityStoreError(Exception):
    """Stable identity error with a machine-readable prefix."""


@dataclass(frozen=True)
class OperationCandidate:
    operation_uid: str
    state_path: str
    semantic_digest: str
    candidate_text: str
    candidate_digest: str
    created_at: str


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _semantic_text(value):
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


def _parse_candidate_text(candidate_text):
    if not isinstance(candidate_text, str):
        raise IdentityStoreError("STATE_CANDIDATE_INVALID")
    try:
        candidate_text.encode("utf-8")
        value = json.loads(
            candidate_text,
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
        canonical = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    except (UnicodeError, TypeError, ValueError) as exc:
        raise IdentityStoreError("STATE_CANDIDATE_INVALID") from exc
    if not isinstance(value, dict) or canonical != candidate_text:
        raise IdentityStoreError("STATE_CANDIDATE_INVALID")
    return value


def _canonical_pattern(kind):
    prefix, width = IDENTITY_SPECS[kind]
    return re.compile(r"^" + re.escape(prefix) + r"\d{" + str(width) + r",}$")


def _parse_number(kind, token):
    prefix, _ = IDENTITY_SPECS[kind]
    if not isinstance(token, str) or not token.startswith(prefix):
        raise IdentityStoreError("ID_NOT_CANONICAL")
    suffix = token[len(prefix):]
    if not suffix.isdigit() or not _canonical_pattern(kind).fullmatch(token):
        raise IdentityStoreError("ID_NOT_CANONICAL")
    number = int(suffix)
    if number < 1:
        raise IdentityStoreError("ID_NOT_CANONICAL")
    return number


def _canonical(kind, number):
    prefix, width = IDENTITY_SPECS[kind]
    return prefix + str(int(number)).zfill(width)


def _normalize_explicit(kind, token):
    """Return canonical token, optional short alias, and numeric value."""
    if not isinstance(token, str):
        raise IdentityStoreError("ID_NOT_CANONICAL")
    if kind == "task" and re.fullmatch(r"T\d+", token):
        number = int(token[1:])
        if number < 1:
            raise IdentityStoreError("ID_NOT_CANONICAL")
        canonical = _canonical(kind, number)
        short = "T" + str(number)
        if token not in (canonical, short):
            raise IdentityStoreError("ID_NOT_CANONICAL")
        return canonical, short if short != canonical else None, number
    number = _parse_number(kind, token)
    canonical = _canonical(kind, number)
    if token != canonical:
        raise IdentityStoreError("ID_NOT_CANONICAL")
    return canonical, None, number


def _is_canonical(kind, token):
    try:
        return token == _canonical(kind, _parse_number(kind, token))
    except IdentityStoreError:
        return False


def _task_read_alias(kind, canonical):
    if kind != "task":
        return None
    number = _parse_number(kind, canonical)
    short = "T" + str(number)
    return short if short != canonical else None


def _alias_number(kind, alias):
    prefix, _ = IDENTITY_SPECS[kind]
    if not isinstance(alias, str) or not alias.startswith(prefix):
        return None
    suffix = alias[len(prefix):]
    if not suffix.isdigit():
        return None
    number = int(suffix)
    return number if number >= 1 else None


class IdentityStore:
    """One SQLite authority shared by all worktrees of a project.

    Text IDs are presentation identities.  UIDs are never reused.  Sequences
    are updated in the same IMMEDIATE transaction that inserts an object.
    """

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.meta = self.root / ".gsp" / "orchestration"
        self.path = self.meta / "registry.sqlite"
        self.metadata_path = self.meta / "registry.json"

    def _migration_pending(self):
        return (self.root / MIGRATION_PENDING_RELATIVE).is_file()

    def _reject_pending_migration(self):
        if self._migration_pending():
            raise IdentityStoreError("IDENTITY_MIGRATION_PENDING")

    @staticmethod
    def _candidate_schema_problem(db):
        columns = tuple(
            (
                row["name"],
                (row["type"] or "").upper(),
                int(row["notnull"]),
                int(row["pk"]),
            )
            for row in db.execute("PRAGMA table_info(operation_candidates)")
        )
        if columns != CANDIDATE_TABLE_COLUMNS:
            return "schema-candidate-table-invalid"
        foreign_keys = tuple(
            (
                row["table"],
                row["from"],
                row["to"],
                row["on_update"],
                row["on_delete"],
                row["match"],
            )
            for row in db.execute("PRAGMA foreign_key_list(operation_candidates)")
        )
        if foreign_keys != (CANDIDATE_FOREIGN_KEY,):
            return "schema-candidate-foreign-key-invalid"
        trigger_sql = {
            row["name"]: row["sql"]
            for row in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
            )
        }
        for name, expected in CANDIDATE_GUARD_SQL.items():
            if _normalize_schema_sql(trigger_sql.get(name)) != _normalize_schema_sql(
                expected
            ):
                return "schema-guard-invalid:" + name
        return None

    @staticmethod
    def _schema_problem(db):
        tables = {
            row["name"]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not set(REQUIRED_COLUMNS).issubset(tables):
            return "schema-tables-missing"
        for table, allowed in _core_schema_shapes().items():
            if _table_shape(db, table) not in allowed:
                return "schema-table-invalid:" + table
        trigger_sql = {
            row["name"]: row["sql"]
            for row in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
            )
        }
        for name in REQUIRED_GUARDS:
            if _normalize_schema_sql(trigger_sql.get(name)) != _normalize_schema_sql(
                GUARD_SQL[name]
            ):
                return "schema-guard-invalid:" + name
        if "operation_candidates" in tables:
            problem = IdentityStore._candidate_schema_problem(db)
            if problem is not None:
                return problem
        target_guard = trigger_sql.get(OPERATION_TARGET_GUARD)
        target_marker = db.execute(
            "SELECT value FROM metadata WHERE key=?",
            (OPERATION_TARGET_METADATA_KEY,),
        ).fetchone()
        if target_guard is None:
            if target_marker is not None:
                return "schema-target-guard-missing"
        elif (
            target_marker is None
            or target_marker["value"] != "1"
            or _normalize_schema_sql(target_guard)
            != _normalize_schema_sql(GUARD_SQL[OPERATION_TARGET_GUARD])
        ):
            return "schema-target-guard-invalid"
        version = db.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        project = db.execute(
            "SELECT value FROM metadata WHERE key='project_uid'"
        ).fetchone()
        if (
            version is None
            or project is None
            or int(version["value"]) != SCHEMA_VERSION
            or not isinstance(project["value"], str)
            or not project["value"].strip()
            or project["value"] == "unknown"
        ):
            return "metadata-invalid"
        return None

    def _connect(self):
        self._reject_pending_migration()
        if not self.path.is_file():
            raise IdentityStoreError("IDENTITY_MIGRATION_REQUIRED")
        try:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=30000")
            if self._schema_problem(db) is not None:
                raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")
            stored_project = db.execute(
                "SELECT value FROM metadata WHERE key='project_uid'"
            ).fetchone()
            expected_project = self._identity_file_project_uid()
            if stored_project["value"] != expected_project:
                raise IdentityStoreError("PROJECT_MISMATCH")
            return db
        except IdentityStoreError:
            if "db" in locals():
                db.close()
            raise
        except (ValueError, sqlite3.Error) as exc:
            if "db" in locals():
                db.close()
            raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT") from exc

    @staticmethod
    def _schema(db):
        statements = (
            *CORE_TABLE_SQL.values(),
            CANDIDATE_TABLE_SQL,
            *GUARD_SQL.values(),
            *CANDIDATE_GUARD_SQL.values(),
        )
        for statement in statements:
            db.execute(statement)
        columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(requests)")
        }
        if "object_kind" not in columns:
            db.execute(
                "ALTER TABLE requests ADD COLUMN object_kind TEXT NOT NULL DEFAULT ''"
            )
        if "scope_uid" not in columns:
            db.execute(
                "ALTER TABLE requests ADD COLUMN scope_uid TEXT NOT NULL DEFAULT ''"
            )

    def _legacy_identity_surfaces(self):
        surfaces = []
        candidates = (
            self.root / ".gsp" / "work-counter.json",
            self.root / ".gsp" / "backlog" / "records.json",
        )
        surfaces.extend(
            str(path.relative_to(self.root))
            for path in candidates
            if path.exists()
        )
        todos = self.root / "TODOS.md"
        if todos.is_file():
            try:
                body = todos.read_text(encoding="utf-8-sig")
            except OSError:
                body = ""
            if "gsp-backlog:v2" in body or "gsp-todo:" in body:
                surfaces.append("TODOS.md")
        for path in sorted((self.root / "docs" / "work").glob("*/*/record.json")):
            surfaces.append(str(path.relative_to(self.root)))
        for path in sorted((self.root / "docs" / "work").glob("*/*/*/record.json")):
            surfaces.append(str(path.relative_to(self.root)))
        for path in sorted((self.root / ".gsp" / "runs").glob("*/state.json")):
            surfaces.append(str(path.relative_to(self.root)))
        return sorted(set(surfaces))

    def authority_state(self):
        if self.path.is_file():
            try:
                with closing(sqlite3.connect(self.path, timeout=5)) as db:
                    db.row_factory = sqlite3.Row
                    db.execute("PRAGMA busy_timeout=30000")
                    problem = self._schema_problem(db)
                    if problem is not None:
                        return {"state": "corrupt", "reason": problem}
                    project = db.execute(
                        "SELECT value FROM metadata WHERE key='project_uid'"
                    ).fetchone()
                    try:
                        identity_project = self._identity_file_project_uid()
                    except IdentityStoreError as exc:
                        return {
                            "state": "project_identity_invalid",
                            "reason": str(exc),
                        }
                    if project["value"] != identity_project:
                        return {
                            "state": "project_mismatch",
                            "reason": "identity-file-and-database-differ",
                        }
                    return {
                        "state": "ready",
                        "project_uid": project["value"],
                        "schema": SCHEMA_VERSION,
                    }
            except (OSError, sqlite3.Error, ValueError) as exc:
                return {
                    "state": "corrupt",
                    "reason": "sqlite-invalid",
                    "error": str(exc),
                }
        surfaces = self._legacy_identity_surfaces()
        if self.metadata_path.exists():
            surfaces.append("gsp/orchestration/registry.json")
        if surfaces:
            return {
                "state": "missing_authority",
                "surfaces": sorted(set(surfaces)),
            }
        return {"state": "uninitialized"}

    def initialize(self):
        self._reject_pending_migration()
        state = self.authority_state()
        if state["state"] == "missing_authority":
            raise IdentityStoreError(
                "IDENTITY_MIGRATION_REQUIRED authoritative allocator is missing"
            )
        if state["state"] == "corrupt":
            raise IdentityStoreError(
                "IDENTITY_AUTHORITY_CORRUPT authoritative allocator is invalid"
            )
        if state["state"] == "project_mismatch":
            raise IdentityStoreError("PROJECT_MISMATCH")
        if state["state"] == "project_identity_invalid":
            raise IdentityStoreError("PROJECT_IDENTITY_INVALID")
        project_uid = self._identity_file_project_uid()
        if state["state"] == "ready":
            if state["project_uid"] != project_uid:
                raise IdentityStoreError("PROJECT_MISMATCH")
            return self.read_schema()
        self.meta.mkdir(parents=True, exist_ok=True)
        temporary_path = self.meta / (
            ".registry." + uuid.uuid4().hex + ".sqlite"
        )
        try:
            with closing(self._connect_creating(temporary_path)) as db:
                try:
                    wal_mode = db.execute("PRAGMA journal_mode=WAL").fetchone()
                    if wal_mode is None or str(wal_mode[0]).lower() != "wal":
                        raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")
                    db.execute("BEGIN IMMEDIATE")
                    self._schema(db)
                    db.execute(
                        "INSERT OR IGNORE INTO metadata(key,value) "
                        "VALUES('schema_version',?)",
                        (str(SCHEMA_VERSION),),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO metadata(key,value) "
                        "VALUES('incarnation',?)",
                        (uuid.uuid4().hex,),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO metadata(key,value) "
                        "VALUES('project_uid',?)",
                        (project_uid,),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO metadata(key,value) "
                        "VALUES(?,?)",
                        (OPERATION_TARGET_METADATA_KEY, "1"),
                    )
                    stored_project = db.execute(
                        "SELECT value FROM metadata WHERE key='project_uid'"
                    ).fetchone()
                    version = db.execute(
                        "SELECT value FROM metadata WHERE key='schema_version'"
                    ).fetchone()
                    if (
                        stored_project["value"] != project_uid
                        or int(version["value"]) != SCHEMA_VERSION
                    ):
                        raise IdentityStoreError("PROJECT_MISMATCH")
                    db.commit()
                except sqlite3.Error:
                    db.rollback()
                    raise
            try:
                os.link(temporary_path, self.path)
            except FileExistsError:
                pass
            except OSError as exc:
                raise IdentityStoreError(
                    "IDENTITY_AUTHORITY_COMMIT_FAILED"
                ) from exc
            finally:
                temporary_path.unlink(missing_ok=True)
        except sqlite3.Error as exc:
            raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT") from exc
        finally:
            temporary_path.unlink(missing_ok=True)
        final_state = self.authority_state()
        if (
            final_state["state"] != "ready"
            or final_state["project_uid"] != project_uid
        ):
            raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")
        if not self.metadata_path.exists():
            metadata = {
                "schema": SCHEMA_VERSION,
                "project_uid": project_uid,
                "created_at": _now(),
            }
            temporary = self.metadata_path.with_name(
                ".registry.json." + uuid.uuid4().hex + ".tmp"
            )
            temporary.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            try:
                os.link(temporary, self.metadata_path)
            except FileExistsError:
                pass
            except OSError as exc:
                raise IdentityStoreError(
                    "IDENTITY_AUTHORITY_COMMIT_FAILED"
                ) from exc
            finally:
                temporary.unlink(missing_ok=True)
        if not self.metadata_path.is_file():
            raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")
        return self.read_schema()

    def _connect_creating(self, path=None):
        target = Path(path or self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            db = sqlite3.connect(target, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA journal_mode=DELETE")
            return db
        except sqlite3.Error as exc:
            try:
                db.close()
            except (NameError, sqlite3.Error):
                pass
            raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT") from exc

    def exists(self):
        return self.path.is_file()

    def _identity_file_project_uid(self):
        identity = self.root / ".gsp" / "project.json"
        if not identity.is_file():
            raise IdentityStoreError("PROJECT_IDENTITY_INVALID")
        try:
            value = json.loads(identity.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise IdentityStoreError("PROJECT_IDENTITY_INVALID") from exc
        project_uid = value.get("id")
        recorded_root = value.get("root")
        if (
            not isinstance(project_uid, str)
            or not project_uid.strip()
            or project_uid == "unknown"
        ):
            raise IdentityStoreError("PROJECT_IDENTITY_INVALID")
        if recorded_root is not None:
            if not isinstance(recorded_root, str) or not recorded_root.strip():
                raise IdentityStoreError("PROJECT_IDENTITY_INVALID")
            try:
                recorded_root_path = Path(recorded_root).resolve()
            except OSError as exc:
                raise IdentityStoreError("PROJECT_IDENTITY_INVALID") from exc
            if recorded_root_path != self.root:
                raise IdentityStoreError("PROJECT_IDENTITY_INVALID")
        return project_uid

    def project_uid(self):
        if self.path.is_file():
            with closing(self._connect()) as db:
                try:
                    row = db.execute(
                        "SELECT value FROM metadata WHERE key='project_uid'"
                    ).fetchone()
                except sqlite3.Error as exc:
                    raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT") from exc
            if row is not None and row["value"]:
                return row["value"]
            raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")
        if self.metadata_path.is_file():
            try:
                value = json.loads(
                    self.metadata_path.read_text(encoding="utf-8-sig")
                )
            except (OSError, ValueError) as exc:
                raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT") from exc
            project_uid = value.get("project_uid")
            if (
                not isinstance(project_uid, str)
                or not project_uid.strip()
                or project_uid == "unknown"
            ):
                raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")
            return project_uid
        return self._identity_file_project_uid()

    def local_allocator_surfaces(self):
        """Local mutable allocator state; tracked task/TODO text is not a ledger."""
        return [path for path in self._legacy_identity_surfaces()
                if Path(path).parts and Path(path).parts[0] == ".gsp"]

    def ensure_authority(self):
        self._reject_pending_migration()
        state = self.authority_state()
        if state["state"] == "uninitialized":
            self.initialize()
        elif state["state"] == "missing_authority":
            raise IdentityStoreError(
                "IDENTITY_MIGRATION_REQUIRED authoritative allocator is missing"
            )
        elif state["state"] == "corrupt":
            raise IdentityStoreError(
                "IDENTITY_AUTHORITY_CORRUPT authoritative allocator is invalid"
            )
        elif state["state"] == "project_mismatch":
            raise IdentityStoreError("PROJECT_MISMATCH")
        elif state["state"] == "project_identity_invalid":
            raise IdentityStoreError("PROJECT_IDENTITY_INVALID")

    def stage_identity_import(
        self, path, mapping, *, request_uid, manifest_uid, mapping_digest,
        created_at=None,
    ):
        """Create one canonical-schema candidate without opening allocator writes."""
        target = Path(path).resolve()
        try:
            target.relative_to(self.root)
        except ValueError:
            raise IdentityStoreError("MIGRATION_STAGE_PATH_INVALID")
        if not isinstance(mapping, dict) or mapping.get("schema") != 1:
            raise IdentityStoreError("MIGRATION_MAPPING_INVALID")
        if not isinstance(request_uid, str) or not request_uid:
            raise IdentityStoreError("MIGRATION_REQUEST_INVALID")
        if not isinstance(manifest_uid, str) or not manifest_uid:
            raise IdentityStoreError("MIGRATION_MANIFEST_INVALID")
        created_at = created_at or _now()
        if not isinstance(created_at, str) or not created_at:
            raise IdentityStoreError("MIGRATION_REQUEST_INVALID")
        project_uid = mapping.get("project_uid")
        if (
            not isinstance(project_uid, str)
            or project_uid != self._identity_file_project_uid()
        ):
            raise IdentityStoreError("PROJECT_MISMATCH")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(
            "." + target.name + "." + uuid.uuid4().hex + ".tmp"
        )
        try:
            with closing(self._connect_creating(temporary)) as db:
                try:
                    self._schema(db)
                    db.execute("BEGIN IMMEDIATE")
                    for row in mapping.get("objects") or ():
                        canonical = row.get("canonical_id")
                        kind = row.get("kind")
                        scope = row.get("scope_uid") or ""
                        if kind not in IDENTITY_SPECS or not isinstance(
                            row.get("uid"), str,
                        ):
                            raise IdentityStoreError("MIGRATION_MAPPING_INVALID")
                        normalized, _alias, _number = _normalize_explicit(
                            kind, canonical,
                        )
                        if normalized != canonical:
                            raise IdentityStoreError("MIGRATION_MAPPING_INVALID")
                        db.execute(
                            "INSERT INTO objects(uid,kind,project_uid,canonical_id,"
                            "scope_uid,created_at) VALUES(?,?,?,?,?,?)",
                            (
                                row["uid"], kind, project_uid, canonical,
                                scope, created_at,
                            ),
                        )
                    for row in mapping.get("aliases") or ():
                        kind = row.get("object_kind")
                        if kind not in IDENTITY_SPECS:
                            raise IdentityStoreError("MIGRATION_MAPPING_INVALID")
                        db.execute(
                            "INSERT INTO aliases(alias,object_kind,scope_uid,"
                            "object_uid,created_at) VALUES(?,?,?,?,?)",
                            (
                                row.get("alias"), kind,
                                row.get("scope_uid") or "",
                                row.get("object_uid"), created_at,
                            ),
                        )
                    for row in mapping.get("sequences") or ():
                        kind = row.get("kind")
                        if kind not in IDENTITY_SPECS:
                            raise IdentityStoreError("MIGRATION_MAPPING_INVALID")
                        scope = row.get("scope_uid") or ""
                        if kind in {"work", "todo", "session", "dispatch"}:
                            scope = ""
                        next_value = row.get("next_value")
                        width = row.get("minimum_width")
                        if (
                            type(next_value) is not int or next_value < 1
                            or type(width) is not int or width < 1
                        ):
                            raise IdentityStoreError("MIGRATION_MAPPING_INVALID")
                        db.execute(
                            "INSERT INTO sequences(kind,scope_uid,minimum_width,"
                            "next_value) VALUES(?,?,?,?)",
                            (kind, scope, width, next_value),
                        )
                    for key, value in (
                        ("project_uid", project_uid),
                        ("migration_state", "staged"),
                        ("migration_request_uid", request_uid),
                        ("migration_manifest_uid", manifest_uid),
                        ("migration_mapping_digest", mapping_digest),
                        (OPERATION_TARGET_METADATA_KEY, "1"),
                    ):
                        db.execute(
                            "INSERT INTO metadata(key,value) VALUES(?,?)",
                            (key, str(value)),
                        )
                    version = db.execute(
                        "SELECT 1 FROM metadata WHERE key='schema_version'"
                    ).fetchone()
                    if version is not None:
                        raise IdentityStoreError("MIGRATION_STAGE_ACTIVE")
                    db.commit()
                    db.execute("PRAGMA wal_checkpoint(FULL)")
                except Exception:
                    db.rollback()
                    raise
            os.replace(temporary, target)
            return {
                "state": "staged",
                "candidate_path": target.relative_to(self.root).as_posix(),
                "candidate_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        except (sqlite3.Error, OSError) as exc:
            raise IdentityStoreError("MIGRATION_STAGE_FAILED") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return self

    def read_schema(self):
        with closing(self._connect()) as db:
            try:
                metadata = {
                    row["key"]: row["value"]
                    for row in db.execute("SELECT key,value FROM metadata ORDER BY key")
                }
                sequences = [
                    dict(row)
                    for row in db.execute(
                        "SELECT kind,scope_uid,minimum_width,next_value FROM sequences "
                        "ORDER BY kind,scope_uid"
                    )
                ]
            except sqlite3.Error as exc:
                raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT") from exc
        return {
            "schema": int(metadata.get("schema_version", 0)),
            "incarnation": metadata.get("incarnation"),
            "project_uid": metadata.get("project_uid"),
            "sequences": sequences,
        }

    @staticmethod
    def _validate_request(request):
        if not isinstance(request, RequestRef):
            raise IdentityStoreError("REQUEST_REQUIRED")
        values = (
            request.request_uid,
            request.logical_key,
            request.input_digest,
            request.project_uid,
            request.operation_kind,
        )
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise IdentityStoreError("REQUEST_INVALID")
        for value in (request.work_uid, request.attempt_uid):
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise IdentityStoreError("REQUEST_INVALID")

    @staticmethod
    def _object(row):
        scope = row["scope_uid"]
        return ObjectRef(
            uid=row["uid"],
            kind=row["kind"],
            project_uid=row["project_uid"],
            canonical_id=row["canonical_id"],
            scope_uid=None if scope == "" else scope,
        )

    @staticmethod
    def _request_row_matches(row, request, kind, scope_key):
        return (
            row["request_uid"] == request.request_uid
            and row["logical_key"] == request.logical_key
            and row["input_digest"] == request.input_digest
            and row["project_uid"] == request.project_uid
            and row["work_uid"] == (request.work_uid or "")
            and row["attempt_uid"] == (request.attempt_uid or "")
            and row["operation_kind"] == request.operation_kind
            and row["object_kind"] == kind
            and row["scope_uid"] == scope_key
        )

    def allocate(self, kind, scope_uid, request, explicit_id=None):
        self._validate_request(request)
        if request.project_uid != self.project_uid():
            raise IdentityStoreError("PROJECT_MISMATCH")
        if kind not in IDENTITY_SPECS:
            raise IdentityStoreError("ID_KIND_INVALID")
        if scope_uid is not None and (
            not isinstance(scope_uid, str) or not scope_uid.strip()
        ):
            raise IdentityStoreError("ID_SCOPE_INVALID")
        if kind in ("work", "todo", "session", "dispatch") and scope_uid:
            raise IdentityStoreError("ID_SCOPE_INVALID")
        if kind == "task" and not scope_uid:
            raise IdentityStoreError("ID_SCOPE_REQUIRED")
        if kind in ("work", "todo") and request.work_uid:
            raise IdentityStoreError("REQUEST_WORK_INVALID")
        if kind in ("task", "session", "dispatch") and not request.work_uid:
            raise IdentityStoreError("REQUEST_WORK_REQUIRED")
        scope_key = scope_uid or ""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if kind == "task":
                    scoped_work = db.execute(
                        "SELECT kind FROM objects "
                        "WHERE uid=? AND project_uid=?",
                        (scope_uid, request.project_uid),
                    ).fetchone()
                    if scoped_work is None:
                        raise IdentityStoreError("ID_SCOPE_NOT_FOUND")
                    if scoped_work["kind"] != "work":
                        raise IdentityStoreError("ID_SCOPE_NOT_WORK")
                    if request.work_uid != scope_uid:
                        raise IdentityStoreError("REQUEST_SCOPE_MISMATCH")
                if kind in ("task", "session", "dispatch"):
                    request_work = db.execute(
                        "SELECT kind FROM objects "
                        "WHERE uid=? AND project_uid=?",
                        (request.work_uid, request.project_uid),
                    ).fetchone()
                    if request_work is None or request_work["kind"] != "work":
                        raise IdentityStoreError("REQUEST_WORK_INVALID")
                existing = db.execute(
                    "SELECT * FROM requests WHERE request_uid=?", (request.request_uid,)
                ).fetchone()
                if existing is None:
                    existing = db.execute(
                        "SELECT * FROM requests WHERE project_uid=? AND logical_key=?",
                        (request.project_uid, request.logical_key),
                    ).fetchone()
                if existing is not None:
                    if not self._request_row_matches(
                        existing, request, kind, scope_key
                    ):
                        raise IdentityStoreError("REQUEST_INPUT_CHANGED")
                    row = db.execute(
                        "SELECT * FROM objects WHERE uid=?", (existing["result_uid"],)
                    ).fetchone()
                    if row is None:
                        raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")
                    db.commit()
                    return self._object(row)

                row = None
                explicit_alias = None
                if explicit_id is not None:
                    explicit_id, explicit_alias, number = _normalize_explicit(
                        kind, explicit_id
                    )
                    row = db.execute(
                        "SELECT * FROM objects WHERE kind=? AND scope_uid=? AND canonical_id=?",
                        (kind, scope_key, explicit_id),
                    ).fetchone()
                    if row is not None:
                        raise IdentityStoreError("ID_CONFLICT")
                else:
                    sequence = db.execute(
                        "SELECT * FROM sequences WHERE kind=? AND scope_uid=?",
                        (kind, scope_key),
                    ).fetchone()
                    width = IDENTITY_SPECS[kind][1]
                    if sequence is None:
                        number = 1
                        db.execute(
                            "INSERT INTO sequences(kind,scope_uid,minimum_width,next_value) "
                            "VALUES(?,?,?,?)",
                            (kind, scope_key, width, number + 1),
                        )
                    else:
                        number = int(sequence["next_value"])
                        db.execute(
                            "UPDATE sequences SET next_value=? WHERE kind=? AND scope_uid=?",
                            (number + 1, kind, scope_key),
                        )
                    explicit_id = _canonical(kind, number)
                if explicit_id is not None:
                    number = _parse_number(kind, explicit_id)
                    aliases = db.execute(
                        "SELECT alias FROM aliases "
                        "WHERE object_kind=? AND scope_uid=?",
                        (kind, scope_key),
                    )
                    if any(
                        _alias_number(kind, row["alias"]) == number
                        for row in aliases
                    ):
                        raise IdentityStoreError("ID_CONFLICT")
                    sequence = db.execute(
                        "SELECT * FROM sequences WHERE kind=? AND scope_uid=?",
                        (kind, scope_key),
                    ).fetchone()
                    width = IDENTITY_SPECS[kind][1]
                    if sequence is None:
                        db.execute(
                            "INSERT INTO sequences(kind,scope_uid,minimum_width,next_value) "
                            "VALUES(?,?,?,?)",
                            (kind, scope_key, width, number + 1),
                        )
                    elif int(sequence["next_value"]) <= number:
                        db.execute(
                            "UPDATE sequences SET next_value=? WHERE kind=? AND scope_uid=?",
                            (number + 1, kind, scope_key),
                        )
                uid = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO objects(uid,kind,project_uid,canonical_id,scope_uid,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (uid, kind, request.project_uid, explicit_id, scope_key, _now()),
                )
                read_alias = explicit_alias or _task_read_alias(kind, explicit_id)
                if read_alias:
                    if db.execute(
                        "SELECT 1 FROM aliases "
                        "WHERE alias=? AND object_kind=? AND scope_uid=?",
                        (read_alias, kind, scope_key),
                    ).fetchone():
                        raise IdentityStoreError("ID_CONFLICT")
                    db.execute(
                        "INSERT INTO aliases(alias,object_kind,scope_uid,object_uid,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (read_alias, kind, scope_key, uid, _now()),
                    )
                db.execute(
                    "INSERT INTO requests(request_uid,logical_key,input_digest,project_uid,"
                    "work_uid,attempt_uid,operation_kind,object_kind,scope_uid,"
                    "result_uid,state,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        request.request_uid,
                        request.logical_key,
                        request.input_digest,
                        request.project_uid,
                        request.work_uid or "",
                        request.attempt_uid or "",
                        request.operation_kind,
                        kind,
                        scope_key,
                        uid,
                        "committed",
                        _now(),
                    ),
                )
                db.commit()
                return ObjectRef(
                    uid=uid,
                    kind=kind,
                    project_uid=request.project_uid,
                    canonical_id=explicit_id,
                    scope_uid=scope_uid,
                )
            except sqlite3.IntegrityError as exc:
                db.rollback()
                message = str(exc)
                if "UNIQUE" in message:
                    raise IdentityStoreError("ID_CONFLICT") from exc
                raise IdentityStoreError("IDENTITY_CONSTRAINT") from exc
            except Exception:
                db.rollback()
                raise

    def resolve(self, kind, token, scope_uid, write):
        if kind not in IDENTITY_SPECS:
            raise IdentityStoreError("ID_KIND_INVALID")
        if not isinstance(token, str) or not token:
            raise IdentityStoreError("ID_INVALID")
        typed_kind = None
        alias = token
        if ":" in token:
            typed_kind, alias = token.split(":", 1)
            if typed_kind != kind:
                raise IdentityStoreError("ID_AMBIGUOUS")
        scope_key = scope_uid or ""
        stable_uid = bool(re.fullmatch(r"[0-9a-f]{32}", alias))
        canonical = _is_canonical(kind, alias)
        if write and not (stable_uid or canonical):
            raise IdentityStoreError("ID_NOT_CANONICAL")
        with closing(self._connect()) as db:
            row = None
            if stable_uid:
                row = db.execute(
                    "SELECT * FROM objects WHERE uid=? AND kind=? AND scope_uid=?",
                    (alias, kind, scope_key),
                ).fetchone()
            elif canonical:
                row = db.execute(
                    "SELECT * FROM objects WHERE kind=? AND scope_uid=? AND canonical_id=?",
                    (kind, scope_key, alias),
                ).fetchone()
            if row is None:
                row = db.execute(
                    "SELECT objects.* FROM aliases JOIN objects ON objects.uid=aliases.object_uid "
                    "WHERE aliases.alias=? AND aliases.object_kind=? AND aliases.scope_uid=?",
                    (alias, kind, scope_key),
                ).fetchone()
            if row is None:
                raise IdentityStoreError("ID_NOT_FOUND")
            return self._object(row)

    def add_alias(self, kind, alias, object_uid, scope_uid):
        if kind not in IDENTITY_SPECS or not isinstance(alias, str) or not alias.strip():
            raise IdentityStoreError("ID_ALIAS_INVALID")
        scope_key = scope_uid or ""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                target = db.execute(
                    "SELECT * FROM objects WHERE uid=? AND kind=? AND scope_uid=?",
                    (object_uid, kind, scope_key),
                ).fetchone()
                if target is None:
                    raise IdentityStoreError("ID_NOT_FOUND")
                alias_number = _alias_number(kind, alias)
                if alias_number is not None:
                    canonical_equivalent = _canonical(kind, alias_number)
                    collision = db.execute(
                        "SELECT uid FROM objects "
                        "WHERE kind=? AND scope_uid=? AND canonical_id=?",
                        (kind, scope_key, canonical_equivalent),
                    ).fetchone()
                    if collision is not None and collision["uid"] != object_uid:
                        raise IdentityStoreError("ID_AMBIGUOUS")
                    if alias == canonical_equivalent:
                        if target["canonical_id"] != alias:
                            raise IdentityStoreError("ID_AMBIGUOUS")
                        sequence = db.execute(
                            "SELECT next_value FROM sequences "
                            "WHERE kind=? AND scope_uid=?",
                            (kind, scope_key),
                        ).fetchone()
                        minimum_width = IDENTITY_SPECS[kind][1]
                        if sequence is None:
                            db.execute(
                                "INSERT INTO sequences(kind,scope_uid,minimum_width,next_value) "
                                "VALUES(?,?,?,?)",
                                (kind, scope_key, minimum_width, alias_number + 1),
                            )
                        elif int(sequence["next_value"]) <= alias_number:
                            db.execute(
                                "UPDATE sequences SET next_value=? "
                                "WHERE kind=? AND scope_uid=?",
                                (alias_number + 1, kind, scope_key),
                            )
                        db.commit()
                        return self._object(target)
                existing = db.execute(
                    "SELECT object_uid FROM aliases WHERE alias=? AND object_kind=? AND scope_uid=?",
                    (alias, kind, scope_key),
                ).fetchone()
                if existing is not None:
                    if existing["object_uid"] != object_uid:
                        raise IdentityStoreError("ID_AMBIGUOUS")
                    if alias_number is not None:
                        sequence = db.execute(
                            "SELECT next_value FROM sequences "
                            "WHERE kind=? AND scope_uid=?",
                            (kind, scope_key),
                        ).fetchone()
                        minimum_width = IDENTITY_SPECS[kind][1]
                        if sequence is None:
                            db.execute(
                                "INSERT INTO sequences(kind,scope_uid,minimum_width,next_value) "
                                "VALUES(?,?,?,?)",
                                (kind, scope_key, minimum_width, alias_number + 1),
                            )
                        elif int(sequence["next_value"]) <= alias_number:
                            db.execute(
                                "UPDATE sequences SET next_value=? "
                                "WHERE kind=? AND scope_uid=?",
                                (alias_number + 1, kind, scope_key),
                            )
                    db.commit()
                    return self._object(target)
                if alias_number is not None:
                    sequence = db.execute(
                        "SELECT next_value FROM sequences "
                        "WHERE kind=? AND scope_uid=?",
                        (kind, scope_key),
                    ).fetchone()
                    minimum_width = IDENTITY_SPECS[kind][1]
                    if sequence is None:
                        db.execute(
                            "INSERT INTO sequences(kind,scope_uid,minimum_width,next_value) "
                            "VALUES(?,?,?,?)",
                            (kind, scope_key, minimum_width, alias_number + 1),
                        )
                    elif int(sequence["next_value"]) <= alias_number:
                        db.execute(
                            "UPDATE sequences SET next_value=? "
                            "WHERE kind=? AND scope_uid=?",
                            (alias_number + 1, kind, scope_key),
                        )
                db.execute(
                    "INSERT INTO aliases(alias,object_kind,scope_uid,object_uid,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (alias, kind, scope_key, object_uid, _now()),
                )
                db.commit()
                return self._object(target)
            except Exception:
                db.rollback()
                raise

    def count(self, kind, scope_uid):
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM objects WHERE kind=? AND scope_uid=?",
                (kind, scope_uid or ""),
            ).fetchone()
            return int(row["n"])

    def next_value(self, kind, scope_uid):
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT next_value FROM sequences WHERE kind=? AND scope_uid=?",
                (kind, scope_uid or ""),
            ).fetchone()
            return int(row["next_value"]) if row else 1

    @staticmethod
    def _validate_operation_request(request):
        IdentityStore._validate_request(request)
        for name in ("controller_generation", "cancel_generation"):
            value = getattr(request, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                raise IdentityStoreError("REQUEST_INVALID")
        contract = (
            request.contract_uid,
            request.contract_version,
            request.contract_digest,
        )
        if any(value is not None for value in contract):
            if (
                not isinstance(request.contract_uid, str)
                or not request.contract_uid.strip()
                or isinstance(request.contract_version, bool)
                or not isinstance(request.contract_version, int)
                or request.contract_version < 1
                or not isinstance(request.contract_digest, str)
                or not re.fullmatch(r"[a-f0-9]{64}", request.contract_digest)
            ):
                raise IdentityStoreError("REQUEST_INVALID")

    @staticmethod
    def _ensure_operation_target_guard(db):
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (OPERATION_TARGET_GUARD,),
        ).fetchone()
        marker = db.execute(
            "SELECT value FROM metadata WHERE key=?",
            (OPERATION_TARGET_METADATA_KEY,),
        ).fetchone()
        if row is None and marker is None:
            db.execute(GUARD_SQL[OPERATION_TARGET_GUARD])
            db.execute(
                "INSERT INTO metadata(key,value) VALUES(?,?)",
                (OPERATION_TARGET_METADATA_KEY, "1"),
            )
            return
        expected = _normalize_schema_sql(GUARD_SQL[OPERATION_TARGET_GUARD])
        if (
            row is None
            or marker is None
            or marker["value"] != "1"
            or _normalize_schema_sql(row["sql"]) != expected
        ):
            raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")

    @staticmethod
    def _valid_digest(value):
        return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)

    def _validate_operation_row(self, row):
        if row is None:
            raise IdentityStoreError("OPERATION_NOT_FOUND")
        if row["project_uid"] != self._identity_file_project_uid():
            raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")
        phase = row["phase"]
        if phase not in OPERATION_PHASES:
            raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")
        for name in ("created_at", "updated_at"):
            value = row[name]
            if not isinstance(value, str) or not value.strip():
                raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")
        for name in ("input_digest", "expected_state_digest"):
            if not self._valid_digest(row[name]):
                raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")
        for name in ("contract_digest", "result_ref"):
            value = row[name]
            if value not in (None, "") and not self._valid_digest(value):
                raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")

        state_path = row["state_path"]
        new_state_digest = row["new_state_digest"]
        if not isinstance(state_path, str) or not isinstance(
            new_state_digest, str
        ):
            raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")
        if phase == "intent":
            if state_path or new_state_digest:
                raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")
        elif phase == "failed" and not state_path and not new_state_digest:
            pass
        else:
            if not state_path or not new_state_digest:
                raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")
            if not self._valid_digest(new_state_digest):
                raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")
            try:
                normalized = self._normalize_operation_path(
                    self.root / state_path
                )
            except IdentityStoreError as exc:
                raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT") from exc
            if normalized != state_path:
                raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")
        if phase == "committed" and row["result_ref"] != new_state_digest:
            raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")
        return row

    def _operation(self, row):
        self._validate_operation_row(row)
        return OperationRecord(
            operation_uid=row["operation_uid"],
            request=RequestRef(
                request_uid=row["request_uid"],
                logical_key=row["logical_key"],
                input_digest=row["input_digest"],
                project_uid=row["project_uid"],
                work_uid=row["work_uid"] or None,
                attempt_uid=row["attempt_uid"] or None,
                operation_kind=row["operation_kind"],
                controller_generation=row["controller_generation"],
                cancel_generation=row["cancel_generation"],
                contract_uid=row["contract_uid"],
                contract_version=row["contract_version"],
                contract_digest=row["contract_digest"],
            ),
            phase=row["phase"],
            expected_state_digest=row["expected_state_digest"],
            result_ref=row["result_ref"],
            error=row["error"],
        )

    @staticmethod
    def _operation_request_matches(row, request):
        return (
            row["logical_key"] == request.logical_key
            and row["input_digest"] == request.input_digest
            and row["project_uid"] == request.project_uid
            and row["work_uid"] == (request.work_uid or "")
            and row["attempt_uid"] == (request.attempt_uid or "")
            and row["operation_kind"] == request.operation_kind
            and row["controller_generation"] == request.controller_generation
            and row["cancel_generation"] == request.cancel_generation
            and row["contract_uid"] == request.contract_uid
            and row["contract_version"] == request.contract_version
            and row["contract_digest"] == request.contract_digest
        )

    def begin_operation(self, request, expected_state_digest):
        self._validate_operation_request(request)
        if request.project_uid != self.project_uid():
            raise IdentityStoreError("PROJECT_MISMATCH")
        if not self._valid_digest(expected_state_digest):
            raise IdentityStoreError("STATE_DIGEST_INVALID")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_operation_target_guard(db)
                if request.work_uid:
                    work = db.execute(
                        "SELECT kind FROM objects WHERE uid=? AND project_uid=?",
                        (request.work_uid, request.project_uid),
                    ).fetchone()
                    if work is None or work["kind"] != "work":
                        raise IdentityStoreError("REQUEST_WORK_INVALID")
                row = db.execute(
                    "SELECT * FROM operations WHERE request_uid=?",
                    (request.request_uid,),
                ).fetchone()
                if row is None:
                    row = db.execute(
                        "SELECT * FROM operations WHERE project_uid=? AND logical_key=?",
                        (request.project_uid, request.logical_key),
                    ).fetchone()
                if row is not None:
                    self._validate_operation_row(row)
                    if not self._operation_request_matches(row, request):
                        raise IdentityStoreError("REQUEST_INPUT_CHANGED")
                    db.commit()
                    return self._operation(row)
                operation_uid = uuid.uuid4().hex
                timestamp = _now()
                db.execute(
                    "INSERT INTO operations("
                    "operation_uid,request_uid,logical_key,input_digest,"
                    "project_uid,work_uid,attempt_uid,operation_kind,"
                    "controller_generation,cancel_generation,contract_uid,"
                    "contract_version,contract_digest,phase,expected_state_digest,"
                    "state_path,new_state_digest,result_ref,error,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        operation_uid, request.request_uid, request.logical_key,
                        request.input_digest, request.project_uid,
                        request.work_uid or "", request.attempt_uid or "",
                        request.operation_kind, request.controller_generation,
                        request.cancel_generation, request.contract_uid,
                        request.contract_version, request.contract_digest, "intent",
                        expected_state_digest, "", "", "", "", timestamp, timestamp,
                    ),
                )
                row = db.execute(
                    "SELECT * FROM operations WHERE operation_uid=?",
                    (operation_uid,),
                ).fetchone()
                db.commit()
                return self._operation(row)
            except Exception:
                db.rollback()
                raise

    def get_operation(self, operation_uid):
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM operations WHERE operation_uid=?",
                (operation_uid,),
            ).fetchone()
            if row is None:
                raise IdentityStoreError("OPERATION_NOT_FOUND")
            return self._operation(row)

    def operation_details(self, operation_uid):
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM operations WHERE operation_uid=?",
                (operation_uid,),
            ).fetchone()
            if row is None:
                raise IdentityStoreError("OPERATION_NOT_FOUND")
            self._validate_operation_row(row)
            return dict(row)

    def operation_for_logical_key(self, logical_key):
        """Read one project-scoped intent by its existing unique key.

        A missing operation returns None. This lookup never creates an intent
        or candidate, and uses the same authoritative row validator as reads
        by operation UID. It is also the caller recovery lookup boundary.
        """
        if not isinstance(logical_key, str) or not logical_key.strip():
            raise IdentityStoreError("OPERATION_KEY_INVALID")
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM operations WHERE project_uid=? AND logical_key=?",
                (self._identity_file_project_uid(), logical_key),
            ).fetchone()
            if row is None:
                return None
            self._validate_operation_row(row)
            return dict(row)

    def operation_state_path(self, operation_uid):
        details = self.operation_details(operation_uid)
        if not details["state_path"]:
            raise IdentityStoreError("OPERATION_PATH_NOT_SET")
        relative_path = self._normalize_operation_path(
            self.root / details["state_path"]
        )
        if relative_path != details["state_path"]:
            raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")
        return (self.root / relative_path).resolve()

    @staticmethod
    def _ensure_operation_candidate_schema(db):
        db.execute(CANDIDATE_TABLE_SQL)
        for statement in CANDIDATE_GUARD_SQL.values():
            db.execute(statement)
        if IdentityStore._candidate_schema_problem(db) is not None:
            raise IdentityStoreError("IDENTITY_AUTHORITY_CORRUPT")

    def _candidate_from_row(self, row):
        candidate_text = row["candidate_text"]
        value = _parse_candidate_text(candidate_text)
        candidate_digest = row["candidate_digest"]
        semantic_digest = row["semantic_digest"]
        state_path = row["state_path"]
        if (
            not isinstance(candidate_digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", candidate_digest)
            or _digest_text(candidate_text) != candidate_digest
            or not isinstance(semantic_digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", semantic_digest)
            or semantic_digest != row["operation_input_digest"]
            or state_path != row["operation_state_path"]
            or candidate_digest != row["operation_new_state_digest"]
            or not isinstance(row["created_at"], str)
            or not row["created_at"].strip()
        ):
            raise IdentityStoreError("STATE_CANDIDATE_INVALID")
        try:
            normalized = self._normalize_operation_path(self.root / state_path)
        except IdentityStoreError as exc:
            raise IdentityStoreError("STATE_CANDIDATE_INVALID") from exc
        if normalized != state_path:
            raise IdentityStoreError("STATE_CANDIDATE_INVALID")
        _semantic_text(value)
        return OperationCandidate(
            operation_uid=row["operation_uid"],
            state_path=state_path,
            semantic_digest=semantic_digest,
            candidate_text=candidate_text,
            candidate_digest=candidate_digest,
            created_at=row["created_at"],
        )

    @staticmethod
    def _candidate_select(db, operation_uid):
        return db.execute(
            "SELECT c.operation_uid,c.state_path,c.semantic_digest,"
            "c.candidate_text,c.candidate_digest,c.created_at,"
            "o.input_digest AS operation_input_digest,"
            "o.state_path AS operation_state_path,"
            "o.new_state_digest AS operation_new_state_digest "
            "FROM operation_candidates c JOIN operations o "
            "ON o.operation_uid=c.operation_uid WHERE c.operation_uid=?",
            (operation_uid,),
        ).fetchone()

    def get_operation_candidate(self, operation_uid):
        with closing(self._connect()) as db:
            operation = db.execute(
                "SELECT * FROM operations WHERE operation_uid=?",
                (operation_uid,),
            ).fetchone()
            if operation is None:
                raise IdentityStoreError("OPERATION_NOT_FOUND")
            self._validate_operation_row(operation)
            present = db.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='operation_candidates'"
            ).fetchone()
            if present is None:
                return None
            row = self._candidate_select(db, operation_uid)
            if row is None:
                return None
            return self._candidate_from_row(row)

    def freeze_operation_candidate(
        self,
        operation_uid,
        state_path,
        candidate_text,
        semantic_digest,
    ):
        value = _parse_candidate_text(candidate_text)
        candidate_digest = _digest_text(candidate_text)
        if (
            not isinstance(semantic_digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", semantic_digest)
        ):
            raise IdentityStoreError("STATE_DIGEST_INVALID")
        relative_path = self._normalize_operation_path(state_path)
        incoming_semantic = _semantic_text(value)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_operation_target_guard(db)
                self._ensure_operation_candidate_schema(db)
                operation = db.execute(
                    "SELECT * FROM operations WHERE operation_uid=?",
                    (operation_uid,),
                ).fetchone()
                if operation is None:
                    raise IdentityStoreError("OPERATION_NOT_FOUND")
                self._validate_operation_row(operation)
                if semantic_digest != operation["input_digest"]:
                    raise IdentityStoreError("REQUEST_INPUT_CHANGED")
                existing_row = self._candidate_select(db, operation_uid)
                if existing_row is not None:
                    existing = self._candidate_from_row(existing_row)
                    if existing.state_path != relative_path:
                        raise IdentityStoreError("REQUEST_INPUT_CHANGED")
                    stored = _parse_candidate_text(existing.candidate_text)
                    stored_semantic = _semantic_text(stored)
                    if stored_semantic != incoming_semantic:
                        raise IdentityStoreError("REQUEST_INPUT_CHANGED")
                    db.commit()
                    return existing
                if operation["phase"] == "failed":
                    raise IdentityStoreError("OPERATION_FAILED")
                if operation["phase"] == "committed":
                    raise IdentityStoreError("OPERATION_PHASE_INVALID")
                if operation["phase"] in ("prepared", "file-written"):
                    if (
                        operation["state_path"] != relative_path
                        or operation["new_state_digest"] != candidate_digest
                    ):
                        raise IdentityStoreError("OPERATION_CANDIDATE_UNAVAILABLE")
                timestamp = _now()
                db.execute(
                    "INSERT INTO operation_candidates("
                    "operation_uid,state_path,semantic_digest,candidate_text,"
                    "candidate_digest,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        operation_uid,
                        relative_path,
                        semantic_digest,
                        candidate_text,
                        candidate_digest,
                        timestamp,
                    ),
                )
                if operation["phase"] == "intent":
                    db.execute(
                        "UPDATE operations SET phase='prepared',state_path=?,"
                        "new_state_digest=?,updated_at=? WHERE operation_uid=?",
                        (relative_path, candidate_digest, timestamp, operation_uid),
                    )
                db.commit()
                return OperationCandidate(
                    operation_uid=operation_uid,
                    state_path=relative_path,
                    semantic_digest=semantic_digest,
                    candidate_text=candidate_text,
                    candidate_digest=candidate_digest,
                    created_at=timestamp,
                )
            except Exception:
                db.rollback()
                raise

    def _normalize_operation_path(self, path):
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            candidate = candidate.resolve()
            return candidate.relative_to(self.root).as_posix()
        except (OSError, ValueError) as exc:
            raise IdentityStoreError("OPERATION_PATH_ESCAPE") from exc

    def prepare_operation(self, operation_uid, state_path, new_state_digest):
        if not self._valid_digest(new_state_digest):
            raise IdentityStoreError("STATE_DIGEST_INVALID")
        relative_path = self._normalize_operation_path(state_path)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_operation_target_guard(db)
                row = db.execute(
                    "SELECT * FROM operations WHERE operation_uid=?",
                    (operation_uid,),
                ).fetchone()
                if row is None:
                    raise IdentityStoreError("OPERATION_NOT_FOUND")
                self._validate_operation_row(row)
                if row["phase"] == "failed":
                    raise IdentityStoreError("OPERATION_FAILED")
                if row["phase"] == "committed":
                    if row["state_path"] == relative_path and row["new_state_digest"] == new_state_digest:
                        db.commit()
                        return new_state_digest
                    raise IdentityStoreError("REQUEST_INPUT_CHANGED")
                if row["phase"] in ("prepared", "file-written"):
                    if row["state_path"] != relative_path or row["new_state_digest"] != new_state_digest:
                        raise IdentityStoreError("REQUEST_INPUT_CHANGED")
                    db.commit()
                    return new_state_digest
                db.execute(
                    "UPDATE operations SET phase='prepared',state_path=?,new_state_digest=?,updated_at=? WHERE operation_uid=?",
                    (relative_path, new_state_digest, _now(), operation_uid),
                )
                row = db.execute(
                    "SELECT * FROM operations WHERE operation_uid=?",
                    (operation_uid,),
                ).fetchone()
                self._validate_operation_row(row)
                db.commit()
                return new_state_digest
            except Exception:
                db.rollback()
                raise

    def mark_operation_written(self, operation_uid):
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_operation_target_guard(db)
                row = db.execute(
                    "SELECT * FROM operations WHERE operation_uid=?",
                    (operation_uid,),
                ).fetchone()
                if row is None:
                    raise IdentityStoreError("OPERATION_NOT_FOUND")
                self._validate_operation_row(row)
                if row["phase"] == "prepared":
                    db.execute(
                        "UPDATE operations SET phase='file-written',updated_at=? WHERE operation_uid=?",
                        (_now(), operation_uid),
                    )
                elif row["phase"] not in ("file-written", "committed"):
                    raise IdentityStoreError("OPERATION_PHASE_INVALID")
                row = db.execute(
                    "SELECT * FROM operations WHERE operation_uid=?",
                    (operation_uid,),
                ).fetchone()
                self._validate_operation_row(row)
                db.commit()
                return self._operation(row)
            except Exception:
                db.rollback()
                raise

    def finish_operation(self, operation_uid, result_ref):
        if not isinstance(result_ref, str) or not result_ref.strip():
            raise IdentityStoreError("OPERATION_RESULT_INVALID")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_operation_target_guard(db)
                row = db.execute(
                    "SELECT * FROM operations WHERE operation_uid=?",
                    (operation_uid,),
                ).fetchone()
                if row is None:
                    raise IdentityStoreError("OPERATION_NOT_FOUND")
                self._validate_operation_row(row)
                if row["phase"] == "committed":
                    if row["result_ref"] != result_ref:
                        raise IdentityStoreError("OPERATION_RESULT_CHANGED")
                    db.commit()
                    return self._operation(row)
                if row["phase"] not in ("prepared", "file-written"):
                    raise IdentityStoreError("OPERATION_PHASE_INVALID")
                db.execute(
                    "UPDATE operations SET phase='committed',result_ref=?,error='',updated_at=? WHERE operation_uid=?",
                    (result_ref, _now(), operation_uid),
                )
                row = db.execute(
                    "SELECT * FROM operations WHERE operation_uid=?",
                    (operation_uid,),
                ).fetchone()
                self._validate_operation_row(row)
                db.commit()
                return self._operation(row)
            except Exception:
                db.rollback()
                raise

    def fail_operation(self, operation_uid, error):
        message = str(error or "").strip()
        if not message:
            raise IdentityStoreError("OPERATION_ERROR_REQUIRED")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_operation_target_guard(db)
                row = db.execute(
                    "SELECT * FROM operations WHERE operation_uid=?",
                    (operation_uid,),
                ).fetchone()
                if row is None:
                    raise IdentityStoreError("OPERATION_NOT_FOUND")
                self._validate_operation_row(row)
                if row["phase"] == "committed":
                    raise IdentityStoreError("OPERATION_ALREADY_COMMITTED")
                db.execute(
                    "UPDATE operations SET phase='failed',error=?,updated_at=? WHERE operation_uid=?",
                    (message[:2000], _now(), operation_uid),
                )
                row = db.execute(
                    "SELECT * FROM operations WHERE operation_uid=?",
                    (operation_uid,),
                ).fetchone()
                self._validate_operation_row(row)
                db.commit()
                return self._operation(row)
            except Exception:
                db.rollback()
                raise

    def state_write_anchor(self, state_path):
        """Bounded lineage for a caller's next incident, not a new allocator.

        A pending retry sees the same last committed operation and file bytes.
        A later incident after an intervening commit gets a different anchor,
        even with identical descriptions and a fixed clock.
        """
        relative = self._normalize_operation_path(state_path)
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM operations WHERE state_path=? AND phase='committed' "
                "ORDER BY rowid DESC LIMIT 1", (relative,),
            ).fetchone()
            if row is not None:
                self._validate_operation_row(row)
        return {"operation_uid": row["operation_uid"] if row is not None else None,
                "state_digest": hashlib.sha256(Path(state_path).read_bytes()).hexdigest()}

    def operation_count(self):
        with closing(self._connect()) as db:
            return int(db.execute("SELECT COUNT(*) FROM operations").fetchone()[0])

    @staticmethod
    def _validate_event(event):
        if not isinstance(event, EventEnvelope):
            raise IdentityStoreError("EVENT_REQUIRED")
        if not isinstance(event.event_uid, str) or not event.event_uid.strip():
            raise IdentityStoreError("EVENT_UID_INVALID")
        if not isinstance(event.kind, str) or not event.kind.strip():
            raise IdentityStoreError("EVENT_KIND_INVALID")
        if not isinstance(event.payload, dict):
            raise IdentityStoreError("EVENT_PAYLOAD_INVALID")
        for name in ("task_uid", "dispatch_uid", "attempt_uid", "contract_uid"):
            value = getattr(event, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise IdentityStoreError("EVENT_CONTEXT_INVALID")

    @staticmethod
    def _check_event_context(operation, event):
        # Task/dispatch identities become authoritative only after session
        # context binding exists.  Reject caller-supplied labels until that
        # binding can be verified instead of persisting forged attribution.
        if event.task_uid is not None:
            raise IdentityStoreError("EVENT_TASK_UNVERIFIED")
        if event.dispatch_uid is not None:
            raise IdentityStoreError("EVENT_DISPATCH_UNVERIFIED")
        if event.project_uid != operation["project_uid"]:
            raise IdentityStoreError("EVENT_PROJECT_MISMATCH")
        if (operation["work_uid"] or None) != event.work_uid:
            raise IdentityStoreError("EVENT_WORK_MISMATCH")
        if (operation["attempt_uid"] or None) != event.attempt_uid:
            raise IdentityStoreError("EVENT_ATTEMPT_MISMATCH")
        if (
            operation["controller_generation"] != event.controller_generation
            or operation["cancel_generation"] != event.cancel_generation
        ):
            raise IdentityStoreError("CONTROL_VERSION_CHANGED")
        if (
            operation["contract_uid"] != event.contract_uid
            or operation["contract_version"] != event.contract_version
            or operation["contract_digest"] != event.contract_digest
        ):
            raise IdentityStoreError("CONTRACT_CHANGED")

    def store_event(self, operation_uid, event):
        self._validate_event(event)
        operation = self.operation_details(operation_uid)
        if operation["project_uid"] != self.project_uid():
            raise IdentityStoreError("PROJECT_MISMATCH")
        if operation["phase"] != "committed":
            raise IdentityStoreError("OPERATION_PENDING")
        self._check_event_context(operation, event)
        envelope = event.to_dict()
        semantic = {key: value for key, value in envelope.items() if key != "event_uid"}
        semantic_key = digest_value(semantic)
        envelope_digest = digest_value(envelope)
        envelope_json = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload_json = json.dumps(event.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute(
                    "SELECT * FROM events WHERE event_uid=?",
                    (event.event_uid,),
                ).fetchone()
                if existing is not None:
                    if existing["operation_uid"] != operation_uid or existing["envelope_digest"] != envelope_digest:
                        raise IdentityStoreError("EVENT_CONFLICT")
                    db.commit()
                    return existing["event_uid"]
                existing = db.execute(
                    "SELECT * FROM events WHERE operation_uid=? AND semantic_key=?",
                    (operation_uid, semantic_key),
                ).fetchone()
                if existing is not None:
                    db.commit()
                    return existing["event_uid"]
                db.execute(
                    "INSERT INTO events("
                    "event_uid,operation_uid,kind,project_uid,work_uid,task_uid,"
                    "dispatch_uid,attempt_uid,contract_uid,contract_version,"
                    "contract_digest,controller_generation,cancel_generation,"
                    "payload_json,envelope_json,semantic_key,envelope_digest,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event.event_uid, operation_uid, event.kind, event.project_uid,
                        event.work_uid, event.task_uid, event.dispatch_uid,
                        event.attempt_uid, event.contract_uid, event.contract_version,
                        event.contract_digest, event.controller_generation,
                        event.cancel_generation, payload_json, envelope_json,
                        semantic_key, envelope_digest, _now(),
                    ),
                )
                db.commit()
                return event.event_uid
            except Exception:
                db.rollback()
                raise

    def event_projection(self, state_path):
        """Validate committed parent rows and full envelopes before projecting."""
        relative_path = self._normalize_operation_path(state_path)
        operation_columns = sorted(REQUIRED_COLUMNS["operations"])
        operation_select = ",".join("o." + key + " AS op__" + key for key in operation_columns)
        with closing(self._connect()) as db:
            db.execute("PRAGMA query_only=ON")
            rows = db.execute(
                "SELECT e.*," + operation_select + " FROM events e JOIN operations o "
                "ON o.operation_uid=e.operation_uid "
                "WHERE o.state_path=? ORDER BY e.created_at,e.rowid",
                (relative_path,),
            ).fetchall()
        result = []
        for row in rows:
            operation = {key: row["op__" + key] for key in operation_columns}
            self._validate_operation_row(operation)
            if operation["phase"] != "committed":
                raise IdentityStoreError("EVENT_PROJECTION_INVALID")
            try:
                envelope = json.loads(row["envelope_json"])
                event = EventEnvelope.from_dict(envelope)
                self._validate_event(event)
                self._check_event_context(operation, event)
                payload = json.loads(row["payload_json"])
                actual = {key: row[key] for key in envelope if key != "payload"}
                actual["payload"] = payload
                canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                semantic = {key: value for key, value in envelope.items() if key != "event_uid"}
                if (
                    not isinstance(payload, dict)
                    or canonical(envelope) != canonical(actual)
                    or canonical(envelope) != row["envelope_json"]
                    or canonical(payload) != row["payload_json"]
                    or digest_value(envelope) != row["envelope_digest"]
                    or digest_value(semantic) != row["semantic_key"]
                    or not isinstance(row["created_at"], str)
                    or not row["created_at"].strip()
                ):
                    raise IdentityStoreError("EVENT_PROJECTION_INVALID")
            except (IdentityStoreError, KeyError, TypeError, ValueError) as exc:
                raise IdentityStoreError("EVENT_PROJECTION_INVALID") from exc
            result.append({
                **event.to_dict(),
                "created_at": row["created_at"],
                "state_path": operation["state_path"],
            })
        return result

    def event_exists(self, event_uid):
        with closing(self._connect()) as db:
            return bool(db.execute("SELECT 1 FROM events WHERE event_uid=?", (event_uid,)).fetchone())

    def event_count(self):
        with closing(self._connect()) as db:
            return int(db.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def record_session_binding(self, binding_uid, canonical_digest):
        """Persist immutable identity/capability facts for one binding."""
        if not isinstance(binding_uid, str) or not binding_uid:
            raise IdentityStoreError("SESSION_BINDING_UID_REQUIRED")
        if not isinstance(canonical_digest, str) or not canonical_digest:
            raise IdentityStoreError("SESSION_BINDING_INVALID")
        key = "session_binding:" + binding_uid
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT value FROM metadata WHERE key=?", (key,)
            ).fetchone()
            if row is not None:
                if row["value"] != canonical_digest:
                    raise IdentityStoreError("SESSION_BINDING_IMMUTABLE")
                return
            db.execute(
                "INSERT INTO metadata(key, value) VALUES(?, ?)",
                (key, canonical_digest),
            )
            db.commit()

    def read_session_binding(self, binding_uid):
        """Return the immutable binding digest previously recorded, or None."""
        if not isinstance(binding_uid, str) or not binding_uid:
            raise IdentityStoreError("SESSION_BINDING_UID_REQUIRED")
        key = "session_binding:" + binding_uid
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT value FROM metadata WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row is not None else None

    @staticmethod
    def _validate_binding_state(state):
        if not isinstance(state, dict):
            raise IdentityStoreError("SESSION_BINDING_STATE_INVALID")
        controller = state.get("controller_generation")
        cancel = state.get("cancel_generation")
        cancelled = state.get("cancelled")
        if (
            not isinstance(controller, int) or controller < 1
            or not isinstance(cancel, int) or cancel < 1
            or not isinstance(cancelled, bool)
        ):
            raise IdentityStoreError("SESSION_BINDING_STATE_INVALID")
        return {
            "controller_generation": controller,
            "cancel_generation": cancel,
            "cancelled": cancelled,
        }

    def record_session_state(
        self, binding_uid, controller_generation, cancel_generation, cancelled,
    ):
        """Create the mutable authoritative state for a new binding."""
        if not isinstance(binding_uid, str) or not binding_uid:
            raise IdentityStoreError("SESSION_BINDING_UID_REQUIRED")
        state = self._validate_binding_state({
            "controller_generation": controller_generation,
            "cancel_generation": cancel_generation,
            "cancelled": cancelled,
        })
        key = "session_binding_state:" + binding_uid
        encoded = json.dumps(state, sort_keys=True, separators=(",", ":"))
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT value FROM metadata WHERE key=?", (key,)
            ).fetchone()
            if row is not None:
                if row["value"] != encoded:
                    raise IdentityStoreError("SESSION_BINDING_STATE_IMMUTABLE")
                return state
            db.execute(
                "INSERT INTO metadata(key, value) VALUES(?, ?)",
                (key, encoded),
            )
            db.commit()
        return state

    def read_session_state(self, binding_uid):
        """Return mutable authoritative binding state, or None."""
        if not isinstance(binding_uid, str) or not binding_uid:
            raise IdentityStoreError("SESSION_BINDING_UID_REQUIRED")
        key = "session_binding_state:" + binding_uid
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT value FROM metadata WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        try:
            return self._validate_binding_state(json.loads(row["value"]))
        except (TypeError, ValueError) as exc:
            raise IdentityStoreError("SESSION_BINDING_STATE_INVALID") from exc

    def record_native_model_observation(self, request_uid, observation):
        """Persist immutable, request-keyed native model observation facts."""
        if not isinstance(request_uid, str) or not request_uid:
            raise IdentityStoreError("REQUEST_UID_REQUIRED")
        observation = self._validate_native_model_observation(observation)
        encoded = json.dumps(
            observation, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        )
        key = "native_model_observation:" + request_uid
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT value FROM metadata WHERE key=?", (key,),
            ).fetchone()
            if row is not None:
                try:
                    stored = json.loads(row["value"])
                except (TypeError, ValueError) as exc:
                    raise IdentityStoreError(
                        "NATIVE_MODEL_OBSERVATION_INVALID"
                    ) from exc
                self._validate_native_model_observation(stored)
                if row["value"] != encoded:
                    raise IdentityStoreError("NATIVE_MODEL_OBSERVATION_CHANGED")
                return stored
            db.execute(
                "INSERT INTO metadata(key, value) VALUES(?, ?)",
                (key, encoded),
            )
            db.commit()
        return json.loads(encoded)

    def read_native_model_observation(self, request_uid):
        if not isinstance(request_uid, str) or not request_uid:
            raise IdentityStoreError("REQUEST_UID_REQUIRED")
        key = "native_model_observation:" + request_uid
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT value FROM metadata WHERE key=?", (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError) as exc:
            raise IdentityStoreError("NATIVE_MODEL_OBSERVATION_INVALID") from exc
        return self._validate_native_model_observation(value)

    @staticmethod
    def _validate_native_model_observation(value):
        expected = {
            "requested_model", "requested_status",
            "host_selected_route", "host_selected_status",
            "provider_backend", "provider_backend_status",
            "effort", "native_turn_id", "observed_at",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise IdentityStoreError("NATIVE_MODEL_OBSERVATION_INVALID")
        statuses = {"actual", "unsupported", "unknown"}
        for key in (
            "requested_status", "host_selected_status",
            "provider_backend_status",
        ):
            if value.get(key) not in statuses:
                raise IdentityStoreError("NATIVE_MODEL_OBSERVATION_INVALID")
        for key in (
            "requested_model", "host_selected_route", "provider_backend",
            "effort", "native_turn_id",
        ):
            item = value.get(key)
            if item is not None and (
                not isinstance(item, str) or not item.strip()
            ):
                raise IdentityStoreError("NATIVE_MODEL_OBSERVATION_INVALID")
        observed_at = value.get("observed_at")
        if not isinstance(observed_at, str) or not observed_at.strip():
            raise IdentityStoreError("NATIVE_MODEL_OBSERVATION_INVALID")
        return dict(value)

    @staticmethod
    def _work_controller(value, work_uid):
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "work_uid", "run_id", "binding_uid"}
            or type(value.get("schema")) is not int or value["schema"] != 1
            or value.get("work_uid") != work_uid
            or not isinstance(value.get("binding_uid"), str) or not value["binding_uid"]
            or not isinstance(value.get("run_id"), str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value["run_id"]) is None
        ):
            raise IdentityStoreError("CONTROLLER_RECORD_INVALID")
        return dict(value)

    def read_work_controller(self, work_uid):
        """Read the work's sole control lease; never create or repair it."""
        if not isinstance(work_uid, str) or not work_uid:
            raise IdentityStoreError("WORK_UID_REQUIRED")
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT value FROM metadata WHERE key=?", ("work_controller:" + work_uid,),
            ).fetchone()
            if row is None:
                return None
            try:
                value = json.loads(row["value"])
            except (TypeError, ValueError) as exc:
                raise IdentityStoreError("CONTROLLER_RECORD_INVALID") from exc
            return self._work_controller(value, work_uid)

    def claim_initial_work_controller(self, work_uid, run_id, binding_uid):
        """Reserve one controller atomically; later bindings remain candidates."""
        candidate = self._work_controller({
            "schema": 1, "work_uid": work_uid, "run_id": run_id,
            "binding_uid": binding_uid,
        }, work_uid)
        work = self.resolve("work", work_uid, None, write=True)
        if work.uid != work_uid:
            raise IdentityStoreError("WORK_UID_REQUIRED")
        key = "work_controller:" + work_uid
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                binding = db.execute(
                    "SELECT value FROM metadata WHERE key=?",
                    ("session_binding:" + binding_uid,),
                ).fetchone()
                state = db.execute(
                    "SELECT value FROM metadata WHERE key=?",
                    ("session_binding_state:" + binding_uid,),
                ).fetchone()
                if binding is None or state is None:
                    raise IdentityStoreError("SESSION_CONTEXT_INVALID")
                if self._validate_binding_state(json.loads(state["value"]))["cancelled"]:
                    raise IdentityStoreError("SESSION_BINDING_STALE")
                row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
                if row is not None:
                    current = self._work_controller(json.loads(row["value"]), work_uid)
                    if current["run_id"] != run_id:
                        raise IdentityStoreError("WORK_UID_MISMATCH")
                    db.commit()
                    return current
                db.execute("INSERT INTO metadata(key,value) VALUES(?,?)", (
                    key, json.dumps(candidate, sort_keys=True, separators=(",", ":")),
                ))
                db.commit()
                return candidate
            except Exception:
                db.rollback()
                raise

    def record_controller_pending(self, request_uid, payload):
        """Persist an immutable first-controller intent before its effects."""
        if not isinstance(request_uid, str) or not request_uid:
            raise IdentityStoreError("REQUEST_UID_REQUIRED")
        if not isinstance(payload, dict):
            raise IdentityStoreError("CONTROLLER_PENDING_INVALID")
        encoded = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        )
        key = "controller_pending:" + request_uid
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT value FROM metadata WHERE key=?", (key,),
            ).fetchone()
            if row is not None:
                if row["value"] != encoded:
                    raise IdentityStoreError("CONTROLLER_PENDING_CHANGED")
                return json.loads(row["value"])
            db.execute(
                "INSERT INTO metadata(key, value) VALUES(?, ?)",
                (key, encoded),
            )
            db.commit()
        return json.loads(encoded)

    def read_controller_pending(self, request_uid):
        if not isinstance(request_uid, str) or not request_uid:
            raise IdentityStoreError("REQUEST_UID_REQUIRED")
        key = "controller_pending:" + request_uid
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT value FROM metadata WHERE key=?", (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError) as exc:
            raise IdentityStoreError("CONTROLLER_PENDING_INVALID") from exc
        if not isinstance(value, dict):
            raise IdentityStoreError("CONTROLLER_PENDING_INVALID")
        return value

    def transition_controller_bindings(
        self, old_uid, expected_old_generation,
        new_uid, expected_new_generation, next_generation, *, work_uid=None, run_id=None,
    ):
        """CAS-invalidate the old controller and activate the new generation.

        The two state rows and both generation changes commit in one SQLite
        transaction.  A retry with the already committed values is idempotent;
        a stale expectation fails closed.
        """
        for value in (
            expected_old_generation, expected_new_generation, next_generation,
        ):
            if type(value) is not int or value < 1:
                raise IdentityStoreError("CONTROL_VERSION_INVALID")
        if not isinstance(old_uid, str) or not old_uid:
            raise IdentityStoreError("SESSION_BINDING_UID_REQUIRED")
        if not isinstance(new_uid, str) or not new_uid:
            raise IdentityStoreError("SESSION_BINDING_UID_REQUIRED")
        if old_uid == new_uid:
            raise IdentityStoreError("SESSION_BINDING_CONFLICT")
        if next_generation <= max(expected_old_generation, expected_new_generation):
            raise IdentityStoreError("CONTROL_VERSION_CHANGED")
        next_controller = None
        if work_uid is not None or run_id is not None:
            next_controller = self._work_controller({
                "schema": 1, "work_uid": work_uid, "run_id": run_id,
                "binding_uid": new_uid,
            }, work_uid)
            if self.resolve("work", work_uid, None, write=True).uid != work_uid:
                raise IdentityStoreError("WORK_UID_REQUIRED")
        old_key = "session_binding_state:" + old_uid
        new_key = "session_binding_state:" + new_uid
        marker_key = (
            "session_binding_transition:"
            + old_uid
            + ":"
            + new_uid
            + ":"
            + str(next_generation)
        )
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                rows = {
                    row["key"]: row["value"]
                    for row in db.execute(
                        "SELECT key,value FROM metadata WHERE key IN (?,?,?)",
                        (old_key, new_key, marker_key),
                    )
                }
                if old_key not in rows or new_key not in rows:
                    raise IdentityStoreError("SESSION_BINDING_STATE_MISSING")
                controller_key = "work_controller:" + work_uid if next_controller else None
                controller = None
                if controller_key is not None:
                    row = db.execute(
                        "SELECT value FROM metadata WHERE key=?", (controller_key,),
                    ).fetchone()
                    if row is not None:
                        controller = self._work_controller(json.loads(row["value"]), work_uid)
                        if controller["run_id"] != run_id:
                            raise IdentityStoreError("WORK_UID_MISMATCH")
                try:
                    old_state = self._validate_binding_state(json.loads(rows[old_key]))
                    new_state = self._validate_binding_state(json.loads(rows[new_key]))
                except (TypeError, ValueError) as exc:
                    raise IdentityStoreError("SESSION_BINDING_STATE_INVALID") from exc
                if marker_key in rows:
                    try:
                        marker = json.loads(rows[marker_key])
                        committed = {
                            "old": self._validate_binding_state(marker["old"]),
                            "new": self._validate_binding_state(marker["new"]),
                        }
                    except (KeyError, TypeError, ValueError) as exc:
                        raise IdentityStoreError(
                            "SESSION_BINDING_STATE_INVALID"
                        ) from exc
                    if (
                        old_state != committed["old"]
                        or new_state != committed["new"]
                        or marker.get("expected_old_generation")
                        != expected_old_generation
                        or marker.get("expected_new_generation")
                        != expected_new_generation
                        or marker.get("next_generation") != next_generation
                        or committed["old"]["controller_generation"]
                        != expected_old_generation
                        or committed["new"]["controller_generation"]
                        != next_generation
                        or marker.get("work_uid") != work_uid
                        or marker.get("run_id") != run_id
                        or (next_controller is not None and controller != next_controller)
                    ):
                        raise IdentityStoreError("CONTROL_VERSION_CHANGED")
                    db.commit()
                    return committed
                desired_old = {
                    "controller_generation": expected_old_generation,
                    "cancel_generation": old_state["cancel_generation"] + 1,
                    "cancelled": True,
                }
                desired_new = {
                    "controller_generation": next_generation,
                    "cancel_generation": new_state["cancel_generation"],
                    "cancelled": False,
                }
                if (
                    old_state["controller_generation"] != expected_old_generation
                    or new_state["controller_generation"] != expected_new_generation
                ):
                    raise IdentityStoreError("CONTROL_VERSION_CHANGED")
                if old_state["cancelled"]:
                    raise IdentityStoreError("SESSION_BINDING_STALE")
                if new_state["cancelled"]:
                    raise IdentityStoreError("SESSION_BINDING_STALE")
                if controller is not None and controller["binding_uid"] != old_uid:
                    raise IdentityStoreError("CONTROLLER_LEASE_REQUIRED")
                db.execute(
                    "UPDATE metadata SET value=? WHERE key=?",
                    (json.dumps(desired_old, sort_keys=True, separators=(",", ":")), old_key),
                )
                db.execute(
                    "UPDATE metadata SET value=? WHERE key=?",
                    (json.dumps(desired_new, sort_keys=True, separators=(",", ":")), new_key),
                )
                if next_controller is not None:
                    db.execute(
                        "INSERT INTO metadata(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (controller_key, json.dumps(next_controller, sort_keys=True, separators=(",", ":"))),
                    )
                db.execute(
                    "INSERT INTO metadata(key, value) VALUES(?, ?)",
                    (
                        marker_key,
                        json.dumps(
                            {
                                "expected_old_generation": expected_old_generation,
                                "expected_new_generation": expected_new_generation,
                                "next_generation": next_generation,
                                "work_uid": work_uid,
                                "run_id": run_id,
                                "old": desired_old,
                                "new": desired_new,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
                db.commit()
                return {"old": desired_old, "new": desired_new}
            except Exception:
                db.rollback()
                raise
