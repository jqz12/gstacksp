"""Read-only migration preview contract for legacy GSP state."""
from __future__ import annotations

import dataclasses
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from gsp.hosts import identity
from gsp.identity_store import IdentityStore
from gsp.orchestration_migration import preview_migration, verify_source
from gsp.runtime import WorkflowError, locked


PROJECT_UID = "0123456789abcdef0123456789abcdef"


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def write_json(path: Path, value: dict) -> None:
    write_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        raise AssertionError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stdout}\n{result.stderr}"
        )
    return result.stdout.strip()


class MigrationFixture:
    def __init__(self, base: Path):
        self.base = base
        self.root = base / "project"
        self.root.mkdir(parents=True)
        self.work_root = self.root / "docs" / "work" / "2026" / "11"
        self.run_root = self.root / ".gsp" / "runs"
        self.work_root.mkdir(parents=True)
        self.run_root.mkdir(parents=True)
        self.work_rows = []
        write_json(self.root / ".gsp" / "work-counter.json", {"next": 1})
        write_bytes(
            self.root / "TODOS.md",
            b"# TODOS\n\n<!-- gsp-backlog:v2 -->\n\n## project\n\n## Completed\n",
        )

    def add_work(self, number, uid, *, run_id=None, stage="implementing", folder=None):
        base_directory = self.work_root / f"{number}-{number.lower()}"
        directory = base_directory
        suffix = 2
        while directory.exists():
            directory = base_directory.with_name(f"{base_directory.name}-{suffix}")
            suffix += 1
        directory.mkdir()
        record = {
            "schema": 1,
            "kind": "work",
            "uuid": uid,
            "number": number,
            "title": f"Fixture {number}",
            "source": "migration fixture",
            "stage": stage,
            "folder": folder if folder is not None else directory.relative_to(self.root).as_posix(),
            "documents": {},
            "modules": [],
            "relations": [],
            "run_id": run_id,
            "generation": 1,
        }
        path = directory / "record.json"
        write_json(path, record)
        self.work_rows.append((path, record))
        self._update_counter(number)
        return path, record

    def add_run(
        self,
        run_id,
        *,
        number=None,
        work_uid=None,
        root=None,
        project_uid=PROJECT_UID,
        topology=None,
        tasks=None,
        directory_name=None,
    ):
        directory = self.run_root / (directory_name or run_id)
        directory.mkdir()
        state = {
            "schema": 1,
            "id": run_id,
            "number": number,
            "title": f"Fixture run {run_id}",
            "root": root or str(self.root),
            "project_id": project_uid,
            "status": "ready",
            "work_uuid": work_uid,
            "tasks": tasks or [],
        }
        if topology is not None:
            state["git_topology"] = topology
        path = directory / "state.json"
        write_json(path, state)
        return path, state

    def add_pair(self, ordinal, *, run_id=None, topology=None, tasks=None):
        number = f"GSP-{ordinal:06d}"
        uid = f"{ordinal:032x}"
        actual_run = run_id or f"run-{ordinal}"
        _, record = self.add_work(number, uid, run_id=actual_run)
        _, state = self.add_run(
            actual_run,
            number=number,
            work_uid=uid,
            topology=topology,
            tasks=tasks,
        )
        return record, state

    def add_todo(self, identity, *, completed=False):
        path = self.root / "TODOS.md"
        text = path.read_text(encoding="utf-8")
        block = (
            f"<!-- gsp-todo:{identity} -->\n"
            f"### {identity} — Fixture item\n\n"
            "**What:** Preserve the legacy identity.\n\n"
            "**Why:** Migration must not collapse object kinds.\n\n"
            "**Context:** Synthetic migration fixture.\n\n"
            "**Priority:** P2\n\n"
            "**Effort:** S\n\n"
            f"<!-- /gsp-todo:{identity} -->\n\n"
        )
        if completed:
            text = text.rstrip() + "\n\n## Completed\n\n" + block
        else:
            text = text.replace("## project\n\n", "## project\n\n" + block, 1)
        write_bytes(path, text.encode("utf-8"))
        records_path = self.root / ".gsp" / "backlog" / "records.json"
        if records_path.is_file():
            records = json.loads(records_path.read_text(encoding="utf-8"))
        else:
            records = {"schema": 2, "items": {}, "document": {}}
        records["items"][identity] = {
            "source": f"manual:TODOS.md#{identity}",
            "origin": "",
            "blocking": "no",
            "evidence": "",
            "resolution": "",
            "history": "",
            "links": [],
            "component": "Completed" if completed else "project",
            "object_uid": "",
            "completed_hash": None,
        }
        records["document"] = {"path": path.name, "sha256": sha256(text.encode("utf-8"))}
        write_json(records_path, records)

    def create_index(self, *, stale_uuid=None, todo_text="ready"):
        path = self.root / ".gsp" / "documents.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute(
                "CREATE TABLE entries (uuid TEXT PRIMARY KEY, kind TEXT, "
                "number TEXT, title TEXT, stage TEXT, path TEXT UNIQUE, "
                "hash TEXT, data TEXT)"
            )
            db.execute("CREATE TABLE texts (path TEXT PRIMARY KEY, hash TEXT, body TEXT)")
            for record_path, record in self.work_rows:
                file_hash = sha256(record_path.read_bytes())
                if record["uuid"] == stale_uuid:
                    file_hash = "0" * 64
                db.execute(
                    "INSERT OR IGNORE INTO entries VALUES (?,?,?,?,?,?,?,?)",
                    (
                        record["uuid"],
                        record["kind"],
                        record["number"],
                        record["title"],
                        record["stage"],
                        record_path.relative_to(self.root).as_posix(),
                        file_hash,
                        json.dumps(record, ensure_ascii=False, sort_keys=True),
                    ),
                )
            todos = self.root / "TODOS.md"
            if todos.is_file() and todo_text != "missing":
                body = todos.read_text(encoding="utf-8-sig")
                raw = todos.read_bytes()
                rel = "TODO.md" if todo_text == "wrong-path" else "TODOS.md"
                text_hash = "0" * 64 if todo_text == "stale" else sha256(raw)
                db.execute(
                    "INSERT OR REPLACE INTO texts VALUES (?,?,?)",
                    (rel, text_hash, body),
                )
            db.commit()

    def initialize_authority(self):
        write_json(self.root / ".gsp" / "project.json", {
            "id": PROJECT_UID,
            "root": str(self.root),
        })
        moved = []
        for relative in ("TODOS.md", ".gsp/work-counter.json", "docs/work",
                         ".gsp/runs", ".gsp/backlog"):
            path = self.root / relative
            if not path.exists():
                continue
            backup = path.with_name(path.name + ".authority-init-backup")
            path.rename(backup)
            moved.append((path, backup))
        try:
            return IdentityStore(self.root).initialize()
        finally:
            for path, backup in reversed(moved):
                backup.rename(path)

    def initialize_git(self):
        run_git(self.root, "init", "-b", "main")
        run_git(self.root, "config", "user.name", "Migration Fixture")
        run_git(self.root, "config", "user.email", "migration@example.invalid")
        write_bytes(self.root / "base.txt", b"base\n")
        run_git(self.root, "add", "base.txt")
        run_git(self.root, "commit", "-m", "base")

    def add_worktree(self, name, branch):
        path = self.root / ".gsp" / "worktrees" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        run_git(self.root, "worktree", "add", "-b", branch, str(path))
        return path

    def _update_counter(self, number):
        number_value = int(number.split("-")[1])
        write_json(self.root / ".gsp" / "work-counter.json", {"next": number_value + 1})


def source_snapshot(root: Path):
    paths = [
        root / "TODOS.md",
        root / ".gsp" / "project.json",
        root / ".gsp" / "work-counter.json",
        root / ".gsp" / "documents.sqlite",
        root / ".gsp" / "documents.sqlite-wal",
        root / ".gsp" / "documents.sqlite-shm",
        root / ".gsp" / "writer.lock",
        root / ".gsp" / "driver.lock",
        root / ".gsp" / "orchestration" / "registry.sqlite",
        root / ".gsp" / "orchestration" / "registry.sqlite-wal",
        root / ".gsp" / "orchestration" / "registry.sqlite-shm",
    ]
    paths.extend(sorted((root / "docs" / "work").glob("**/record.json")))
    paths.extend(sorted((root / ".gsp" / "runs").glob("*/state.json")))
    paths.extend(sorted((root / ".gsp" / "runs").glob("*/driver/state.json")))
    paths.extend(sorted((root / ".gsp" / "runs").glob("*/evidence/*/command-progress.json")))
    paths.extend(sorted((root / ".gsp" / "runs").glob("*/sessions/index.json")))
    paths.extend(sorted((root / ".gsp" / "runs").glob("*/sessions/dispatch.json")))
    return {
        path.relative_to(root).as_posix(): sha256(path.read_bytes())
        for path in paths
        if path.is_file()
    }


def exclusive_reader_guard(path: Path):
    if os.name != "nt":
        raise unittest.SkipTest("Windows exclusive handle is required")
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    handle = create_file(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())

    class Guard:
        def close(self):
            if handle:
                close_handle(handle)

    return Guard()

class MigrationPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gsp-migration-preview-")
        self.addCleanup(self.temp.cleanup)
        self.fixture = MigrationFixture(Path(self.temp.name))

    def test_preview_is_byte_readonly_and_does_not_initialize_authority(self):
        self.fixture.add_pair(1)
        self.fixture.add_todo("GSP-000001")
        self.fixture.create_index()
        before = source_snapshot(self.fixture.root)
        before_paths = {p.relative_to(self.fixture.root).as_posix() for p in self.fixture.root.rglob("*") if p.is_file()}
        manifest = preview_migration(self.fixture.root)
        self.assertEqual(before, source_snapshot(self.fixture.root))
        after_paths = {p.relative_to(self.fixture.root).as_posix() for p in self.fixture.root.rglob("*") if p.is_file()}
        self.assertEqual(before_paths, after_paths)
        self.assertFalse((self.fixture.root / ".gsp" / "orchestration" / "registry.sqlite").exists())
        self.assertFalse((self.fixture.root / ".gsp" / "document-index-context.json").exists())
        self.assertIsNone(manifest.allocator_incarnation)

    def test_unreadable_source_is_a_blocker_without_second_read_or_source_change(self):
        self.fixture.add_pair(37)
        record_path = self.fixture.work_rows[-1][0]
        self.fixture.create_index()
        original = record_path.read_bytes()
        guard = exclusive_reader_guard(record_path)
        try:
            try:
                manifest = preview_migration(self.fixture.root)
            except OSError as exc:
                self.fail("preview leaked a second source read: " + str(exc))
        finally:
            guard.close()
        self.assertEqual(record_path.read_bytes(), original)
        self.assertIn("SOURCE_READ_FAILED", {row["code"] for row in manifest.blockers})
        capture = manifest.backup_policy["source_capture"]
        unavailable = {row["path"] for row in capture["unavailable"]}
        self.assertIn(record_path.relative_to(self.fixture.root).as_posix(), unavailable)

    def test_real_authority_is_snapshotted_without_mutating_closed_wal_source(self):
        authority = self.fixture.initialize_authority()
        self.fixture.create_index()
        before = source_snapshot(self.fixture.root)
        manifest = preview_migration(self.fixture.root)
        self.assertEqual(before, source_snapshot(self.fixture.root))
        state = manifest.backup_policy["authority"]
        self.assertEqual(state["state"], "ready")
        self.assertEqual(state["project_uid"], PROJECT_UID)
        self.assertEqual(state["schema"], authority["schema"])
        self.assertEqual(state["incarnation"], authority["incarnation"])
        self.assertNotIn("AUTHORITY_INVALID", {row["code"] for row in manifest.blockers})

    def test_retained_authority_writer_blocks_without_source_mutation(self):
        self.fixture.initialize_authority()
        self.fixture.create_index()
        authority_path = self.fixture.root / ".gsp" / "orchestration" / "registry.sqlite"
        writer = sqlite3.connect(authority_path, timeout=5)
        self.addCleanup(writer.close)
        writer.execute("BEGIN")
        before = source_snapshot(self.fixture.root)
        manifest = preview_migration(self.fixture.root)
        self.assertEqual(before, source_snapshot(self.fixture.root))
        self.assertIn("AUTHORITY_WRITER_LIVE", {row["code"] for row in manifest.blockers})
        self.assertEqual(manifest.backup_policy["authority"]["state"], "writer-live")

    def test_retained_document_index_writer_blocks_without_source_mutation(self):
        self.fixture.add_pair(39)
        self.fixture.create_index()
        index_path = self.fixture.root / ".gsp" / "documents.sqlite"
        writer = sqlite3.connect(index_path, timeout=5)
        self.addCleanup(writer.close)
        writer.execute("BEGIN")
        before = source_snapshot(self.fixture.root)
        manifest = preview_migration(self.fixture.root)
        self.assertEqual(before, source_snapshot(self.fixture.root))
        self.assertIn(
            "DOCUMENT_INDEX_WRITER_LIVE",
            {row["code"] for row in manifest.blockers},
        )

    def test_authority_requires_full_schema_attestation_not_metadata_only(self):
        write_json(self.fixture.root / ".gsp" / "project.json", {
            "id": PROJECT_UID,
            "root": str(self.fixture.root),
        })
        authority = self.fixture.root / ".gsp" / "orchestration" / "registry.sqlite"
        authority.parent.mkdir(parents=True)
        with closing(sqlite3.connect(authority)) as db:
            db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
            db.executemany(
                "INSERT INTO metadata VALUES (?,?)",
                (
                    ("schema_version", "5"),
                    ("project_uid", PROJECT_UID),
                    ("incarnation", "a" * 32),
                ),
            )
            db.commit()
        self.fixture.create_index()
        manifest = preview_migration(self.fixture.root)
        self.assertIn("AUTHORITY_INVALID", {row["code"] for row in manifest.blockers})
        self.assertEqual(manifest.backup_policy["authority"]["state"], "invalid")

    def test_authority_project_provenance_mismatch_blocks(self):
        self.fixture.initialize_authority()
        self.fixture.create_index()
        project = self.fixture.root / ".gsp" / "project.json"
        write_json(project, {"id": PROJECT_UID, "root": str(self.fixture.base / "elsewhere")})
        manifest = preview_migration(self.fixture.root)
        self.assertIn("PROJECT_IDENTITY_INVALID", {row["code"] for row in manifest.blockers})

    def test_all_work_runs_todos_dirs_classified_exactly_once(self):
        self.fixture.add_pair(1)
        self.fixture.add_work("GSP-000002", f"{2:032x}", run_id=None, stage="idea")
        self.fixture.add_run("legacy-run")
        orphan = self.fixture.work_root / "GSP-000003-orphan"
        orphan.mkdir()
        write_bytes(orphan / "notes.md", b"orphan\n")
        self.fixture.add_todo("GSP-000001")
        self.fixture.add_todo("TODO-000001")
        self.fixture.create_index()
        manifest = preview_migration(self.fixture.root)
        rows = manifest.classification_manifest
        keys = [(row["domain"], row["source_path"], row["identity"]) for row in rows]
        self.assertEqual(len(keys), len(set(keys)))
        work_rows = [row for row in rows if row["domain"] == "work_record"]
        run_rows = [row for row in rows if row["domain"] == "run_state"]
        directory_rows = [row for row in rows if row["domain"] == "task_directory"]
        todo_rows = [row for row in rows if row["domain"] == "todo_marker"]
        self.assertEqual(len(work_rows), 2)
        self.assertEqual(len(run_rows), 2)
        self.assertEqual(len(directory_rows), 3)
        self.assertEqual(len(todo_rows), 2)
        self.assertEqual({row["classification"] for row in work_rows}, {"matched_run", "work_without_run"})
        self.assertEqual({row["classification"] for row in run_rows}, {"matched_work", "legacy_only_run"})
        self.assertIn("orphan_directory", {row["classification"] for row in directory_rows})
        self.assertEqual(manifest.source_counts["work_record"], 2)
        self.assertEqual(manifest.source_counts["run_state"], 2)
        self.assertEqual(manifest.source_counts["task_directory"], 3)
        self.assertEqual(manifest.source_counts["todo_marker"], 2)
        self.assertEqual(
            manifest.source_counts["work_record"],
            manifest.source_counts["work_matched_run"]
            + manifest.source_counts["work_without_run"]
            + manifest.source_counts["work_blocked"],
        )
        self.assertEqual(
            manifest.source_counts["run_state"],
            manifest.source_counts["run_matched_work"]
            + manifest.source_counts["run_legacy_only"]
            + manifest.source_counts["run_blocked"],
        )

    def test_work_highwater_includes_legacy_runs_but_not_todo_numbers(self):
        self.fixture.add_work("GSP-000010", f"{10:032x}", run_id=None)
        self.fixture.add_run("legacy-high-water", number="GSP-000025")
        self.fixture.add_todo("TODO-009999")
        self.fixture.create_index()
        manifest = preview_migration(self.fixture.root)
        self.assertIn("WORK_COUNTER_COLLISION", {row["code"] for row in manifest.blockers})
        counter = manifest.backup_policy["counter"]
        self.assertEqual(counter["highest"], 25)
        self.assertEqual(counter.get("effective_next"), 26)
        self.assertIn(
            "legacy-high-water",
            (counter.get("sources") or {}).get("run_number", []),
        )
        self.assertEqual(counter.get("todo_highest"), 9999)
        self.assertEqual(
            (counter.get("todo_sources") or {}).get("todo_marker"),
            ["TODO-009999"],
        )

    def test_duplicate_or_ambiguous_source_is_blocker(self):
        duplicate = f"{7:032x}"
        self.fixture.add_work("GSP-000007", duplicate, run_id="run-a")
        self.fixture.add_work("GSP-000007", duplicate, run_id="run-b")
        self.fixture.add_run("run-a", number="GSP-000007", work_uid=duplicate)
        self.fixture.add_run("run-b", number="GSP-000007", work_uid=duplicate)
        self.fixture.create_index()
        manifest = preview_migration(self.fixture.root)
        blockers = {row["code"] for row in manifest.blockers}
        self.assertIn("DUPLICATE_WORK_UUID", blockers)
        self.assertIn("DUPLICATE_WORK_NUMBER", blockers)
        self.assertIn("DUPLICATE_RUN_NUMBER", blockers)
        self.assertFalse(any(
            row["classification"] == "matched_run"
            for row in manifest.classification_manifest
            if row["domain"] == "work_record"
        ))

    def test_ambiguous_work_run_link_is_blocker(self):
        self.fixture.add_work("GSP-000020", f"{20:032x}", run_id="shared-run")
        self.fixture.add_work("GSP-000021", f"{21:032x}", run_id="shared-run")
        self.fixture.add_run(
            "shared-run", number="GSP-000020", work_uid=f"{20:032x}",
        )
        self.fixture.create_index()
        manifest = preview_migration(self.fixture.root)
        self.assertIn("WORK_RUN_AMBIGUOUS", {row["code"] for row in manifest.blockers})
        work_classifications = {
            row["classification"] for row in manifest.classification_manifest
            if row["domain"] == "work_record"
        }
        self.assertEqual(work_classifications, {"blocked"})
    def test_todo_work_run_mapping_keeps_object_kind(self):
        self.fixture.add_pair(7)
        self.fixture.add_todo("GSP-0007")
        self.fixture.add_todo("TODO-000007")
        self.fixture.create_index()
        manifest = preview_migration(self.fixture.root)
        aliases = {row["identity"]: row for row in manifest.aliases}
        self.assertEqual(aliases["GSP-0007"]["object_kind"], "todo")
        self.assertEqual(aliases["TODO-000007"]["object_kind"], "todo")
        self.assertTrue(all(
            row["object_kind"] == "todo"
            for row in manifest.aliases
            if row["identity"] in {"GSP-0007", "TODO-000007"}
        ))
        todo_identities = {row["identity"] for row in manifest.classification_manifest if row["domain"] == "todo_marker"}
        self.assertEqual(todo_identities, {"GSP-0007", "TODO-000007"})
        work_identities = {row["identity"] for row in manifest.classification_manifest if row["domain"] == "work_record"}
        self.assertIn("GSP-000007", work_identities)
        self.assertNotIn("GSP-0007", work_identities)

    def test_missing_workspace_is_not_disposed_or_recreated(self):
        missing = self.fixture.base / "already-missing"
        self.fixture.add_pair(8, topology={"workspace_root": str(missing), "branch": "feature/missing", "effective_mode": "worktree"})
        self.fixture.create_index()
        manifest = preview_migration(self.fixture.root)
        self.assertFalse(missing.exists())
        resource = next(row for row in manifest.resources if row["identity"].endswith("run-8"))
        self.assertEqual(resource["disposition"], "workspace_missing")
        self.assertFalse(resource["disposed"])
        self.assertFalse(resource["deletable"])

    def test_task_level_topology_is_preserved_in_resource_scan(self):
        missing = self.fixture.base / "task-worktree"
        self.fixture.add_pair(38, tasks=[{
            "id": "T01",
            "status": "running",
            "topology": {
                "workspace_root": str(missing),
                "branch": "feature/task-worktree",
                "effective_mode": "worktree",
            },
        }])
        self.fixture.create_index()
        manifest = preview_migration(self.fixture.root)
        resource = next(
            row for row in manifest.resources
            if row["identity"] == "run-38:T01"
        )
        self.assertEqual(resource["workspace_path"], str(missing))
        self.assertEqual(resource["disposition"], "workspace_missing")
        self.assertFalse(resource["deletable"])
        self.assertFalse(resource["disposed"])

    def test_dirty_and_unmerged_workspaces_are_retained(self):
        self.fixture.initialize_git()
        dirty = self.fixture.add_worktree("dirty", "feature/dirty")
        unmerged = self.fixture.add_worktree("unmerged", "feature/unmerged")
        merged = self.fixture.add_worktree("merged", "feature/merged")
        write_bytes(dirty / "dirty.txt", b"keep\n")
        write_bytes(unmerged / "later.txt", b"later\n")
        run_git(unmerged, "add", "later.txt")
        run_git(unmerged, "commit", "-m", "unmerged work")
        for ordinal, run_id, path, branch in (
            (9, "dirty-run", dirty, "feature/dirty"),
            (10, "unmerged-run", unmerged, "feature/unmerged"),
            (11, "merged-run", merged, "feature/merged"),
        ):
            self.fixture.add_pair(ordinal, run_id=run_id, topology={"workspace_root": str(path), "branch": branch, "effective_mode": "worktree"})
        self.fixture.create_index()
        manifest = preview_migration(self.fixture.root)
        resources = {row["identity"]: row for row in manifest.resources}
        dirty_row = next(row for key, row in resources.items() if key.endswith("dirty-run"))
        unmerged_row = next(row for key, row in resources.items() if key.endswith("unmerged-run"))
        merged_row = next(row for key, row in resources.items() if key.endswith("merged-run"))
        self.assertTrue(dirty_row["dirty"])
        self.assertEqual(dirty_row["disposition"], "retain_dirty")
        self.assertFalse(unmerged_row["merged"])
        self.assertEqual(unmerged_row["disposition"], "retain_unmerged")
        self.assertEqual(merged_row["disposition"], "eligible_after_proof")
        self.assertFalse(dirty_row["deletable"])
        self.assertFalse(unmerged_row["deletable"])
        self.assertFalse(merged_row["deletable"])

    def test_external_and_unknown_owners_are_retained(self):
        external = self.fixture.base / "external"
        external.mkdir()
        self.fixture.add_pair(12, run_id="external-run", topology={"workspace_root": str(external), "branch": "external/branch", "effective_mode": "bind"})
        self.fixture.create_index()
        manifest = preview_migration(self.fixture.root)
        resource = next(row for row in manifest.resources if row["identity"].endswith("external-run"))
        self.assertEqual(resource["disposition"], "external_release_only")
        self.assertFalse(resource["deletable"])

    def test_verify_source_detects_byte_or_manifest_drift(self):
        self.fixture.add_pair(13)
        self.fixture.add_todo("GSP-000013")
        self.fixture.create_index()
        manifest = preview_migration(self.fixture.root)
        verify_source(self.fixture.root, manifest)
        todos = self.fixture.root / "TODOS.md"
        original = todos.read_bytes()
        write_bytes(todos, original + b"\n")
        with self.assertRaisesRegex(WorkflowError, "MIGRATION_SOURCE_CHANGED"):
            verify_source(self.fixture.root, manifest)
        write_bytes(todos, original)
        tampered = dataclasses.replace(manifest, source_digest="0" * 64)
        with self.assertRaisesRegex(WorkflowError, "MIGRATION_MANIFEST_CHANGED|MIGRATION_SOURCE_CHANGED"):
            verify_source(self.fixture.root, tampered)

    def test_index_or_git_conflict_is_blocker(self):
        self.fixture.initialize_git()
        registered = self.fixture.add_worktree("registered", "feature/registered")
        foreign = self.fixture.base / "foreign"
        foreign.mkdir()
        run_git(foreign, "init", "-b", "main")
        run_git(foreign, "config", "user.name", "Foreign Fixture")
        run_git(foreign, "config", "user.email", "foreign@example.invalid")
        write_bytes(foreign / "foreign.txt", b"foreign\n")
        run_git(foreign, "add", "foreign.txt")
        run_git(foreign, "commit", "-m", "foreign")
        record, _ = self.fixture.add_pair(14, run_id="registered-run", topology={"workspace_root": str(registered), "branch": "feature/registered", "effective_mode": "worktree"})
        self.fixture.add_pair(15, run_id="foreign-run", topology={"workspace_root": str(foreign), "branch": "main", "effective_mode": "bind"})
        self.fixture.create_index(stale_uuid=record["uuid"])
        manifest = preview_migration(self.fixture.root)
        blockers = {row["code"] for row in manifest.blockers}
        self.assertIn("DOCUMENT_INDEX_STALE", blockers)
        self.assertIn("GIT_COMMON_DIR_MISMATCH", blockers)
        self.assertTrue(any(
            row["classification"] == "blocked"
            for row in manifest.classification_manifest
            if row["domain"] == "work_record"
        ))


    def test_unowned_git_worktree_is_unknown_and_retained(self):
        self.fixture.initialize_git()
        self.fixture.add_worktree("unowned", "feature/unowned")
        self.fixture.add_pair(16)
        self.fixture.create_index()
        manifest = preview_migration(self.fixture.root)
        self.assertIn("GIT_WORKTREE_UNOWNED", {row["code"] for row in manifest.blockers})
        resource = next(row for row in manifest.resources if row["identity"].startswith("unowned:"))
        self.assertEqual(resource["disposition"], "retain_unknown")
        self.assertFalse(resource["deletable"])

    def test_unregistered_existing_workspace_is_blocker(self):
        workspace = self.fixture.root / ".gsp" / "worktrees" / "unregistered"
        workspace.mkdir(parents=True)
        self.fixture.add_pair(17, topology={
            "workspace_root": str(workspace),
            "branch": "feature/unregistered",
            "effective_mode": "worktree",
        })
        self.fixture.create_index()
        manifest = preview_migration(self.fixture.root)
        self.assertIn("GIT_WORKTREE_UNREGISTERED", {row["code"] for row in manifest.blockers})
        resource = next(row for row in manifest.resources if row["identity"] == "run-17")
        self.assertEqual(resource["disposition"], "retain_unknown")
        self.assertFalse(resource["deletable"])

    def test_worktree_path_compatibility_field_participates_in_resource_scan(self):
        missing = self.fixture.base / "worktree-path-only"
        self.fixture.add_pair(35, topology={
            "worktree_path": str(missing),
            "branch": "feature/path-only",
            "effective_mode": "worktree",
        })
        self.fixture.create_index()
        manifest = preview_migration(self.fixture.root)
        resource = next((
            row for row in manifest.resources
            if row["identity"].endswith("run-35")
        ), None)
        self.assertIsNotNone(resource)
        self.assertEqual(resource["workspace_path"], str(missing))
        self.assertEqual(resource["disposition"], "workspace_missing")
        self.assertFalse(resource["deletable"])
        self.assertFalse(resource["disposed"])

    def test_conflicting_workspace_root_and_worktree_path_are_unknown(self):
        workspace_root = self.fixture.base / "workspace-root"
        worktree_path = self.fixture.base / "worktree-path"
        self.fixture.add_pair(36, topology={
            "workspace_root": str(workspace_root),
            "worktree_path": str(worktree_path),
            "branch": "feature/conflict",
            "effective_mode": "worktree",
        })
        self.fixture.create_index()
        manifest = preview_migration(self.fixture.root)
        self.assertIn("WORKTREE_PATH_CONFLICT", {row["code"] for row in manifest.blockers})
        resource = next((
            row for row in manifest.resources
            if row["identity"].endswith("run-36")
        ), None)
        self.assertIsNotNone(resource)
        self.assertEqual(resource["disposition"], "retain_unknown")
        self.assertFalse(resource["deletable"])
        self.assertFalse(resource["disposed"])

    def test_todo_transaction_and_project_mismatch_are_blockers(self):
        self.fixture.add_pair(18)
        self.fixture.add_work("GSP-000019", f"{19:032x}", run_id="run-19")
        self.fixture.add_run(
            "run-19", number="GSP-000019", work_uid=f"{19:032x}",
            project_uid="f" * 32,
        )
        self.fixture.add_todo("TODO-000018")
        write_bytes(self.fixture.root / ".gsp" / "backlog" / "transaction.json", b"{}\n")
        self.fixture.create_index()
        manifest = preview_migration(self.fixture.root)
        blockers = {row["code"] for row in manifest.blockers}
        self.assertIn("TODO_TRANSACTION_PENDING", blockers)
        self.assertIn("PROJECT_UID_MISMATCH", blockers)
        todo = next(row for row in manifest.classification_manifest if row["domain"] == "todo_marker")
        self.assertEqual(todo["classification"], "blocked")

    def test_todo_markers_and_records_require_bidirectional_coverage(self):
        self.fixture.add_todo("TODO-000101")
        records_path = self.fixture.root / ".gsp" / "backlog" / "records.json"
        records = json.loads(records_path.read_text(encoding="utf-8"))
        del records["items"]["TODO-000101"]
        records["items"]["TODO-000102"] = {
            "source": "manual:TODOS.md#TODO-000102",
            "origin": "",
            "blocking": "no",
            "evidence": "",
            "resolution": "",
            "history": "",
            "links": [],
            "component": "project",
            "object_uid": "",
            "completed_hash": None,
        }
        write_json(records_path, records)
        self.fixture.create_index()
        manifest = preview_migration(self.fixture.root)
        codes = {row["code"] for row in manifest.blockers}
        self.assertIn("TODO_RECORD_MISSING", codes)
        self.assertIn("TODO_RECORD_ORPHAN", codes)

    def test_todo_raw_markdown_digest_must_match_document_index(self):
        self.fixture.add_todo("TODO-000103")
        self.fixture.create_index(todo_text="stale")
        manifest = preview_migration(self.fixture.root)
        self.assertIn("TODO_DOCUMENT_STALE", {row["code"] for row in manifest.blockers})

    def test_live_writer_lock_is_blocker_and_preview_is_byte_readonly(self):
        self.fixture.add_pair(20)
        self.fixture.create_index()
        owner = identity(os.getpid())
        write_json(self.fixture.root / ".gsp" / "writer.lock", {
            "schema": 2, "pid": owner["pid"],
            "process_token": owner.get("token"),
            "at": "2026-11-25T00:00:00+00:00",
        })
        before = source_snapshot(self.fixture.root)
        manifest = preview_migration(self.fixture.root)
        after = source_snapshot(self.fixture.root)
        self.assertIn("WRITER_LOCK_LIVE", {row["code"] for row in manifest.blockers})
        self.assertEqual(before, after)

    def test_real_held_writer_advisory_lock_is_blocker(self):
        self.fixture.add_pair(27)
        self.fixture.create_index()
        lock = self.fixture.root / ".gsp" / "writer.lock"
        with locked(self.fixture.root):
            before_size = lock.stat().st_size
            manifest = preview_migration(self.fixture.root)
            after_size = lock.stat().st_size
        self.assertIn("WRITER_LOCK_LIVE", {row["code"] for row in manifest.blockers})
        self.assertEqual(before_size, after_size)
    def test_malformed_and_unknown_writer_are_blockers(self):
        self.fixture.add_pair(21)
        self.fixture.create_index()
        lock = self.fixture.root / ".gsp" / "writer.lock"
        write_bytes(lock, b"{not-json\n")
        malformed = preview_migration(self.fixture.root)
        self.assertIn("WRITER_LOCK_MALFORMED", {row["code"] for row in malformed.blockers})
        owner = identity(os.getpid())
        write_json(lock, {
            "schema": 2, "pid": owner["pid"],
            "process_token": "not-the-current-process:" + str(owner.get("token")),
            "at": "2026-11-25T00:00:00+00:00",
        })
        before = source_snapshot(self.fixture.root)
        unknown = preview_migration(self.fixture.root)
        self.assertIn("WRITER_LOCK_UNKNOWN", {row["code"] for row in unknown.blockers})
        self.assertEqual(before, source_snapshot(self.fixture.root))

    def test_live_driver_and_live_inflight_are_blockers(self):
        _, run_state = self.fixture.add_pair(22, run_id="run-22")
        self.fixture.create_index()
        owner = identity(os.getpid())
        write_json(self.fixture.root / ".gsp" / "driver.lock", {
            "schema": 1, "pid": owner["pid"], "token": owner.get("token"),
            "task": "run-22", "nonce": "fixture", "at": "2026-11-25T00:00:00+00:00",
        })
        write_json(self.fixture.run_root / "run-22" / "driver" / "state.json", {
            "schema": 1, "task": "run-22", "status": "running",
            "pending": {"id": "job-22", "worker": owner},
            "pending_check": None,
        })
        evidence_id = "a" * 32
        run_state["inflight"] = {
            "id": evidence_id, "task": "T01", "phase": "command",
            "at": "2026-11-25T00:00:00+00:00",
        }
        write_json(self.fixture.run_root / "run-22" / "state.json", run_state)
        write_json(self.fixture.run_root / "run-22" / "evidence" / evidence_id / "command-progress.json", {
            "schema": 1, "evidence_id": evidence_id, "task": "T01",
            "phase": "command", "status": "running",
            "process_identity": owner, "parent": owner,
        })
        before = source_snapshot(self.fixture.root)
        manifest = preview_migration(self.fixture.root)
        codes = {row["code"] for row in manifest.blockers}
        self.assertIn("DRIVER_LOCK_LIVE", codes)
        self.assertIn("DRIVER_STATE_LIVE", codes)
        self.assertIn("INFLIGHT_LIVE", codes)
        self.assertEqual(before, source_snapshot(self.fixture.root))

    def test_unknown_driver_and_inflight_are_blockers(self):
        _, run_state = self.fixture.add_pair(23, run_id="run-23")
        self.fixture.create_index()
        owner = identity(os.getpid())
        unknown_owner = {"pid": owner["pid"], "token": "unknown"}
        write_json(self.fixture.run_root / "run-23" / "driver" / "state.json", {
            "schema": 1, "task": "run-23", "status": "running",
            "pending": {"id": "job-23", "worker": unknown_owner},
            "pending_check": None,
        })
        evidence_id = "b" * 32
        run_state["inflight"] = {
            "id": evidence_id, "task": "T01", "phase": "command",
            "at": "2026-11-25T00:00:00+00:00",
        }
        write_json(self.fixture.run_root / "run-23" / "state.json", run_state)
        write_json(self.fixture.run_root / "run-23" / "evidence" / evidence_id / "command-progress.json", {
            "schema": 1, "evidence_id": evidence_id, "task": "T01",
            "phase": "command", "status": "running",
            "process_identity": unknown_owner, "parent": unknown_owner,
        })
        manifest = preview_migration(self.fixture.root)
        codes = {row["code"] for row in manifest.blockers}
        self.assertIn("DRIVER_STATE_UNKNOWN", codes)
        self.assertIn("INFLIGHT_UNKNOWN", codes)

    def test_proven_dead_or_stale_execution_is_not_reported_live(self):
        _, run_state = self.fixture.add_pair(24, run_id="run-24")
        self.fixture.create_index()
        dead_owner = {"pid": 2147483647, "token": "historical-process-token"}
        write_json(self.fixture.run_root / "run-24" / "driver" / "state.json", {
            "schema": 1, "task": "run-24", "status": "running",
            "pending": {"id": "job-24", "worker": dead_owner},
            "pending_check": None,
        })
        evidence_id = "c" * 32
        run_state["inflight"] = {
            "id": evidence_id, "task": "T01", "phase": "command",
            "at": "2026-11-25T00:00:00+00:00",
        }
        write_json(self.fixture.run_root / "run-24" / "state.json", run_state)
        write_json(self.fixture.run_root / "run-24" / "evidence" / evidence_id / "command-progress.json", {
            "schema": 1, "evidence_id": evidence_id, "task": "T01",
            "phase": "command", "status": "running",
            "process_identity": dead_owner, "parent": dead_owner,
        })
        manifest = preview_migration(self.fixture.root)
        codes = {row["code"] for row in manifest.blockers}
        self.assertIn("DRIVER_STATE_STALE", codes)
        self.assertIn("INFLIGHT_STALE", codes)
        self.assertNotIn("DRIVER_STATE_LIVE", codes)
        self.assertNotIn("INFLIGHT_LIVE", codes)
        self.assertNotIn("DRIVER_STATE_UNKNOWN", codes)
        self.assertNotIn("INFLIGHT_UNKNOWN", codes)

    def test_malformed_inflight_identifier_is_blocker(self):
        _, run_state = self.fixture.add_pair(28, run_id="run-28")
        self.fixture.create_index()
        run_state["inflight"] = {
            "id": "../../outside", "task": "T01", "phase": "command",
            "at": "2026-11-25T00:00:00+00:00",
        }
        write_json(self.fixture.run_root / "run-28" / "state.json", run_state)
        manifest = preview_migration(self.fixture.root)
        self.assertIn("INFLIGHT_MALFORMED", {row["code"] for row in manifest.blockers})
    def test_malformed_driver_and_execution_evidence_are_blockers(self):
        self.fixture.add_pair(25, run_id="run-25")
        self.fixture.create_index()
        write_bytes(self.fixture.root / ".gsp" / "driver.lock", b"{bad-driver-lock\n")
        write_bytes(self.fixture.run_root / "run-25" / "driver" / "state.json", b"{bad-driver-state\n")
        manifest = preview_migration(self.fixture.root)
        codes = {row["code"] for row in manifest.blockers}
        self.assertIn("DRIVER_LOCK_MALFORMED", codes)
        self.assertIn("DRIVER_STATE_INVALID", codes)

    def test_verify_source_detects_execution_file_drift(self):
        self.fixture.add_pair(26)
        self.fixture.create_index()
        lock = self.fixture.root / ".gsp" / "writer.lock"
        write_json(lock, {
            "schema": 2, "pid": 2147483647,
            "process_token": "historical-process-token",
            "at": "2026-11-25T00:00:00+00:00",
        })
        manifest = preview_migration(self.fixture.root)
        write_json(lock, {
            "schema": 2, "pid": 2147483647,
            "process_token": "historical-process-token",
            "at": "2026-11-25T00:00:01+00:00",
        })
        with self.assertRaisesRegex(WorkflowError, "MIGRATION_SOURCE_CHANGED"):
            verify_source(self.fixture.root, manifest)
    def test_legacy_session_dispatch_aliases_are_scoped_and_byte_readonly(self):
        for ordinal, run_id in ((30, "run-30"), (31, "run-31")):
            self.fixture.add_pair(
                ordinal, run_id=run_id, tasks=[{"id": "T01", "status": "running"}],
            )
            write_json(self.fixture.run_root / run_id / "sessions" / "index.json", {
                "schema": 1, "task": run_id, "number": f"GSP-{ordinal:06d}",
                "controller_session": "S01", "next_session": 2,
                "workspace_leases": {}, "sessions": [{"id": "S01", "task": "T01"}],
                "tasks": {}, "events": [], "outbox": [],
            })
            write_json(self.fixture.run_root / run_id / "sessions" / "dispatch.json", {
                "schema": 1, "task": run_id, "next_dispatch": 2,
                "dispatches": [{
                    "id": "D001", "task": "T01",
                    "session_hint": "S02" if run_id == "run-30" else "S01",
                }],
                "cleanup": [],
            })
        self.fixture.create_index()
        before = source_snapshot(self.fixture.root)
        manifest = preview_migration(self.fixture.root)
        after = source_snapshot(self.fixture.root)
        self.assertEqual(before, after)

        aliases = [
            row for row in manifest.aliases
            if row.get("object_kind") in {"session", "dispatch"}
        ]
        sessions = {
            (row["run_identity"], row["identity"], row.get("alias_kind")): row
            for row in aliases if row["object_kind"] == "session"
        }
        dispatches = {
            (row["run_identity"], row["identity"]): row
            for row in aliases if row["object_kind"] == "dispatch"
        }
        self.assertEqual(sessions[("run-30", "S01", "registry_session")]["work_identity"], "GSP-000030")
        self.assertEqual(sessions[("run-31", "S01", "registry_session")]["work_identity"], "GSP-000031")
        self.assertTrue(sessions[("run-30", "S02", "dispatch_session_hint")]["reservation"])
        self.assertEqual(dispatches[("run-30", "D001")]["plan_task"], "T01")
        self.assertEqual(dispatches[("run-31", "D001")]["plan_task"], "T01")
        self.assertIsNone(dispatches[("run-30", "D001")]["canonical_identity"])
        self.assertFalse(any(
            row["code"] in {"SESSION_ID_DUPLICATE", "DISPATCH_ID_DUPLICATE"}
            for row in manifest.blockers
        ))

        water = manifest.backup_policy["session_dispatch"]
        session_scopes = {row["scope_identity"]: row for row in water["sessions"]}
        dispatch_scopes = {row["scope_identity"]: row for row in water["dispatches"]}
        self.assertEqual(session_scopes["run-30"]["observed_max"], 1)
        self.assertEqual(session_scopes["run-30"]["reserved_max"], 2)
        self.assertEqual(session_scopes["run-30"]["declared_next"], 2)
        self.assertEqual(session_scopes["run-30"]["effective_next"], 3)
        self.assertEqual(session_scopes["run-31"]["observed_max"], 1)
        self.assertEqual(session_scopes["run-31"]["effective_next"], 2)
        self.assertEqual(dispatch_scopes["run-30"]["observed_max"], 1)
        self.assertEqual(dispatch_scopes["run-30"]["declared_next"], 2)
        self.assertEqual(dispatch_scopes["run-30"]["effective_next"], 2)
        self.assertEqual(dispatch_scopes["run-31"]["effective_next"], 2)

    def test_session_dispatch_registry_mismatch_duplicate_and_invalid_are_blockers(self):
        self.fixture.add_pair(
            32, run_id="run-32", tasks=[{"id": "T01", "status": "running"}],
        )
        write_json(self.fixture.run_root / "run-32" / "sessions" / "index.json", {
            "schema": 1, "task": "another-run", "next_session": "2",
            "sessions": [{"id": "S01"}, {"id": "S01"}, {"id": "S1"}],
            "workspace_leases": {}, "tasks": {}, "events": [], "outbox": [],
        })
        write_json(self.fixture.run_root / "run-32" / "sessions" / "dispatch.json", {
            "schema": 1, "task": "another-run", "next_dispatch": "2",
            "dispatches": [{"id": "D001", "task": "T01"}, {"id": "D001", "task": "T01"},
                           {"id": "D1", "task": "T01"}],
            "cleanup": [],
        })
        self.fixture.create_index()
        manifest = preview_migration(self.fixture.root)
        codes = {row["code"] for row in manifest.blockers}
        self.assertIn("SESSION_REGISTRY_TASK_MISMATCH", codes)
        self.assertIn("SESSION_NEXT_INVALID", codes)
        self.assertIn("SESSION_ID_DUPLICATE", codes)
        self.assertIn("SESSION_ID_INVALID", codes)
        self.assertIn("DISPATCH_REGISTRY_TASK_MISMATCH", codes)
        self.assertIn("DISPATCH_NEXT_INVALID", codes)
        self.assertIn("DISPATCH_ID_DUPLICATE", codes)
        self.assertIn("DISPATCH_ID_INVALID", codes)

    def test_session_dispatch_high_water_never_rewinds(self):
        self.fixture.add_pair(
            33, run_id="run-33", tasks=[{"id": "T01", "status": "running"}],
        )
        write_json(self.fixture.run_root / "run-33" / "sessions" / "index.json", {
            "schema": 1, "task": "run-33", "next_session": 2,
            "sessions": [{"id": "S01"}, {"id": "S03"}],
            "workspace_leases": {}, "tasks": {}, "events": [], "outbox": [],
        })
        write_json(self.fixture.run_root / "run-33" / "sessions" / "dispatch.json", {
            "schema": 1, "task": "run-33", "next_dispatch": 2,
            "dispatches": [
                {"id": "D001", "task": "T01", "session_hint": "S05"},
                {"id": "D004", "task": "T01", "session_hint": "S05"},
            ],
            "cleanup": [],
        })
        self.fixture.create_index()
        manifest = preview_migration(self.fixture.root)
        codes = {row["code"] for row in manifest.blockers}
        self.assertIn("SESSION_NEXT_NOT_ABOVE_OBSERVED", codes)
        self.assertIn("DISPATCH_NEXT_NOT_ABOVE_OBSERVED", codes)
        water = manifest.backup_policy["session_dispatch"]
        session = water["sessions"][0]
        dispatch = water["dispatches"][0]
        self.assertEqual(session["observed_max"], 3)
        self.assertEqual(session["reserved_max"], 5)
        self.assertEqual(session["declared_next"], 2)
        self.assertEqual(session["effective_next"], 6)
        self.assertEqual(dispatch["observed_max"], 4)
        self.assertEqual(dispatch["declared_next"], 2)
        self.assertEqual(dispatch["effective_next"], 5)

    def test_verify_source_detects_session_dispatch_drift(self):
        self.fixture.add_pair(
            34, run_id="run-34", tasks=[{"id": "T01", "status": "running"}],
        )
        index = self.fixture.run_root / "run-34" / "sessions" / "index.json"
        dispatch = self.fixture.run_root / "run-34" / "sessions" / "dispatch.json"
        write_json(index, {
            "schema": 1, "task": "run-34", "controller_session": "S01",
            "next_session": 2, "workspace_leases": {}, "sessions": [{"id": "S01"}],
            "tasks": {}, "events": [], "outbox": [],
        })
        write_json(dispatch, {
            "schema": 1, "task": "run-34", "next_dispatch": 2,
            "dispatches": [{"id": "D001", "task": "T01", "session_hint": "S02"}],
            "cleanup": [],
        })
        self.fixture.create_index()
        manifest = preview_migration(self.fixture.root)
        original_index = index.read_bytes()
        original_dispatch = dispatch.read_bytes()
        write_bytes(index, original_index + b"\n")
        with self.assertRaisesRegex(WorkflowError, "MIGRATION_SOURCE_CHANGED"):
            verify_source(self.fixture.root, manifest)
        write_bytes(index, original_index)
        verify_source(self.fixture.root, manifest)
        write_bytes(dispatch, original_dispatch + b"\n")
        with self.assertRaisesRegex(WorkflowError, "MIGRATION_SOURCE_CHANGED"):
            verify_source(self.fixture.root, manifest)

    def test_sol_legacy_todo_alias_contributes_only_to_todo_highwater(self):
        self.fixture.add_pair(1)
        self.fixture.add_todo('GSP-9999')
        self.fixture.add_todo('TODO-000023')
        self.fixture.create_index()
        before = source_snapshot(self.fixture.root)
        manifest = preview_migration(self.fixture.root)
        counter = manifest.backup_policy['counter']
        self.assertEqual(counter['highest'], 1)
        self.assertEqual(counter['todo_highest'], 9999)
        self.assertIn('GSP-9999', counter['todo_sources']['todo_marker'])
        self.assertEqual(source_snapshot(self.fixture.root), before)

    def test_sol_todo_record_document_binding_is_verified(self):
        self.fixture.add_todo('TODO-000001')
        self.fixture.create_index()
        path = self.fixture.root / '.gsp/backlog/records.json'
        original = json.loads(path.read_text(encoding='utf-8'))
        for document in (
            {'path': 'TODO.md', 'sha256': original['document']['sha256']},
            {'path': 'TODOS.md', 'sha256': '0' * 64},
            {},
        ):
            with self.subTest(document=document):
                records = json.loads(json.dumps(original))
                records['document'] = document
                write_json(path, records)
                before = source_snapshot(self.fixture.root)
                manifest = preview_migration(self.fixture.root)
                self.assertTrue(any(row['code'] == 'TODO_RECORD_DOCUMENT_MISMATCH'
                                    for row in manifest.blockers))
                self.assertTrue(all(row['classification'] == 'blocked'
                                    for row in manifest.classification_manifest
                                    if row['domain'] == 'todo_marker'))
                self.assertEqual(source_snapshot(self.fixture.root), before)

    def test_sol_custom_backlog_source_is_preserved_without_enum(self):
        self.fixture.add_todo('TODO-000001')
        self.fixture.create_index()
        path = self.fixture.root / '.gsp/backlog/records.json'
        records = json.loads(path.read_text(encoding='utf-8'))
        records['items']['TODO-000001']['source'] = 'custom source: recorded user observation'
        write_json(path, records)
        manifest = preview_migration(self.fixture.root)
        self.assertFalse(any(row['code'].startswith(('TODO_', 'BACKLOG_'))
                             for row in manifest.blockers))

    def test_sol_unusable_workspace_path_is_structured_and_readonly(self):
        _, state = self.fixture.add_pair(1)
        self.fixture.create_index()
        state_path = self.fixture.root / '.gsp/runs/run-1/state.json'
        for topology in (
            {'workspace_root': 'bad\u0000path', 'effective_mode': 'worktree'},
            {'worktree_path': 'bad\u0000path', 'effective_mode': 'worktree'},
            {'workspace_root': str(self.fixture.root), 'worktree_path': 'bad\u0000path'},
        ):
            with self.subTest(topology=topology):
                state['git_topology'] = topology
                write_json(state_path, state)
                before = source_snapshot(self.fixture.root)
                try:
                    manifest = preview_migration(self.fixture.root)
                except (OSError, RuntimeError, ValueError) as exc:
                    self.fail('expected structured blocker, got ' + repr(exc))
                self.assertTrue(any(row['code'] == 'WORKTREE_PATH_INVALID'
                                    for row in manifest.blockers))
                self.assertEqual(source_snapshot(self.fixture.root), before)

    def test_sol_malformed_run_and_task_topology_is_blocked(self):
        _, original = self.fixture.add_pair(1)
        self.fixture.create_index()
        state_path = self.fixture.root / '.gsp/runs/run-1/state.json'
        for kind in ('run', 'task'):
            for malformed in (
                'not-an-object', ['lost'], 0, '',
                {'workspace_root': str(self.fixture.root), 'branch': ['lost']},
                {'workspace_root': str(self.fixture.root), 'branch': 0},
                {'workspace_root': str(self.fixture.root), 'effective_mode': []},
            ):
                with self.subTest(kind=kind, malformed=malformed):
                    state = json.loads(json.dumps(original))
                    identity = 'run-1'
                    if kind == 'run':
                        state['git_topology'] = malformed
                    else:
                        state['tasks'] = [{'id': 'T01', 'topology': malformed}]
                        identity += ':T01'
                    write_json(state_path, state)
                    before = source_snapshot(self.fixture.root)
                    manifest = preview_migration(self.fixture.root)
                    self.assertTrue(any(row['code'] == 'WORKTREE_TOPOLOGY_INVALID'
                                        and row.get('identity') == identity
                                        for row in manifest.blockers))
                    self.assertEqual(source_snapshot(self.fixture.root), before)

if __name__ == "__main__":
    unittest.main()
