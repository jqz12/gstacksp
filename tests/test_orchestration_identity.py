"""Project-wide identity allocation and typed legacy aliases."""
import importlib
import io
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import replace
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch


def _request(logical_key, input_value="same-input", work_uid=None):
    types = importlib.import_module("gsp.orchestration_types")
    return types.RequestRef(
        request_uid=uuid.uuid4().hex,
        logical_key=logical_key,
        input_digest=types.digest_text(input_value),
        project_uid="project-fixture",
        work_uid=work_uid,
        attempt_uid=None,
        operation_kind="allocate",
    )


def _allocate_in_child(root, index, queue):
    store_module = importlib.import_module("gsp.identity_store")
    store = store_module.IdentityStore(Path(root))
    result = store.allocate("work", None, _request("child-" + str(index)))
    queue.put((result.uid, result.canonical_id))


def _initialize_and_allocate_in_child(root, index, barrier, queue):
    try:
        store_module = importlib.import_module("gsp.identity_store")
        store = store_module.IdentityStore(Path(root))
        barrier.wait(timeout=20)
        store.initialize()
        request = replace(
            _request("first-initialize-" + str(index)),
            project_uid=store.project_uid(),
        )
        result = store.allocate(
            "work", None, request
        )
        queue.put(("ok", result.uid, result.canonical_id))
    except Exception as exc:
        queue.put(("error", type(exc).__name__, str(exc)))


def _initialize_after_publish_barrier(root, barrier, queue):
    try:
        from gsp.identity_store import IdentityStore

        real_link = os.link
        authority_state = IdentityStore.authority_state
        first_state = True

        def coordinated_state(store):
            nonlocal first_state
            state = authority_state(store)
            if first_state:
                first_state = False
                barrier.wait(timeout=30)
            return state

        def coordinated_link(source, destination):
            candidate = Path(source).name.startswith(".registry.") and Path(destination).name == "registry.sqlite"
            try:
                return real_link(source, destination)
            finally:
                if candidate:
                    with closing(sqlite3.connect(destination, timeout=30)) as db:
                        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
                    queue.put(("published", str(mode).lower()))
                    barrier.wait(timeout=30)

        with patch.object(IdentityStore, "authority_state", coordinated_state):
            with patch("gsp.identity_store.os.link", coordinated_link):
                IdentityStore(Path(root)).initialize()
        queue.put(("ok",))
    except Exception as exc:
        queue.put(("error", type(exc).__name__, str(exc)))


def _initialize_project_in_child(root, start, queue):
    try:
        from gsp.runtime import Project

        start.wait(timeout=20)
        Project(Path(root)).initialize()
        queue.put(("ok", Path(root).resolve()))
    except Exception as exc:
        queue.put(("error", type(exc).__name__, str(exc)))


class IdentityStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-identity-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".gsp").mkdir(parents=True)
        (self.root / ".gsp/project.json").write_text(
            '{"id":"project-fixture","root":' +
            __import__("json").dumps(str(self.root)) + '}',
            encoding="utf-8",
        )
        try:
            module = importlib.import_module("gsp.identity_store")
        except ModuleNotFoundError:
            self.fail("identity store feature is missing")
        self.store = module.IdentityStore(self.root)
        self.store.initialize()

    def project_root(self, name, project_id=None):
        root = self.root / name
        (root / ".gsp").mkdir(parents=True)
        if project_id is not None:
            (root / ".gsp/project.json").write_text(
                json.dumps({"id": project_id, "root": str(root)}),
                encoding="utf-8",
            )
        return root

    def test_concurrent_allocations_are_unique(self):
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        processes = [
            context.Process(target=_allocate_in_child, args=(str(self.root), index, queue))
            for index in range(12)
        ]
        for process in processes:
            process.start()
        rows = [queue.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(20)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(len({uid for uid, _ in rows}), len(rows))
        self.assertEqual(len({canonical for _, canonical in rows}), len(rows))
        numbers = sorted(int(canonical.split("-")[1]) for _, canonical in rows)
        self.assertEqual(numbers, list(range(1, len(rows) + 1)))

    def test_concurrent_first_initialization_converges_on_one_authority(self):
        root = self.project_root("concurrent-first-init", "first-init-project")
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        barrier = context.Barrier(8)
        processes = [
            context.Process(
                target=_initialize_and_allocate_in_child,
                args=(str(root), index, barrier, queue),
            )
            for index in range(8)
        ]
        for process in processes:
            process.start()
        rows = [queue.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(30)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(
            [row for row in rows if row[0] == "error"],
            [],
        )
        self.assertEqual(len({row[1] for row in rows}), len(rows))
        numbers = sorted(int(row[2].split("-")[1]) for row in rows)
        self.assertEqual(numbers, list(range(1, len(rows) + 1)))
        self.assertEqual(
            list((root / ".gsp" / "orchestration").glob(".registry.*")),
            [],
        )

    def test_publication_barrier_never_races_on_shared_journal_mode(self):
        root = self.project_root("barrier-first-init", "barrier-project")
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        barrier = context.Barrier(8)
        processes = [
            context.Process(
                target=_initialize_after_publish_barrier,
                args=(str(root), barrier, queue),
            )
            for _ in range(8)
        ]
        for process in processes:
            process.start()
        rows = []
        try:
            while sum(row[0] in ("ok", "error") for row in rows) < len(processes):
                rows.append(queue.get(timeout=40))
        finally:
            for process in processes:
                process.join(40)
                if process.is_alive():
                    process.terminate()
                    process.join(10)
        for process in processes:
            process.join(40)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual([row for row in rows if row[0] == "error"], [])
        self.assertEqual([row for row in rows if row[0] == "published"], [("published", "wal")] * len(processes))
        self.assertEqual(
            IdentityStoreTests.store_state(root), {"state": "ready"}
        )
        with closing(
            sqlite3.connect(root / ".gsp/orchestration/registry.sqlite")
        ) as db:
            mode = db.execute("PRAGMA journal_mode").fetchone()
        self.assertEqual(str(mode[0]).lower(), "wal")

    @staticmethod
    def store_state(root):
        from gsp.identity_store import IdentityStore

        state = IdentityStore(root).authority_state()
        return {key: state[key] for key in ("state",) if key in state}

    def test_request_replay(self):
        request = _request("create-fixture")
        first = self.store.allocate("work", None, request)
        second = self.store.allocate("work", None, request)
        self.assertEqual(first, second)
        self.assertEqual(self.store.count("work", None), 1)

    def test_request_input_change_is_rejected(self):
        request = _request("create-fixture", "first")
        self.store.allocate("work", None, request)
        changed = _request("create-fixture", "second")
        with self.assertRaisesRegex(Exception, "REQUEST_INPUT_CHANGED"):
            self.store.allocate("work", None, changed)

    def test_request_for_another_project_is_rejected(self):
        foreign = _request("foreign-project")
        foreign = type(foreign)(
            request_uid=foreign.request_uid,
            logical_key=foreign.logical_key,
            input_digest=foreign.input_digest,
            project_uid="another-project",
            work_uid=foreign.work_uid,
            attempt_uid=foreign.attempt_uid,
            operation_kind=foreign.operation_kind,
        )
        with self.assertRaisesRegex(Exception, "PROJECT_MISMATCH"):
            self.store.allocate("work", None, foreign)

    def test_authority_project_uid_does_not_follow_later_identity_file(self):
        before = self.store.project_uid()
        (self.root / ".gsp/project.json").write_text(
            '{"id":"replacement-project","root":' +
            __import__("json").dumps(str(self.root)) + '}',
            encoding="utf-8",
        )
        self.assertEqual(before, "project-fixture")
        self.assertEqual(
            self.store.authority_state()["state"],
            "project_mismatch",
        )
        with self.assertRaisesRegex(Exception, "PROJECT_MISMATCH"):
            self.store.project_uid()
        with self.assertRaisesRegex(Exception, "PROJECT_MISMATCH"):
            self.store.allocate("work", None, _request("after-identity-change"))

    def test_authority_metadata_file_cannot_override_database_project(self):
        before = self.store.project_uid()
        self.store.metadata_path.write_text(
            '{"schema":1,"project_uid":"forged-project"}', encoding="utf-8"
        )
        self.assertEqual(self.store.project_uid(), before)

    def test_explicit_duplicate_rejected(self):
        work = self.store.allocate("work", None, _request("session-owner"))
        first = self.store.allocate(
            "session",
            None,
            _request("session-one", work_uid=work.uid),
            explicit_id="S-000001",
        )
        self.assertEqual(first.canonical_id, "S-000001")
        with self.assertRaisesRegex(Exception, "ID_CONFLICT"):
            self.store.allocate(
                "session",
                None,
                _request("session-duplicate", work_uid=work.uid),
                explicit_id="S-000001",
            )

    def test_typed_legacy_aliases(self):
        work = self.store.allocate(
            "work",
            None,
            _request("work-seven"),
            explicit_id="GSP-000007",
        )
        todo = self.store.allocate("todo", None, _request("todo-seven"))
        self.store.add_alias("todo", "GSP-0007", todo.uid, None)
        self.assertEqual(self.store.resolve("work", "GSP-000007", None, write=False).uid, work.uid)
        self.assertEqual(self.store.resolve("todo", "GSP-0007", None, write=False).uid, todo.uid)
        self.assertNotEqual(
            self.store.resolve("todo", "GSP-0007", None, write=False).uid,
            self.store.resolve("work", "GSP-000007", None, write=False).uid,
        )
        with self.assertRaisesRegex(Exception, "ID_NOT_CANONICAL"):
            self.store.resolve("work", "GSP-0007", None, write=True)

    def test_typed_alias_reserves_numeric_token_against_reuse(self):
        holder = self.store.allocate("work", None, _request("alias-holder"))
        self.store.add_alias("work", "GSP-0007", holder.uid, None)
        with self.assertRaisesRegex(Exception, "ID_CONFLICT"):
            self.store.allocate(
                "work",
                None,
                _request("alias-canonical-conflict"),
                explicit_id="GSP-000007",
            )
        next_work = self.store.allocate("work", None, _request("after-alias-highwater"))
        self.assertEqual(next_work.canonical_id, "GSP-000008")

    def test_typed_alias_cannot_shadow_a_canonical_object(self):
        first = self.store.allocate("work", None, _request("canonical-first"))
        second = self.store.allocate("work", None, _request("canonical-second"))
        with self.assertRaisesRegex(Exception, "ID_AMBIGUOUS"):
            self.store.add_alias("work", second.canonical_id, first.uid, None)

    def test_counter_expands(self):
        work = self.store.allocate("work", None, _request("session-counter-owner"))
        self.store.allocate(
            "session",
            None,
            _request("session-max-six", work_uid=work.uid),
            explicit_id="S-999999",
        )
        next_object = self.store.allocate(
            "session",
            None,
            _request("session-seven-digits", work_uid=work.uid),
        )
        self.assertEqual(next_object.canonical_id, "S-1000000")

    def test_noncanonical_work_padding_is_rejected(self):
        with self.assertRaisesRegex(Exception, "ID_NOT_CANONICAL"):
            self.store.allocate(
                "work",
                None,
                _request("work-noncanonical-padding"),
                explicit_id="GSP-0000005",
            )

    def test_task_short_alias_resolves_to_one_canonical_identity(self):
        work = self.store.allocate("work", None, _request("work-task-alias"))
        task = self.store.allocate(
            "task",
            work.uid,
            _request("task-short-alias", work_uid=work.uid),
            explicit_id="T1",
        )
        self.assertEqual(task.canonical_id, "T01")
        self.assertEqual(
            self.store.resolve("task", "T1", work.uid, write=False).uid,
            task.uid,
        )
        self.assertEqual(
            self.store.resolve("task", "T01", work.uid, write=False).uid,
            task.uid,
        )
        with self.assertRaisesRegex(Exception, "ID_NOT_CANONICAL"):
            self.store.resolve("task", "T1", work.uid, write=True)
        next_task = self.store.allocate(
            "task",
            work.uid,
            _request("task-next", work_uid=work.uid),
        )
        self.assertEqual(next_task.canonical_id, "T02")

    def test_task_numbers_are_scoped(self):
        work_a = self.store.allocate("work", None, _request("work-a"))
        work_b = self.store.allocate("work", None, _request("work-b"))
        task_a = self.store.allocate(
            "task",
            work_a.uid,
            _request("task-a", work_uid=work_a.uid),
        )
        task_b = self.store.allocate(
            "task",
            work_b.uid,
            _request("task-b", work_uid=work_b.uid),
        )
        self.assertEqual(task_a.canonical_id, "T01")
        self.assertEqual(task_b.canonical_id, "T01")
        self.assertNotEqual(task_a.uid, task_b.uid)

    def test_replay_rejects_changed_request_identity_or_scope(self):
        request = _request("strict-replay")
        self.store.allocate("work", None, request)
        work = self.store.allocate("work", None, _request("strict-replay-scope"))
        changed = {
            "request_uid": replace(request, request_uid=uuid.uuid4().hex),
            "logical_key": replace(request, logical_key="different-logical-key"),
            "operation_kind": replace(request, operation_kind="update"),
        }
        for field, candidate in changed.items():
            with self.subTest(field=field):
                with self.assertRaisesRegex(Exception, "REQUEST_INPUT_CHANGED"):
                    self.store.allocate("work", None, candidate)
        with self.assertRaisesRegex(Exception, "REQUEST_INPUT_CHANGED"):
            self.store.allocate("todo", None, request)
        scoped_request = _request("scoped-replay", work_uid=work.uid)
        self.store.allocate("session", None, scoped_request)
        with self.assertRaisesRegex(Exception, "REQUEST_INPUT_CHANGED"):
            self.store.allocate("task", work.uid, scoped_request)

    def test_stable_uid_is_accepted_as_a_write_identity(self):
        work = self.store.allocate("work", None, _request("stable-write-work"))
        resolved = self.store.resolve("work", work.uid, None, write=True)
        self.assertEqual(resolved, work)

    def test_task_overpadding_is_rejected(self):
        work = self.store.allocate("work", None, _request("task-overpadding-work"))
        for token in ("T001", "T0001"):
            with self.subTest(token=token):
                with self.assertRaisesRegex(Exception, "ID_NOT_CANONICAL"):
                    self.store.allocate(
                        "task",
                        work.uid,
                        _request(
                            "task-overpadding-" + token,
                            work_uid=work.uid,
                        ),
                        explicit_id=token,
                    )

    def test_automatic_task_allocation_creates_short_read_alias(self):
        work = self.store.allocate("work", None, _request("automatic-task-work"))
        task = self.store.allocate(
            "task",
            work.uid,
            _request("automatic-task", work_uid=work.uid),
        )
        self.assertEqual(task.canonical_id, "T01")
        self.assertEqual(
            self.store.resolve("task", "T1", work.uid, write=False).uid,
            task.uid,
        )
        with self.assertRaisesRegex(Exception, "ID_NOT_CANONICAL"):
            self.store.resolve("task", "T1", work.uid, write=True)

    def test_session_binding_json_round_trip(self):
        types = importlib.import_module("gsp.orchestration_types")
        work = self.store.allocate("work", None, _request("session-binding-work"))
        session = self.store.allocate(
            "session",
            None,
            _request("session-binding", work_uid=work.uid),
        )
        binding = types.SessionBinding(
            session=session,
            work_uid="work-a",
            host="codex",
            host_thread_id=None,
            controller_generation=3,
            cancel_generation=1,
            capabilities=frozenset({"accept", "write"}),
            handle_path=".gsp/runs/example/sessions/S01.json",
        )
        encoded = json.dumps(binding.to_dict(), sort_keys=True)
        restored = types.SessionBinding.from_dict(json.loads(encoded))
        self.assertEqual(restored, binding)
        self.assertIsInstance(restored.session, types.ObjectRef)
        self.assertIsInstance(restored.capabilities, frozenset)

    def test_open_issue_provider_protocol_is_defined(self):
        types = importlib.import_module("gsp.orchestration_types")
        self.assertTrue(hasattr(types, "OpenIssueProvider"))

    def test_first_time_authority_reports_and_initializes(self):
        root = self.project_root("first-time", "first-time-project")
        store = importlib.import_module("gsp.identity_store").IdentityStore(root)
        self.assertEqual(store.authority_state()["state"], "uninitialized")
        store.initialize()
        self.assertEqual(store.authority_state()["state"], "ready")

    def test_missing_database_with_surviving_identity_surface_is_not_reinitialized(self):
        hints = (
            ("orchestration", Path(".gsp/orchestration/registry.json"), "{}"),
            ("work-counter", Path(".gsp/work-counter.json"), '{"next":35}'),
            (
                "todos",
                Path("TODOS.md"),
                "<!-- gsp-backlog:v2 -->\n<!-- gsp-todo:GSP-0007 -->\n"
                "### GSP-0007 — Legacy\n<!-- /gsp-todo:GSP-0007 -->\n",
            ),
            ("backlog", Path(".gsp/backlog/records.json"), "{}"),
        )
        for name, relative, content in hints:
            with self.subTest(name=name):
                root = self.project_root("missing-" + name, "legacy-project")
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                store = importlib.import_module("gsp.identity_store").IdentityStore(root)
                self.assertEqual(store.authority_state()["state"], "missing_authority")
                with self.assertRaisesRegex(Exception, "IDENTITY_MIGRATION_REQUIRED"):
                    store.initialize()
                self.assertFalse(store.path.exists())

    def test_corrupt_authority_is_not_replaced(self):
        root = self.project_root("corrupt", "corrupt-project")
        registry = root / ".gsp/orchestration/registry.sqlite"
        registry.parent.mkdir(parents=True)
        registry.write_bytes(b"not-a-sqlite-authority")
        store = importlib.import_module("gsp.identity_store").IdentityStore(root)
        self.assertEqual(store.authority_state()["state"], "corrupt")
        with self.assertRaisesRegex(Exception, "IDENTITY_AUTHORITY_CORRUPT"):
            store.initialize()
        self.assertEqual(registry.read_bytes(), b"not-a-sqlite-authority")

    def test_operations_reject_an_unsupported_authority_schema(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("DROP TRIGGER metadata_identity_immutable")
            db.execute(
                "UPDATE metadata SET value=? WHERE key='schema_version'",
                ("999",),
            )
            self.store._schema(db)
            db.commit()
        with self.assertRaisesRegex(Exception, "IDENTITY_AUTHORITY_CORRUPT"):
            self.store.allocate("work", None, _request("unsupported-schema"))

    def test_read_schema_allows_absent_candidate_sidecar_without_creating_it(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            present = db.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='operation_candidates'"
            ).fetchone()
            if present:
                db.execute("DROP TABLE operation_candidates")
                db.commit()

        before = self.store.read_schema()
        self.assertEqual(self.store.authority_state()["state"], "ready")
        with closing(sqlite3.connect(self.store.path)) as db:
            present = db.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='operation_candidates'"
            ).fetchone()
        self.assertIsNone(present)
        self.assertEqual(before["schema"], 1)

    def test_old_v1_candidate_sidecar_is_lazily_created_without_reset(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("DROP TABLE IF EXISTS operation_candidates")
            db.commit()
        before = self.store.read_schema()
        authority_before = self.store.authority_state()
        path = self.root / ".gsp/runs/legacy-sidecar/state.json"
        path.parent.mkdir(parents=True)
        initial = {"status": "execute", "updated": "2026-11-25T00:00:00+00:00"}
        path.write_text(
            json.dumps(initial, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        request = replace(
            _request("legacy-sidecar"),
            project_uid=self.store.project_uid(),
        )
        expected = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
        operation = self.store.begin_operation(request, expected)
        self.assertIsNone(self.store.get_operation_candidate(operation.operation_uid))
        self.assertEqual(self.store.read_schema(), before)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='operation_candidates'"
                ).fetchone()
            )

        candidate_state = {**initial, "status": "review"}
        candidate_text = json.dumps(
            candidate_state,
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        with self.assertRaisesRegex(Exception, "REQUEST_INPUT_CHANGED"):
            self.store.freeze_operation_candidate(
                operation.operation_uid,
                path,
                candidate_text,
                "f" * 64,
            )
        self.assertEqual(self.store.read_schema(), before)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='operation_candidates'"
                ).fetchone()
            )
            self.assertEqual(
                db.execute(
                    "SELECT phase FROM operations WHERE operation_uid=?",
                    (operation.operation_uid,),
                ).fetchone()[0],
                "intent",
            )

        candidate = self.store.freeze_operation_candidate(
            operation.operation_uid,
            path,
            candidate_text,
            request.input_digest,
        )

        after = self.store.read_schema()
        self.assertEqual(after["schema"], before["schema"])
        self.assertEqual(after["incarnation"], before["incarnation"])
        self.assertEqual(after["sequences"], before["sequences"])
        self.assertEqual(
            self.store.authority_state()["state"],
            authority_before["state"],
        )
        self.assertEqual(
            self.store.get_operation_candidate(operation.operation_uid),
            candidate,
        )
        current = self.store.get_operation(operation.operation_uid)
        self.assertEqual(current.phase, "prepared")
        self.assertEqual(current.expected_state_digest, expected)
        with closing(sqlite3.connect(self.store.path)) as db:
            triggers = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND name LIKE 'operation_candidates_%'"
                )
            }
        self.assertEqual(
            triggers,
            {
                "operation_candidates_immutable",
                "operation_candidates_no_delete",
            },
        )

    def test_old_v1_target_guard_is_lazily_installed_only_at_write(self):
        path = self.root / ".gsp/runs/legacy-target/state.json"
        path.parent.mkdir(parents=True)
        expected = "b" * 64
        operation = self.store.begin_operation(
            replace(
                _request("legacy-target"),
                project_uid=self.store.project_uid(),
            ),
            expected,
        )
        sequences_before = self.store.read_schema()["sequences"]
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("DROP TRIGGER operations_targets_immutable")
            db.execute(
                "DELETE FROM metadata WHERE key='operations_target_guard'"
            )
            db.commit()

        self.assertEqual(self.store.authority_state()["state"], "ready")
        self.assertEqual(
            self.store.get_operation(operation.operation_uid).phase,
            "intent",
        )
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                    "AND name='operations_targets_immutable'"
                ).fetchone()
            )
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM metadata "
                    "WHERE key='operations_target_guard'"
                ).fetchone()
            )

        self.store.prepare_operation(
            operation.operation_uid,
            path,
            "c" * 64,
        )

        self.assertEqual(
            self.store.read_schema()["sequences"],
            sequences_before,
        )
        self.assertEqual(self.store.authority_state()["state"], "ready")
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertIsNotNone(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                    "AND name='operations_targets_immutable'"
                ).fetchone()
            )
            self.assertEqual(
                db.execute(
                    "SELECT value FROM metadata "
                    "WHERE key='operations_target_guard'"
                ).fetchone()[0],
                "1",
            )

    def test_read_schema_rejects_malformed_present_candidate_sidecar(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("DROP TABLE IF EXISTS operation_candidates")
            db.execute(
                "CREATE TABLE operation_candidates(operation_uid TEXT PRIMARY KEY)"
            )
            db.commit()

        self.assertEqual(self.store.authority_state()["state"], "corrupt")
        with self.assertRaisesRegex(Exception, "IDENTITY_AUTHORITY_CORRUPT"):
            self.store.read_schema()

    def test_guard_definitions_are_attested_not_only_names(self):
        replacements = {
            "candidate-delete": (
                "operation_candidates_no_delete",
                """CREATE TRIGGER operation_candidates_no_delete
                BEFORE DELETE ON operation_candidates
                BEGIN SELECT 1; END""",
            ),
            "operation-delete": (
                "operations_no_delete",
                """CREATE TRIGGER operations_no_delete
                BEFORE DELETE ON operations
                BEGIN SELECT 1; END""",
            ),
            "event-update": (
                "events_immutable",
                """CREATE TRIGGER events_immutable
                BEFORE UPDATE ON events
                BEGIN SELECT 1; END""",
            ),
            "wrong-table": (
                "events_immutable",
                """CREATE TRIGGER events_immutable
                BEFORE UPDATE ON metadata
                BEGIN SELECT 1; END""",
            ),
        }
        for name, (trigger, replacement) in replacements.items():
            with self.subTest(name=name):
                root = self.project_root(name, name + "-project")
                store = importlib.import_module(
                    "gsp.identity_store"
                ).IdentityStore(root)
                store.initialize()
                with closing(sqlite3.connect(store.path)) as db:
                    db.execute("DROP TRIGGER " + trigger)
                    db.execute(replacement)
                    db.commit()
                self.assertEqual(store.authority_state()["state"], "corrupt")
                with self.assertRaisesRegex(
                    Exception,
                    "IDENTITY_AUTHORITY_CORRUPT",
                ):
                    store.read_schema()

    def test_candidate_table_shape_is_attested(self):
        variants = {
            "missing-primary-key": """
                CREATE TABLE operation_candidates (
                    operation_uid TEXT NOT NULL,
                    state_path TEXT NOT NULL,
                    semantic_digest TEXT NOT NULL,
                    candidate_text TEXT NOT NULL,
                    candidate_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(operation_uid) REFERENCES operations(operation_uid)
                )
            """,
            "missing-foreign-key": """
                CREATE TABLE operation_candidates (
                    operation_uid TEXT PRIMARY KEY,
                    state_path TEXT NOT NULL,
                    semantic_digest TEXT NOT NULL,
                    candidate_text TEXT NOT NULL,
                    candidate_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """,
            "wrong-column-type": """
                CREATE TABLE operation_candidates (
                    operation_uid INTEGER PRIMARY KEY,
                    state_path TEXT NOT NULL,
                    semantic_digest TEXT NOT NULL,
                    candidate_text TEXT NOT NULL,
                    candidate_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(operation_uid) REFERENCES operations(operation_uid)
                )
            """,
        }
        triggers = (
            """CREATE TRIGGER operation_candidates_immutable
            BEFORE UPDATE ON operation_candidates
            BEGIN SELECT RAISE(ABORT, 'CANDIDATE_IMMUTABLE'); END""",
            """CREATE TRIGGER operation_candidates_no_delete
            BEFORE DELETE ON operation_candidates
            BEGIN SELECT RAISE(ABORT, 'CANDIDATE_NO_DELETE'); END""",
        )
        for name, create_table in variants.items():
            with self.subTest(name=name):
                root = self.project_root(name, name + "-project")
                store = importlib.import_module(
                    "gsp.identity_store"
                ).IdentityStore(root)
                store.initialize()
                with closing(sqlite3.connect(store.path)) as db:
                    db.execute("DROP TABLE operation_candidates")
                    db.execute(create_table)
                    for trigger in triggers:
                        db.execute(trigger)
                    db.commit()
                self.assertEqual(store.authority_state()["state"], "corrupt")
                with self.assertRaisesRegex(
                    Exception,
                    "IDENTITY_AUTHORITY_CORRUPT",
                ):
                    store.read_schema()

    def prepared_state_operation(self, name):
        root = self.project_root(name, name + "-project")
        store = importlib.import_module("gsp.identity_store").IdentityStore(root)
        store.initialize()
        path = root / ".gsp/runs" / name / "state.json"
        path.parent.mkdir(parents=True)
        initial = {"status": "execute", "updated": "2026-11-25T00:00:00+00:00"}
        path.write_text(
            json.dumps(initial, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        request = replace(
            _request(name),
            project_uid=store.project_uid(),
            operation_kind="state-write",
        )
        expected = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
        operation = store.begin_operation(request, expected)
        target = {**initial, "status": "review"}
        target_digest = __import__("hashlib").sha256(
            (json.dumps(target, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        ).hexdigest()
        store.prepare_operation(operation.operation_uid, path, target_digest)
        return store, operation, path, target_digest

    def test_operation_targets_are_transition_guarded(self):
        store, operation, path, target_digest = self.prepared_state_operation(
            "target-guard"
        )
        before = store.operation_details(operation.operation_uid)
        with closing(sqlite3.connect(store.path)) as db:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "OPERATION_TARGET_IMMUTABLE",
            ):
                db.execute(
                    "UPDATE operations SET state_path=?, new_state_digest=? "
                    "WHERE operation_uid=?",
                    (
                        ".gsp/runs/forged/state.json",
                        "f" * 64,
                        operation.operation_uid,
                    ),
                )
            db.rollback()
        self.assertEqual(store.operation_details(operation.operation_uid), before)
        self.assertEqual(store.operation_state_path(operation.operation_uid), path.resolve())
        self.assertEqual(before["new_state_digest"], target_digest)

    def test_operation_reads_reject_legacy_escaped_or_malformed_targets(self):
        cases = (
            (
                "escaped-path",
                "../escaped/state.json",
                None,
                None,
                "(OPERATION_PATH_ESCAPE|IDENTITY_AUTHORITY_CORRUPT)",
            ),
            (
                "bad-new-digest",
                None,
                "not-a-digest",
                None,
                "(STATE_DIGEST_INVALID|IDENTITY_AUTHORITY_CORRUPT)",
            ),
            (
                "bad-timestamp",
                None,
                None,
                "timestamp",
                "IDENTITY_AUTHORITY_CORRUPT",
            ),
            (
                "bad-phase",
                None,
                None,
                "phase",
                "IDENTITY_AUTHORITY_CORRUPT",
            ),
        )
        for name, replacement_path, replacement_digest, mutation, error in cases:
            with self.subTest(name=name):
                store, operation, _, _ = self.prepared_state_operation(
                    "read-" + name
                )
                with closing(sqlite3.connect(store.path)) as db:
                    db.execute("DROP TRIGGER IF EXISTS operations_targets_immutable")
                    db.execute(
                        "DELETE FROM metadata WHERE key='operations_target_guard'"
                    )
                    if replacement_path is not None:
                        db.execute(
                            "UPDATE operations SET state_path=? WHERE operation_uid=?",
                            (replacement_path, operation.operation_uid),
                        )
                    elif replacement_digest is not None:
                        db.execute(
                            "UPDATE operations SET new_state_digest=? "
                            "WHERE operation_uid=?",
                            (replacement_digest, operation.operation_uid),
                        )
                    elif mutation == "timestamp":
                        db.execute(
                            "UPDATE operations SET updated_at='' WHERE operation_uid=?",
                            (operation.operation_uid,),
                        )
                    else:
                        db.execute("PRAGMA ignore_check_constraints=ON")
                        db.execute(
                            "UPDATE operations SET phase='forged' WHERE operation_uid=?",
                            (operation.operation_uid,),
                        )
                        db.execute("PRAGMA ignore_check_constraints=OFF")
                    db.commit()
                with self.assertRaisesRegex(Exception, error):
                    store.get_operation(operation.operation_uid)

    def test_candidate_content_is_checked_only_when_the_row_is_accessed(self):
        request = replace(
            _request("candidate-content"),
            project_uid=self.store.project_uid(),
        )
        path = self.root / ".gsp/runs/candidate/state.json"
        path.parent.mkdir(parents=True)
        initial = {"status": "execute", "updated": "2026-11-25T00:00:00+00:00"}
        path.write_text(
            json.dumps(initial, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        expected = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
        operation = self.store.begin_operation(request, expected)
        candidate_state = {**initial, "status": "review"}
        candidate_text = json.dumps(
            candidate_state, ensure_ascii=False, indent=2
        ) + "\n"
        candidate = self.store.freeze_operation_candidate(
            operation.operation_uid,
            path,
            candidate_text,
            request.input_digest,
        )
        self.assertEqual(
            self.store.get_operation_candidate(operation.operation_uid),
            candidate,
        )

        with closing(sqlite3.connect(self.store.path)) as db:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "CANDIDATE_IMMUTABLE"):
                db.execute(
                    "UPDATE operation_candidates SET candidate_text='{}' "
                    "WHERE operation_uid=?",
                    (operation.operation_uid,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "CANDIDATE_NO_DELETE"):
                db.execute(
                    "DELETE FROM operation_candidates WHERE operation_uid=?",
                    (operation.operation_uid,),
                )
            db.rollback()
            db.execute("DROP TRIGGER operation_candidates_no_delete")
            db.commit()
        self.assertEqual(self.store.authority_state()["state"], "corrupt")
        with self.assertRaisesRegex(Exception, "IDENTITY_AUTHORITY_CORRUPT"):
            self.store.read_schema()

        with closing(sqlite3.connect(self.store.path)) as db:
            db.row_factory = sqlite3.Row
            self.store._schema(db)
            db.execute("DROP TRIGGER operation_candidates_immutable")
            db.execute(
                "UPDATE operation_candidates SET candidate_text='not-json' "
                "WHERE operation_uid=?",
                (operation.operation_uid,),
            )
            self.store._schema(db)
            db.commit()

        self.store.read_schema()
        self.assertEqual(self.store.authority_state()["state"], "ready")
        with self.assertRaisesRegex(
            Exception, "(STATE_CANDIDATE_INVALID|IDENTITY_AUTHORITY_CORRUPT)"
        ):
            self.store.get_operation_candidate(operation.operation_uid)

    def test_candidate_freeze_rejects_type_collapsed_semantic_replay(self):
        request = replace(
            _request("candidate-typed-replay"),
            project_uid=self.store.project_uid(),
        )
        path = self.root / ".gsp/runs/candidate-typed/state.json"
        path.parent.mkdir(parents=True)
        initial = {"status": "execute", "updated": "2026-11-25T00:00:00+00:00"}
        path.write_text(
            json.dumps(initial, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        operation = self.store.begin_operation(
            request,
            __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
        )
        first = {
            "status": "review",
            "value": 1,
            "updated": "2026-11-25T00:00:01+00:00",
        }
        candidate = self.store.freeze_operation_candidate(
            operation.operation_uid,
            path,
            json.dumps(first, ensure_ascii=False, indent=2) + "\n",
            request.input_digest,
        )
        operation_before = self.store.operation_details(operation.operation_uid)
        changed = {
            "status": "review",
            "value": True,
            "updated": "2026-11-25T00:00:02+00:00",
        }

        with self.assertRaisesRegex(
            Exception,
            "REQUEST_INPUT_CHANGED",
        ):
            self.store.freeze_operation_candidate(
                operation.operation_uid,
                path,
                json.dumps(changed, ensure_ascii=False, indent=2) + "\n",
                request.input_digest,
            )

        self.assertEqual(
            self.store.get_operation_candidate(operation.operation_uid),
            candidate,
        )
        self.assertEqual(
            self.store.operation_details(operation.operation_uid),
            operation_before,
        )

    def test_authority_rejects_missing_request_columns_and_required_guards(self):
        mutations = {
            "missing-request-column": (
                "DROP TABLE requests",
                "CREATE TABLE requests(request_uid TEXT PRIMARY KEY)",
            ),
            "missing-object-update-guard": (
                "DROP TRIGGER objects_immutable",
                "",
            ),
            "missing-object-delete-guard": (
                "DROP TRIGGER objects_no_delete",
                "",
            ),
            "missing-metadata-update-guard": (
                "DROP TRIGGER metadata_identity_immutable",
                "",
            ),
            "missing-metadata-delete-guard": (
                "DROP TRIGGER metadata_identity_no_delete",
                "",
            ),
            "missing-sequence-delete-guard": (
                "DROP TRIGGER sequences_no_delete",
                "",
            ),
            "missing-sequence-rewind-guard": (
                "DROP TRIGGER sequences_never_rewind",
                "",
            ),
            "missing-request-update-guard": (
                "DROP TRIGGER requests_immutable",
                "",
            ),
            "missing-request-delete-guard": (
                "DROP TRIGGER requests_no_delete",
                "",
            ),
        }
        for name, statements in mutations.items():
            with self.subTest(name=name):
                root = self.project_root(name, name + "-project")
                store = importlib.import_module(
                    "gsp.identity_store"
                ).IdentityStore(root)
                store.initialize()
                with closing(sqlite3.connect(store.path)) as db:
                    for statement in statements:
                        db.execute(statement)
                    db.commit()
                self.assertEqual(store.authority_state()["state"], "corrupt")
                with self.assertRaisesRegex(
                    Exception, "IDENTITY_AUTHORITY_CORRUPT"
                ):
                    store.allocate(
                        "work",
                        None,
                        type(_request("guarded-authority"))(
                            request_uid=uuid.uuid4().hex,
                            logical_key="guarded-authority",
                            input_digest="digest",
                            project_uid=name + "-project",
                            work_uid=None,
                            attempt_uid=None,
                            operation_kind="allocate",
                        ),
                    )

    def test_database_project_uid_and_incarnation_are_immutable(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "IDENTITY_METADATA_IMMUTABLE",
            ):
                db.execute(
                    "UPDATE metadata SET value='forged-project' "
                    "WHERE key='project_uid'"
                )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "IDENTITY_METADATA_NO_DELETE",
            ):
                db.execute("DELETE FROM metadata WHERE key='incarnation'")
            db.rollback()
        self.assertEqual(self.store.project_uid(), "project-fixture")

    def test_missing_project_identity_after_authority_init_blocks_operations(self):
        (self.root / ".gsp/project.json").unlink()
        self.assertEqual(
            self.store.authority_state()["state"],
            "project_identity_invalid",
        )
        with self.assertRaisesRegex(Exception, "PROJECT_IDENTITY_INVALID"):
            self.store.project_uid()
        with self.assertRaisesRegex(Exception, "PROJECT_IDENTITY_INVALID"):
            self.store.allocate("work", None, _request("after-identity-delete"))

    def test_identity_guards_actually_block_tampering(self):
        work = self.store.allocate("work", None, _request("guard-work"))
        with closing(sqlite3.connect(self.store.path)) as db:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "IDENTITY_IMMUTABLE"):
                db.execute(
                    "UPDATE objects SET canonical_id=? WHERE uid=?",
                    ("GSP-999999", work.uid),
                )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "IDENTITY_NO_DELETE"
            ):
                db.execute("DELETE FROM objects WHERE uid=?", (work.uid,))
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "IDENTITY_SEQUENCE_REWIND"
            ):
                db.execute(
                    "UPDATE sequences SET next_value=1 "
                    "WHERE kind='work' AND scope_uid=''"
                )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "IDENTITY_REQUEST_IMMUTABLE"
            ):
                db.execute("UPDATE requests SET state='forged'")
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "IDENTITY_REQUEST_NO_DELETE"
            ):
                db.execute("DELETE FROM requests")

    def test_idempotent_preexisting_alias_advances_the_high_watermark(self):
        holder = self.store.allocate("work", None, _request("legacy-alias-holder"))
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute(
                "INSERT INTO aliases(alias,object_kind,scope_uid,object_uid,created_at) "
                "VALUES(?,?,?,?,?)",
                ("GSP-0007", "work", "", holder.uid, "2026-01-01T00:00:00+00:00"),
            )
            db.commit()
        self.store.add_alias("work", "GSP-0007", holder.uid, None)
        next_object = self.store.allocate(
            "work", None, _request("after-preexisting-alias")
        )
        self.assertEqual(next_object.canonical_id, "GSP-000008")

    def test_project_level_objects_reject_scopes_and_work_bindings(self):
        for kind in ("work", "todo"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(Exception, "ID_SCOPE_INVALID"):
                    self.store.allocate(
                        kind,
                        "unexpected-scope",
                        _request(kind + "-scope-invalid"),
                    )

    def test_task_and_execution_requests_require_an_existing_work(self):
        work = self.store.allocate("work", None, _request("request-work"))
        with self.assertRaisesRegex(Exception, "REQUEST_WORK_REQUIRED"):
            self.store.allocate("session", None, _request("session-without-work"))
        with self.assertRaisesRegex(Exception, "REQUEST_WORK_REQUIRED"):
            self.store.allocate("dispatch", None, _request("dispatch-without-work"))
        with self.assertRaisesRegex(Exception, "REQUEST_WORK_REQUIRED"):
            self.store.allocate("task", work.uid, _request("task-without-work"))
        with self.assertRaisesRegex(Exception, "REQUEST_WORK_INVALID"):
            self.store.allocate(
                "session",
                None,
                _request("session-unknown-work", work_uid="missing-work"),
            )
        session = self.store.allocate(
            "session",
            None,
            _request("session-with-work", work_uid=work.uid),
        )
        dispatch = self.store.allocate(
            "dispatch",
            None,
            _request("dispatch-with-work", work_uid=work.uid),
        )
        self.assertEqual(session.canonical_id, "S-000001")
        self.assertEqual(dispatch.canonical_id, "D-000001")

    def test_task_scope_must_match_existing_work_request(self):
        work_a = self.store.allocate("work", None, _request("scope-work-a"))
        work_b = self.store.allocate("work", None, _request("scope-work-b"))
        with self.assertRaisesRegex(Exception, "REQUEST_SCOPE_MISMATCH"):
            self.store.allocate(
                "task",
                work_b.uid,
                _request("task-scope-mismatch", work_uid=work_a.uid),
            )
        session = self.store.allocate(
            "session",
            None,
            _request("scope-session", work_uid=work_a.uid),
        )
        with self.assertRaisesRegex(Exception, "ID_SCOPE_NOT_WORK"):
            self.store.allocate(
                "task",
                session.uid,
                _request("task-scope-session", work_uid=work_a.uid),
            )
        with self.assertRaisesRegex(Exception, "ID_SCOPE_NOT_FOUND"):
            self.store.allocate(
                "task",
                "missing-work",
                _request("task-scope-missing", work_uid="missing-work"),
            )

    def test_missing_or_empty_project_identity_is_rejected(self):
        module = importlib.import_module("gsp.identity_store")
        cases = (
            ("missing", None),
            ("empty", ""),
            ("unknown", "unknown"),
        )
        for name, project_id in cases:
            with self.subTest(name=name):
                root = self.project_root("project-" + name, project_id)
                store = module.IdentityStore(root)
                with self.assertRaisesRegex(Exception, "PROJECT_IDENTITY_INVALID"):
                    store.initialize()
                self.assertFalse(store.path.exists())

    def test_core_table_shapes_with_exact_guards_are_attested(self):
        module = importlib.import_module("gsp.identity_store")
        cases = [(table, mode) for table in ("operations", "events")
                 for mode in ("primary-key", "type", "not-null", "unique")]
        cases += [("events", "foreign-key"), ("operations", "check")]
        for table, mode in cases:
            with self.subTest(table=table, constraint=mode):
                root = self.project_root("shape-" + table + "-" + mode, "project-fixture")
                store = module.IdentityStore(root)
                store.initialize()
                self.assertEqual(store.authority_state()["state"], "ready")
                with closing(sqlite3.connect(store.path)) as db:
                    ddl = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
                    guards = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)).fetchall()
                    if mode == "primary-key":
                        bad = ddl.replace("PRIMARY KEY", "", 1)
                    elif mode == "type":
                        bad = ddl.replace("operation_uid TEXT", "operation_uid INTEGER", 1)
                    elif mode == "not-null":
                        column = "request_uid" if table == "operations" else "operation_uid"
                        bad = ddl.replace(column + " TEXT NOT NULL", column + " TEXT", 1)
                    elif mode == "unique":
                        key = "project_uid, logical_key" if table == "operations" else "operation_uid, semantic_key"
                        bad = ddl.replace("UNIQUE(" + key + ")", "CHECK(1)", 1)
                    elif mode == "foreign-key":
                        bad = ddl.replace("FOREIGN KEY(operation_uid) REFERENCES operations(operation_uid)", "CHECK(1)", 1)
                    else:
                        bad = ddl.replace("CHECK(phase IN (", "CHECK(1 OR phase IN (", 1)
                    self.assertNotEqual(ddl, bad)
                    db.execute("DROP TABLE " + table)
                    db.execute(bad)
                    for (guard,) in guards:
                        db.execute(guard)
                    db.commit()
                self.assertNotEqual(store.authority_state()["state"], "ready")
                with self.assertRaisesRegex(module.IdentityStoreError, "IDENTITY_AUTHORITY_CORRUPT"):
                    store.read_schema()

    def test_supported_old_v1_request_append_columns_remains_readable(self):
        import re
        before = self.store.read_schema()
        with closing(sqlite3.connect(self.store.path)) as db:
            ddl = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='requests'").fetchone()[0]
            guards = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name='requests'").fetchall()
            legacy = re.sub(r"\s*object_kind TEXT NOT NULL,", "", ddl, count=1)
            legacy = re.sub(r"\s*scope_uid TEXT NOT NULL,", "", legacy, count=1)
            self.assertNotEqual(ddl, legacy)
            db.execute("DROP TABLE requests")
            db.execute(legacy)
            db.execute("ALTER TABLE requests ADD COLUMN object_kind TEXT NOT NULL DEFAULT ''")
            db.execute("ALTER TABLE requests ADD COLUMN scope_uid TEXT NOT NULL DEFAULT ''")
            for (guard,) in guards:
                db.execute(guard)
            db.commit()
        self.assertEqual(self.store.authority_state()["state"], "ready")
        self.assertEqual(self.store.read_schema(), before)

    def test_schema_attestation_preserves_case_sensitive_sql_literals(self):
        module = importlib.import_module("gsp.identity_store")
        for kind in ("table-check", "trigger-message"):
            with self.subTest(kind=kind):
                root = self.project_root("sol-sql-literal-" + kind, "project-fixture")
                store = module.IdentityStore(root)
                store.initialize()
                with closing(sqlite3.connect(store.path)) as db:
                    if kind == "table-check":
                        ddl = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='operations'").fetchone()[0]
                        guards = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name='operations'").fetchall()
                        changed = ddl.replace("'intent'", "'INTENT'", 1)
                        self.assertNotEqual(ddl, changed)
                        db.execute("DROP TABLE operations")
                        db.execute(changed)
                        for (guard,) in guards:
                            db.execute(guard)
                    else:
                        sql = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='operations_immutable'").fetchone()[0]
                        changed = sql.replace("'OPERATION_IMMUTABLE'", "'operation_immutable'", 1)
                        self.assertNotEqual(sql, changed)
                        db.execute("DROP TRIGGER operations_immutable")
                        db.execute(changed)
                    db.commit()
                self.assertNotEqual(store.authority_state()["state"], "ready")
                with self.assertRaisesRegex(module.IdentityStoreError, "IDENTITY_AUTHORITY_CORRUPT"):
                    store.read_schema()

    def test_controller_transition_retry_is_idempotent_and_conflict_fails(self):
        old_uid = "controller-old"
        new_uid = "controller-new"
        self.store.record_session_state(old_uid, 1, 1, False)
        self.store.record_session_state(new_uid, 1, 1, False)
        first = self.store.transition_controller_bindings(
            old_uid, 1, new_uid, 1, 2,
        )
        second = self.store.transition_controller_bindings(
            old_uid, 1, new_uid, 1, 2,
        )
        self.assertEqual(second, first)
        self.assertEqual(self.store.read_session_state(old_uid), first["old"])
        self.assertEqual(self.store.read_session_state(new_uid), first["new"])
        with self.assertRaisesRegex(Exception, "CONTROL_VERSION_CHANGED"):
            self.store.transition_controller_bindings(
                old_uid, 1, new_uid, 1, 3,
            )
        self.assertEqual(self.store.read_session_state(old_uid), first["old"])
        self.assertEqual(self.store.read_session_state(new_uid), first["new"])



class IdentityCliIntegrationTests(unittest.TestCase):
    def setUp(self):
        from gsp.runtime import Project

        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-identity-cli-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = Project(self.root)

    def cli(self, *args, ok=True):
        from gsp.runtime import main

        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            try:
                code = main(["--project", str(self.root), *args])
            except SystemExit as exc:
                code = exc.code
        if ok:
            self.assertEqual(code, 0, output.getvalue() + errors.getvalue())
            return __import__("json").loads(output.getvalue())
        self.assertNotEqual(code, 0, output.getvalue() + errors.getvalue())
        return output.getvalue() + errors.getvalue()

    def test_new_work_item_uses_authoritative_sequence(self):
        import json
        from gsp.runtime import read

        self.cli("init")
        first = self.cli("work-new", "--title", "First", "--brief", "First goal",
                         "--source", "fixture")
        second = self.cli("work-new", "--title", "Second", "--brief", "Second goal",
                          "--source", "fixture")
        self.assertEqual((first["number"], second["number"]),
                         ("GSP-000001", "GSP-000002"))
        (self.root / ".gsp" / "work-counter.json").write_text(
            json.dumps({"next": 1}), encoding="utf-8")
        third = self.cli("work-new", "--title", "Third", "--brief", "Third goal",
                         "--source", "fixture")
        self.assertEqual(third["number"], "GSP-000003")
        self.assertEqual(self.cli("work-show", third["uuid"])["uuid"], third["uuid"])
        self.assertEqual(read(self.root / ".gsp/work-counter.json")["next"], 4)

    def test_old_project_without_authority_is_not_silently_reset(self):
        import json
        from gsp.runtime import atomic

        atomic(self.root / ".gsp/project.json", {
            "schema": 1, "id": "legacy-project", "root": str(self.root),
        })
        atomic(self.root / ".gsp/work-counter.json", {"next": 35})
        output = self.cli("work-new", "--title", "Legacy", "--brief", "Legacy goal",
                          "--source", "legacy", ok=False)
        self.assertIn("IDENTITY_MIGRATION_REQUIRED", output)
        self.assertFalse((self.root / ".gsp/orchestration/registry.sqlite").exists())
        self.assertEqual(json.loads(
            (self.root / ".gsp/work-counter.json").read_text(encoding="utf-8")
        )["next"], 35)

    def test_todo_uses_independent_namespace(self):
        from gsp.identity_store import IdentityStore
        from gsp.runtime import read
        from gsp.todos import Backlog

        self.cli("init")
        item = self.cli(
            "todo-add", "--title", "Independent", "--what", "Handle gap",
            "--why", "Avoid conflict", "--context", "Fixture", "--source",
            "fixture/review", "--priority", "P2", "--effort", "S",
        )["item"]
        self.assertEqual(item["id"], "TODO-000001")
        rows = Backlog(self.root).item(item["id"])
        self.assertEqual(rows["id"], "TODO-000001")
        resolved = IdentityStore(self.root).resolve("todo", item["id"], None, write=False)
        self.assertEqual(resolved.uid, item["object_uid"])
        record = read(self.root / ".gsp/backlog/records.json")["items"][item["id"]]
        self.assertEqual(record["object_uid"], item["object_uid"])

        IdentityStore(self.root).add_alias("todo", "GSP-0007", item["object_uid"], None)
        legacy = Backlog(self.root).item("todo:GSP-0007")
        self.assertEqual(legacy["id"], item["id"])

    def test_work_create_replays_the_same_logical_request(self):
        from gsp.documents import Governance

        self.cli("init")
        book = Governance(self.root)
        first = book.create(
            "Replayable work", "Create once", "fixture:replay", run_id="replay"
        )
        second = book.create(
            "Replayable work", "Create once", "fixture:replay", run_id="replay"
        )
        self.assertEqual(second["uuid"], first["uuid"])
        self.assertEqual(second["number"], first["number"])
        self.assertEqual(
            len(list((self.root / "docs/work").glob("*/**/record.json"))), 1
        )

    def test_work_create_operation_key_rejects_changed_input(self):
        from gsp.documents import Governance
        from gsp.identity_store import IdentityStore
        from gsp.runtime import WorkflowError

        self.cli("init")
        book = Governance(self.root)
        first = book.create(
            "Stable operation",
            "First input",
            "fixture:operation-key",
            run_id="stable-operation",
        )
        with self.assertRaisesRegex(WorkflowError, "REQUEST_INPUT_CHANGED"):
            book.create(
                "Changed operation",
                "Second input",
                "fixture:operation-key",
                run_id="stable-operation",
            )
        store = IdentityStore(self.root)
        self.assertEqual(store.count("work", None), 1)
        self.assertEqual(store.resolve(
            "work", first["number"], None, write=False
        ).canonical_id, first["number"])

    def test_work_run_id_number_forms_are_canonical_or_rejected_before_allocation(self):
        self.cli("init")
        from gsp.identity_store import IdentityStore
        from gsp.runtime import read

        store = IdentityStore(self.root)
        before = store.next_value("work", None)
        output = self.cli(
            "new",
            "GSP-00007",
            "--title",
            "Malformed explicit number",
            ok=False,
        )
        self.assertIn("WORK_NUMBER_INVALID", output)
        self.assertEqual(store.next_value("work", None), before)
        self.assertFalse(
            (self.root / ".gsp/runs/GSP-00007/state.json").exists()
        )

        created = self.cli(
            "new",
            "GSP-1000000",
            "--title",
            "Seven digit explicit number",
        )
        state = read(self.root / ".gsp/runs/GSP-1000000/state.json")
        self.assertEqual(created["task"], "GSP-1000000")
        self.assertEqual(state["number"], "GSP-1000000")

    def test_governance_first_use_initializes_authority_before_document_index(self):
        from gsp.documents import Governance

        root = Path(self.tmp.name) / "governance-first-use"
        (root / ".gsp").mkdir(parents=True)
        (root / ".gsp/project.json").write_text(
            json.dumps({"schema": 1, "id": "governance-first-use", "root": str(root)}),
            encoding="utf-8",
        )
        created = Governance(root).create("First work", "First goal", "fixture")
        self.assertEqual(created["number"], "GSP-000001")
        self.assertTrue((root / ".gsp/orchestration/registry.sqlite").is_file())

    def test_governance_without_project_identity_creates_no_authority_or_index(self):
        from gsp.documents import Governance
        from gsp.runtime import WorkflowError

        root = Path(self.tmp.name) / "governance-invalid-project"
        root.mkdir()
        with self.assertRaisesRegex(WorkflowError, "PROJECT_IDENTITY_INVALID"):
            Governance(root).create("Invalid", "No project", "fixture")
        self.assertFalse((root / ".gsp/orchestration/registry.sqlite").exists())
        self.assertFalse((root / ".gsp/documents.sqlite").exists())

    def test_project_initialize_rejects_missing_or_corrupt_authority(self):
        import json
        from gsp.runtime import Project, WorkflowError

        missing = Path(self.tmp.name) / "project-missing-authority"
        (missing / ".gsp").mkdir(parents=True)
        (missing / ".gsp/project.json").write_text(
            json.dumps({"id": "missing-authority", "root": str(missing)}),
            encoding="utf-8",
        )
        (missing / ".gsp/work-counter.json").write_text(
            json.dumps({"next": 35}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            WorkflowError, "IDENTITY_MIGRATION_REQUIRED"
        ):
            Project(missing).initialize()
        self.assertFalse(
            (missing / ".gsp/orchestration/registry.sqlite").exists()
        )

        corrupt = Path(self.tmp.name) / "project-corrupt-authority"
        (corrupt / ".gsp/orchestration").mkdir(parents=True)
        (corrupt / ".gsp/project.json").write_text(
            json.dumps({"id": "corrupt-authority", "root": str(corrupt)}),
            encoding="utf-8",
        )
        (corrupt / ".gsp/orchestration/registry.sqlite").write_bytes(
            b"not-a-sqlite-authority"
        )
        with self.assertRaisesRegex(
            WorkflowError, "IDENTITY_AUTHORITY_CORRUPT"
        ):
            Project(corrupt).initialize()


class ControlRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-control-root-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def git(self, root, *args):
        return subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()

    def repository(self, name="primary", kind="linked"):
        primary = self.base / name
        primary.mkdir()
        self.git(primary, "init", "-q", "-b", "main")
        self.git(primary, "config", "user.name", "Control Root Test")
        self.git(
            primary,
            "config",
            "user.email",
            "control-root@example.invalid",
        )
        (primary / "README.md").write_text("# fixture\n", encoding="utf-8")
        self.git(primary, "add", "README.md")
        self.git(primary, "commit", "-qm", "chore: fixture baseline")
        if kind == "linked":
            linked = self.base / (name + "-linked")
            self.git(
                primary,
                "worktree",
                "add",
                "-q",
                "-b",
                "feature/linked",
                str(linked),
            )
            self.addCleanup(
                lambda: subprocess.run(
                    ["git", "worktree", "remove", "--force", str(linked)],
                    cwd=primary,
                    capture_output=True,
                )
            )
            return primary, linked
        if kind == "separate":
            separate = self.base / (name + "-separate")
            separate.mkdir()
            self.git(
                primary,
                "init",
                "-q",
                "-b",
                "main",
                "--separate-git-dir",
                str(separate),
            )
            return primary, separate
        return primary, None

    def write_project_identity(self, root, project_id):
        meta = root / ".gsp"
        meta.mkdir(exist_ok=True)
        (meta / "project.json").write_text(
            json.dumps({"id": project_id, "root": str(root.resolve())}),
            encoding="utf-8",
        )

    def copy_git_marker(self, source, destination):
        marker = (source / ".git").read_text(encoding="utf-8")
        text = marker.removeprefix("gitdir: ").strip()
        path = Path(text)
        if not path.is_absolute():
            path = source / path
        destination.mkdir()
        (destination / ".git").write_text(
            "gitdir: " + str(path.resolve()) + "\n", encoding="utf-8"
        )

    def project_surfaces(self, root):
        return sorted(
            str(path.relative_to(root)).replace("\\", "/")
            for path in root.rglob("*")
            if path.is_file()
            and (
                path.name == "project.json"
                or path.name == "registry.sqlite"
                or path.name == "registry.json"
            )
        )

    def test_linked_first_refuses_before_either_root_is_initialized(self):
        from gsp.runtime import Project, WorkflowError

        primary, linked = self.repository()
        project = Project(linked)
        self.assertEqual(project.root, primary.resolve())
        self.assertEqual(project.invocation_root, linked.resolve())
        self.assertEqual(project.meta, primary.resolve() / ".gsp")
        with self.assertRaisesRegex(
            WorkflowError, "CONTROL_ROOT_UNINITIALIZED"
        ):
            project.initialize()
        self.assertEqual(self.project_surfaces(primary), [])
        self.assertEqual(self.project_surfaces(linked), [])

    def test_main_first_and_linked_retry_share_one_uid_and_authority(self):
        from gsp.runtime import Project, read

        primary, linked = self.repository()
        primary_project = Project(primary)
        primary_project.initialize()
        linked_project = Project(linked)
        linked_project.initialize()
        self.assertEqual(linked_project.root, primary.resolve())
        self.assertEqual(linked_project.invocation_root, linked.resolve())
        self.assertEqual(
            read(linked_project.meta / "project.json")["id"],
            read(primary_project.meta / "project.json")["id"],
        )
        self.assertEqual(
            len(list((primary / ".gsp").rglob("registry.sqlite"))),
            1,
        )
        self.assertEqual(self.project_surfaces(linked), [])

    def test_separate_git_dir_single_checkout_keeps_its_actual_root(self):
        from gsp.runtime import Project

        checkout, _ = self.repository(kind="separate")
        project = Project(checkout)
        self.assertEqual(project.root, checkout.resolve())
        self.assertEqual(project.invocation_root, checkout.resolve())
        project.initialize()
        self.assertEqual(project.root, checkout.resolve())

    def test_linked_tree_with_split_identity_and_allocator_is_refused(self):
        from gsp.runtime import Project, WorkflowError

        primary, linked = self.repository()
        (linked / ".gsp/orchestration").mkdir(parents=True)
        (linked / ".gsp/project.json").write_text(
            json.dumps({"id": "split-project", "root": str(linked.resolve())}),
            encoding="utf-8",
        )
        (linked / ".gsp/orchestration/registry.sqlite").write_bytes(
            b"split-authority"
        )
        with self.assertRaisesRegex(
            WorkflowError, "CONTROL_ROOT_SPLIT_AUTHORITY"
        ):
            Project(linked)
        self.assertFalse((linked / ".gsp/project.json").read_text(
            encoding="utf-8"
        ) == "")

    def test_initialized_primary_never_hides_linked_split_identity_or_allocator(self):
        from gsp.runtime import Project, WorkflowError

        primary, linked = self.repository(name="initialized-split")
        Project(primary).initialize()
        self.write_project_identity(linked, "linked-split-id")
        with self.assertRaisesRegex(
            WorkflowError, "CONTROL_ROOT_SPLIT_AUTHORITY"
        ):
            Project(linked)
        (linked / ".gsp/orchestration").mkdir()
        (linked / ".gsp/orchestration/registry.sqlite").write_bytes(
            b"linked-split-allocator"
        )
        with self.assertRaisesRegex(
            WorkflowError, "CONTROL_ROOT_SPLIT_AUTHORITY"
        ):
            Project(linked)

    def test_unregistered_invocation_cannot_borrow_a_git_marker(self):
        from gsp.runtime import Project, WorkflowError

        primary, linked = self.repository(name="borrowed-marker")
        Project(primary).initialize()
        borrowed = self.base / "borrowed-marker-copy"
        self.copy_git_marker(linked, borrowed)
        with self.assertRaisesRegex(
            WorkflowError, "CONTROL_ROOT_TOPOLOGY_AMBIGUOUS"
        ):
            Project(borrowed)

    def test_registered_linked_inside_primary_state_directory_remains_supported(self):
        from gsp.runtime import Project, read

        primary, _ = self.repository(name="managed-linked")
        Project(primary).initialize()
        managed = primary / ".gsp/worktrees/feature/managed"
        managed.parent.mkdir(parents=True)
        self.git(
            primary,
            "worktree",
            "add",
            "-q",
            "-b",
            "feature/managed",
            str(managed),
        )
        project = Project(managed)
        project.initialize()
        self.assertEqual(project.root, primary.resolve())
        self.assertEqual(project.invocation_root, managed.resolve())
        self.assertEqual(
            read(project.meta / "project.json")["id"],
            read(primary / ".gsp/project.json")["id"],
        )
        self.assertFalse((managed / ".gsp/project.json").exists())

    def test_submodule_and_nested_repository_fail_closed_before_identity(self):
        from gsp.runtime import Project, WorkflowError

        primary, _ = self.repository(name="special-topology")
        nested = primary / "nested"
        nested.mkdir()
        self.git(nested, "init", "-q", "-b", "main")
        with self.assertRaisesRegex(
            WorkflowError, "CONTROL_ROOT_TOPOLOGY_UNSUPPORTED"
        ):
            Project(nested)
        self.assertFalse((nested / ".gsp/project.json").exists())

        source = self.base / "submodule-source"
        source.mkdir()
        self.git(source, "init", "-q", "-b", "main")
        self.git(source, "config", "user.name", "Control Root Test")
        self.git(
            source, "config", "user.email", "control-root@example.invalid"
        )
        (source / "README.md").write_text("# submodule\n", encoding="utf-8")
        self.git(source, "add", "README.md")
        self.git(source, "commit", "-qm", "chore: submodule baseline")
        self.git(
            primary,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(source),
            "module",
        )
        submodule = primary / "module"
        with self.assertRaisesRegex(
            WorkflowError, "CONTROL_ROOT_TOPOLOGY_UNSUPPORTED"
        ):
            Project(submodule)
        self.assertFalse((submodule / ".gsp/project.json").exists())

    def test_bare_and_ambiguous_primary_topology_fail_closed(self):
        from gsp.runtime import Project, WorkflowError

        bare = self.base / "bare.git"
        self.git(self.base, "init", "--bare", "-q", str(bare))
        with self.assertRaisesRegex(
            WorkflowError, "CONTROL_ROOT_TOPOLOGY_UNSUPPORTED"
        ):
            Project(bare)

        primary, linked = self.repository(name="ambiguous")
        (primary / ".gsp").mkdir()
        (primary / ".gsp/project.json").write_text(
            json.dumps({"id": "unexpected", "root": str(linked.resolve())}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            WorkflowError, "CONTROL_ROOT_TOPOLOGY_AMBIGUOUS"
        ):
            Project(linked)

    def test_concurrent_primary_and_linked_init_converge_or_retry_safely(self):
        from gsp.runtime import Project

        primary, linked = self.repository(name="concurrent")
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        queue = context.Queue()
        processes = [
            context.Process(
                target=_initialize_project_in_child,
                args=(str(root), start, queue),
            )
            for root in (primary, linked)
        ]
        for process in processes:
            process.start()
        start.set()
        first = queue.get(timeout=30)
        second = queue.get(timeout=30)
        for process in processes:
            process.join(30)
            self.assertEqual(process.exitcode, 0)
        results = (first, second)
        self.assertIn(("ok", primary.resolve()), results)
        failures = [row for row in results if row[0] == "error"]
        if failures:
            self.assertEqual(failures[0][1], "WorkflowError")
            self.assertIn("CONTROL_ROOT_UNINITIALIZED", failures[0][2])
            Project(linked).initialize()
        self.assertEqual(
            Project(primary).root,
            Project(linked).root,
        )
        self.assertEqual(
            len(list((primary / ".gsp").rglob("registry.sqlite"))),
            1,
        )
        self.assertEqual(self.project_surfaces(linked), [])

    def test_non_git_project_does_not_require_an_executable_git(self):
        from gsp.runtime import Project

        root = self.base / "plain-project"
        root.mkdir()
        with patch("gsp.runtime._git_run", side_effect=FileNotFoundError("git")):
            project = Project(root)
            project.initialize()
        self.assertEqual(project.root, root.resolve())
        self.assertTrue((root / ".gsp/project.json").is_file())

    def test_normal_status_brief_entrypoint_launches_no_git(self):
        from gsp.runtime import Project, main, now

        root, _ = self.repository(name="brief-project", kind="ordinary")
        Project(root).initialize()
        state_file = root / ".gsp/runs/work/state.json"
        state_file.parent.mkdir(parents=True)
        ident = json.loads(
            (root / ".gsp/project.json").read_text(encoding="utf-8")
        )
        state_file.write_text(
            json.dumps(
                {
                    "id": "work",
                    "root": str(root.resolve()),
                    "project_id": ident["id"],
                    "status": "design",
                    "updated": now(),
                    "tasks": [],
                    "evidence": [],
                    "inflight": None,
                }
            ),
            encoding="utf-8",
        )
        output = io.StringIO()
        with patch(
            "gsp.runtime._git_run",
            side_effect=AssertionError("brief launched Git"),
        ):
            with redirect_stdout(output):
                code = main(["--project", str(root), "status", "work", "--brief"])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["validation"], "NOT_CHECKED")
        self.assertTrue(result["not_authoritative"])



class SolControlRootBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gsp-sol-root-")
        self.addCleanup(self.temp.cleanup)
        self.area = Path(self.temp.name)

    def git(self, root, *args):
        result = subprocess.run(["git", "-C", str(root), *args],
                                capture_output=True, text=True)
        if result.returncode:
            self.fail(f"Git fixture {args!r} failed ({result.returncode}): {result.stderr}\n{result.stdout}")
        return result

    def repo(self, name):
        root = self.area / name
        root.mkdir()
        self.git(root, "init")
        self.git(root, "config", "user.email", "fixture@example.invalid")
        self.git(root, "config", "user.name", "GSP fixture")
        (root / "seed.txt").write_text("seed", encoding="utf-8")
        self.git(root, "add", "seed.txt")
        self.git(root, "commit", "-m", "fixture")
        return root

    def linked(self, primary, name, managed=False):
        path = primary / ".gsp/worktrees" / name if managed else self.area / name
        self.git(primary, "worktree", "add", "-b", name, str(path))
        return path

    def test_legacy_local_allocator_surfaces_are_never_hidden(self):
        from gsp.runtime import Project, WorkflowError
        primary = self.repo("primary")
        Project(primary).initialize()
        for index, relative in enumerate(["work-counter.json", "backlog/records.json", "runs/old/state.json"]):
            with self.subTest(surface=relative):
                linked = self.linked(primary, "legacy" + str(index))
                state = linked / ".gsp" / relative
                state.parent.mkdir(parents=True, exist_ok=True)
                state.write_text("{}", encoding="utf-8")
                with self.assertRaisesRegex(WorkflowError, "CONTROL_ROOT_SPLIT_AUTHORITY"):
                    Project(linked)
                self.assertFalse((linked / ".gsp/project.json").exists())

    def test_fabricated_git_head_cannot_initialize_authority(self):
        from gsp.runtime import Project, WorkflowError
        root = self.area / "fabricated"
        (root / ".git").mkdir(parents=True)
        (root / ".git/HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "CONTROL_ROOT_TOPOLOGY"):
            Project(root).initialize()
        self.assertFalse((root / ".gsp").exists())

    def test_submodule_in_registered_linked_tree_is_rejected(self):
        from gsp.runtime import Project, WorkflowError
        primary = self.repo("primary")
        module = self.repo("module-source")
        linked = self.linked(primary, "linked")
        self.git(linked, "-c", "protocol.file.allow=always", "submodule", "add", str(module), "child")
        with self.assertRaisesRegex(WorkflowError, "CONTROL_ROOT_TOPOLOGY"):
            Project(linked / "child").initialize()
        self.assertFalse((linked / "child/.gsp").exists())

    def test_native_temp_isolation_ceiling_is_honored(self):
        from gsp.runtime import Project, WorkflowError
        outer = self.repo("ceiling-outer")
        boundary = outer / ".gsp/tmp/run"
        boundary.mkdir(parents=True)
        plain = boundary / "plain-fixture"
        plain.mkdir()
        with self.assertRaisesRegex(WorkflowError, "CONTROL_ROOT_TOPOLOGY"):
            Project(plain)
        ceilings = {"GIT_CEILING_DIRECTORIES": str(boundary),
                    "GSP_CEILING_DIRECTORIES": str(boundary)}
        previous = {key: os.environ.get(key) for key in ceilings}
        try:
            os.environ.update(ceilings)
            Project(plain).initialize()
            self.assertTrue((plain / ".gsp/project.json").is_file())
            git_fixture = self.repo("ceiling-outer/.gsp/tmp/run/git-fixture")
            Project(git_fixture).initialize()
            self.assertTrue((git_fixture / ".gsp/project.json").is_file())
        finally:
            # Windows putenv of an empty value deletes that native variable.
            # Never clear/rebuild unrelated host GIT_CONFIG_* environment entries.
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.git(git_fixture, "status", "--porcelain")
        with self.assertRaisesRegex(WorkflowError, "CONTROL_ROOT_TOPOLOGY"):
            Project(git_fixture)

    def test_normal_linked_brief_entry_reads_ledger_without_git_or_constructor(self):
        import io, json
        from contextlib import redirect_stdout
        from gsp.runtime import Project, main
        primary = self.repo("primary")
        project = Project(primary)
        project.initialize()
        ident = project.identity()
        record = {"id": "work", "root": str(primary), "project_id": ident["id"],
                  "status": "execute", "tasks": [], "evidence": []}
        path = primary / ".gsp/runs/work/state.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        for name, managed in [("external-linked", False), ("managed-linked", True)]:
            linked = self.linked(primary, name, managed)
            output = io.StringIO()
            with patch("gsp.runtime._git_run", side_effect=AssertionError("brief launched Git")), \
                 patch("gsp.runtime.Project", side_effect=AssertionError("brief constructed Project")), \
                 redirect_stdout(output):
                self.assertEqual(main(["--project", str(linked), "status", "work", "--brief"]), 0)
            result = json.loads(output.getvalue())
            self.assertTrue(result["not_authoritative"])
            self.assertEqual(result["validation"], "NOT_CHECKED")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), record)


    def test_storage_escape_is_rejected_for_every_root_layout_and_brief(self):
        from gsp.runtime import Project, WorkflowError, main
        for layout in ("plain", "git", "separate"):
            for relative in (".gsp", "docs/gsp"):
                with self.subTest(layout=layout, storage=relative):
                    name = layout + "-" + relative.replace("/", "-").replace(".", "")
                    root = self.area / name
                    root.mkdir()
                    if layout == "git":
                        self.git(root, "init")
                    elif layout == "separate":
                        self.git(root, "init", "--separate-git-dir=" + str(self.area / (name + "-git")))
                    outside = self.area / (name + "-outside")
                    outside.mkdir()
                    link = root / relative
                    link.parent.mkdir(parents=True, exist_ok=True)
                    if os.name == "nt":
                        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
                        script = ("New-Item -ItemType Junction -Path " + quote(link)
                                  + " -Target " + quote(outside) + " -ErrorAction Stop | Out-Null")
                        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                                capture_output=True, text=True, timeout=30,
                                                creationflags=subprocess.CREATE_NO_WINDOW)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    else:
                        link.symlink_to(outside, target_is_directory=True)
                    with self.assertRaisesRegex(WorkflowError, "PROJECT_PATH_ESCAPE"):
                        Project(root)
                    errors = io.StringIO()
                    with patch("gsp.runtime._git_run", side_effect=AssertionError("brief launched Git")), \
                         patch("gsp.runtime.Project", side_effect=AssertionError("brief constructed Project")), \
                         redirect_stderr(errors):
                        self.assertEqual(main(["--project", str(root), "status", "work", "--brief"]), 1)
                    self.assertIn("PROJECT_PATH_ESCAPE", errors.getvalue())
                    self.assertEqual(list(outside.iterdir()), [])

    def test_invocation_equal_to_ceiling_does_not_discover_parent_repository(self):
        from gsp.runtime import Project, WorkflowError
        outer = self.repo("ceiling-equal-outer")
        boundary = outer / ".gsp/tmp/equal-boundary"
        boundary.mkdir(parents=True)
        self.git(boundary, "init")
        ceilings = {"GIT_CEILING_DIRECTORIES": str(boundary),
                    "GSP_CEILING_DIRECTORIES": str(boundary)}
        previous = {key: os.environ.get(key) for key in ceilings}
        try:
            os.environ.update(ceilings)
            project = Project(boundary)
            self.assertEqual(project.root, boundary.resolve())
            plain = boundary / "plain"
            plain.mkdir()
            Project(plain).initialize()
            self.assertTrue((plain / ".gsp/project.json").is_file())
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        with self.assertRaisesRegex(WorkflowError, "CONTROL_ROOT_TOPOLOGY"):
            Project(boundary)

if __name__ == "__main__":
    unittest.main()
