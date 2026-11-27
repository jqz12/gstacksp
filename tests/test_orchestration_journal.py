"""Durable intent, state CAS, outbox, and shared-document merge tests."""
from contextlib import closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import uuid

from gsp.documents import Governance
from gsp.identity_store import IdentityStore, IdentityStoreError
from gsp.operation_journal import (
    begin_intent,
    commit_state,
    emit_event,
    get_operation,
    merge_shared_document,
    recover_operation,
    state_digest,
)
from gsp.orchestration_types import (
    DocumentEdit,
    EventEnvelope,
    RequestRef,
    SessionBinding,
    digest_text,
    digest_value,
)
from gsp.runtime import Project, WorkflowError, atomic, digest, read
from gsp.session_context import record_binding_authoritative
from gsp.todos import Backlog, field_digest


def canonical_state_digest(value):
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request(
    project_uid,
    logical_key,
    *,
    input_value="same input",
    work_uid=None,
    attempt_uid="attempt-a",
    operation_kind="state-write",
    request_uid=None,
    controller_generation=None,
    cancel_generation=None,
    contract_uid=None,
    contract_version=None,
    contract_digest=None,
):
    return RequestRef(
        request_uid=request_uid or uuid.uuid4().hex,
        logical_key=logical_key,
        input_digest=digest_text(input_value),
        project_uid=project_uid,
        work_uid=work_uid,
        attempt_uid=attempt_uid,
        operation_kind=operation_kind,
        controller_generation=controller_generation,
        cancel_generation=cancel_generation,
        contract_uid=contract_uid,
        contract_version=contract_version,
        contract_digest=contract_digest,
    )


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-journal-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".gsp").mkdir(parents=True)
        (self.root / ".gsp/project.json").write_text(
            json.dumps({"id": "journal-project", "root": str(self.root)}),
            encoding="utf-8",
        )
        self.store = IdentityStore(self.root)
        self.store.initialize()
        self.work = self.store.allocate(
            "work",
            None,
            request(
                self.store.project_uid(),
                "journal-work",
                operation_kind="allocate-work",
            ),
        )
        self.session = self.store.allocate(
            "session",
            None,
            request(
                self.store.project_uid(),
                "journal-session",
                work_uid=self.work.uid,
                operation_kind="allocate-session",
            ),
        )
        record_binding_authoritative(self.store, self.binding())

    def journal_request(self, logical_key, **kwargs):
        return request(
            self.store.project_uid(),
            logical_key,
            work_uid=self.work.uid,
            controller_generation=2,
            cancel_generation=3,
            contract_uid="contract-1",
            contract_version=1,
            contract_digest="a" * 64,
            **kwargs,
        )

    def state_file(self, name="work"):
        path = self.root / ".gsp" / "runs" / name / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def begin_state_operation(self, logical_key="state-one", name="work"):
        path = self.state_file(name)
        initial = {"status": "execute", "number": 1}
        atomic(path, initial)
        expected = state_digest(path)
        operation = begin_intent(
            self.store,
            self.journal_request(logical_key),
            expected,
        )
        return path, initial, expected, operation

    def binding(self):
        return SessionBinding(
            binding_uid=self.session.uid + "-binding",
            session=self.session,
            work_uid=self.work.uid,
            kind="main",
            host="codex",
            host_thread_id="thread-fixture",
            host_state="unverified",
            controller_generation=2,
            cancel_generation=3,
            capabilities=frozenset({"write"}),
            cancelled=False,
            created_at="2026-11-25T00:00:00+00:00",
            project_uid=self.store.project_uid(),
            common_dir=str(self.root),
            handle_path=".gsp/runs/work/sessions/S01.json",
        )

    def backlog_item(self):
        backlog = Backlog(self.root)
        backlog.initialize()
        created = backlog.add(
            SimpleNamespace(
                title="Shared backlog",
                what="Original behavior",
                why="Cover merge",
                context="Fixture",
                priority="P2",
                effort="S",
                source="journal-fixture",
                origin="orchestration",
                depends_on="",
                blocked_by="",
                component="项目",
                blocking=False,
            )
        )
        return backlog, backlog.item(created["item"]["id"])

    def test_same_logical_intent_replays_and_changed_input_is_rejected(self):
        path, _, expected, first = self.begin_state_operation("strict-intent")
        second = begin_intent(
            self.store,
            self.journal_request("strict-intent"),
            expected,
        )
        self.assertEqual(first.operation_uid, second.operation_uid)
        self.assertEqual(get_operation(self.store, first.operation_uid), first)
        self.assertEqual(self.store.operation_count(), 1)

        changed = self.journal_request("strict-intent", input_value="changed input")
        with self.assertRaisesRegex(IdentityStoreError, "REQUEST_INPUT_CHANGED"):
            begin_intent(self.store, changed, expected)
        self.assertEqual(read(path), {"status": "execute", "number": 1})

    def test_commit_state_uses_sha_cas_and_replays_committed_file(self):
        path, _, expected, operation = self.begin_state_operation("cas-commit")
        new_state = {"status": "checking", "number": 2}
        committed = commit_state(
            self.store,
            operation.operation_uid,
            path,
            new_state,
        )
        self.assertEqual(committed.phase, "committed")
        self.assertEqual(get_operation(self.store, operation.operation_uid), committed)
        self.assertEqual(read(path), new_state)
        self.assertEqual(recover_operation(self.store, operation.operation_uid), committed)

        stale = begin_intent(
            self.store,
            self.journal_request("cas-stale"),
            expected,
        )
        with self.assertRaisesRegex(IdentityStoreError, "STATE_CAS_MISMATCH"):
            commit_state(
                self.store,
                stale.operation_uid,
                path,
                {"status": "blocked", "number": 3},
            )
        self.assertEqual(read(path), new_state)

    def test_file_written_before_journal_commit_recovers_from_state_file(self):
        path, _, _, operation = self.begin_state_operation("file-first")
        new_state = {"status": "review", "number": 9}
        new_digest = self.store.prepare_operation(
            operation.operation_uid,
            path,
            canonical_state_digest(new_state),
        )
        atomic(path, new_state)
        self.assertEqual(state_digest(path), new_digest)

        replayed = begin_intent(
            self.store,
            self.journal_request("file-first"),
            state_digest(path),
        )
        self.assertEqual(replayed.operation_uid, operation.operation_uid)
        recovered = commit_state(
            self.store,
            operation.operation_uid,
            path,
            new_state,
        )
        self.assertEqual(recovered.phase, "committed")
        self.assertEqual(recovered.result_ref, new_digest)
        self.assertEqual(read(path), new_state)

    def test_event_projection_query_is_state_path_scoped_and_read_only(self):
        first_path, _, _, first_operation = self.begin_state_operation(
            "projection-first", name="projection-first"
        )
        second_path, _, _, second_operation = self.begin_state_operation(
            "projection-second", name="projection-second"
        )
        commit_state(
            self.store,
            first_operation.operation_uid,
            first_path,
            {"status": "review", "number": 11},
        )
        commit_state(
            self.store,
            second_operation.operation_uid,
            second_path,
            {"status": "review", "number": 12},
        )
        common = {
            "kind": "state-transition",
            "project_uid": self.store.project_uid(),
            "work_uid": self.work.uid,
            "task_uid": None,
            "dispatch_uid": None,
            "attempt_uid": "attempt-a",
            "contract_uid": "contract-1",
            "contract_version": 1,
            "contract_digest": "a" * 64,
            "controller_generation": 2,
            "cancel_generation": 3,
        }
        first_event = EventEnvelope(
            event_uid=uuid.uuid4().hex,
            payload={"event": "first", "detail": "one"},
            **common,
        )
        second_event = EventEnvelope(
            event_uid=uuid.uuid4().hex,
            payload={"event": "second", "detail": "two"},
            **common,
        )
        emit_event(self.store, first_operation.operation_uid, first_event)
        emit_event(self.store, second_operation.operation_uid, second_event)
        before = self.store.event_count()
        projection = self.store.event_projection(first_path)
        again = self.store.event_projection(first_path)
        self.assertEqual(projection, again)
        self.assertEqual([row["event_uid"] for row in projection], [first_event.event_uid])
        self.assertEqual(projection[0]["payload"]["event"], "first")
        self.assertEqual(self.store.event_count(), before)

    def test_state_file_remains_authoritative_after_journal_commit(self):
        path, _, _, operation = self.begin_state_operation("state-authority")
        committed = commit_state(
            self.store,
            operation.operation_uid,
            path,
            {"status": "ready", "number": 4},
        )
        atomic(path, {"status": "blocked", "number": 4})
        with self.assertRaisesRegex(IdentityStoreError, "STATE_AUTHORITY_MISMATCH"):
            recover_operation(self.store, committed.operation_uid)
        self.assertEqual(read(path), {"status": "blocked", "number": 4})

    def test_outbox_deduplicates_and_rejects_cross_context_events(self):
        path, _, _, operation = self.begin_state_operation("event-operation")
        commit_state(
            self.store,
            operation.operation_uid,
            path,
            {"status": "review", "number": 5},
        )
        common = dict(
            kind="state-transition",
            project_uid=self.store.project_uid(),
            work_uid=self.work.uid,
            task_uid=None,
            dispatch_uid=None,
            attempt_uid="attempt-a",
            contract_uid="contract-1",
            contract_version=1,
            contract_digest="a" * 64,
            controller_generation=2,
            cancel_generation=3,
            payload={"to": "review"},
        )
        event = EventEnvelope(event_uid=uuid.uuid4().hex, **common)
        first = emit_event(self.store, operation.operation_uid, event)
        second = emit_event(self.store, operation.operation_uid, event)
        duplicate_uid = EventEnvelope(event_uid=uuid.uuid4().hex, **common)
        third = emit_event(self.store, operation.operation_uid, duplicate_uid)
        self.assertEqual(first, second)
        self.assertEqual(first, third)
        self.assertEqual(self.store.event_count(), 1)

        changes = {
            "cross-project": {"project_uid": "other-project"},
            "cross-work": {"work_uid": "other-work"},
            "old-generation": {"controller_generation": 1},
            "old-cancel-generation": {"cancel_generation": 2},
            "wrong-contract": {"contract_digest": "b" * 64},
            "forged-task": {"task_uid": "forged-task"},
            "forged-dispatch": {"dispatch_uid": "forged-dispatch"},
        }
        expected_codes = {
            "cross-project": "EVENT_PROJECT_MISMATCH",
            "cross-work": "EVENT_WORK_MISMATCH",
            "old-generation": "CONTROL_VERSION_CHANGED",
            "old-cancel-generation": "CONTROL_VERSION_CHANGED",
            "wrong-contract": "CONTRACT_CHANGED",
            "forged-task": "EVENT_TASK_UNVERIFIED",
            "forged-dispatch": "EVENT_DISPATCH_UNVERIFIED",
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                candidate = dict(common)
                candidate.update(change)
                with self.assertRaisesRegex(IdentityStoreError, expected_codes[name]):
                    emit_event(
                        self.store,
                        operation.operation_uid,
                        EventEnvelope(event_uid=uuid.uuid4().hex, **candidate),
                    )

        changed_payload = dict(common)
        changed_payload["payload"] = {"to": "ready"}
        with self.assertRaisesRegex(IdentityStoreError, "EVENT_CONFLICT"):
            emit_event(
                self.store,
                operation.operation_uid,
                EventEnvelope(event_uid=event.event_uid, **changed_payload),
            )

    def test_operation_and_event_guards_block_tampering(self):
        path, _, _, operation = self.begin_state_operation("guarded-operation")
        commit_state(
            self.store,
            operation.operation_uid,
            path,
            {"status": "review", "number": 6},
        )
        event = EventEnvelope(
            event_uid=uuid.uuid4().hex,
            kind="state-transition",
            project_uid=self.store.project_uid(),
            work_uid=self.work.uid,
            task_uid=None,
            dispatch_uid=None,
            attempt_uid="attempt-a",
            contract_uid="contract-1",
            contract_version=1,
            contract_digest="a" * 64,
            controller_generation=2,
            cancel_generation=3,
            payload={"to": "review"},
        )
        emit_event(self.store, operation.operation_uid, event)
        with closing(sqlite3.connect(self.store.path)) as db:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "OPERATION_IMMUTABLE"):
                db.execute(
                    "UPDATE operations SET input_digest='forged' WHERE operation_uid=?",
                    (operation.operation_uid,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "OPERATION_PHASE_REWIND"):
                db.execute(
                    "UPDATE operations SET phase='intent' WHERE operation_uid=?",
                    (operation.operation_uid,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "OPERATION_NO_DELETE"):
                db.execute(
                    "DELETE FROM operations WHERE operation_uid=?",
                    (operation.operation_uid,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "EVENT_IMMUTABLE"):
                db.execute(
                    "UPDATE events SET payload_json='{}' WHERE event_uid=?",
                    (event.event_uid,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "EVENT_NO_DELETE"):
                db.execute("DELETE FROM events WHERE event_uid=?", (event.event_uid,))
            db.rollback()
        self.assertEqual(self.store.operation_count(), 1)
        self.assertEqual(self.store.event_count(), 1)

    def test_shared_todo_disjoint_edits_merge_after_stale_version(self):
        backlog, item = self.backlog_item()
        stale_version = digest(backlog.file)
        first = merge_shared_document(
            self.store,
            self.binding(),
            backlog.file,
            stale_version,
            [
                DocumentEdit(
                    target_uid=item["object_uid"],
                    field="what",
                    before_digest=field_digest(item["what"]),
                    new_value="First concurrent edit",
                    operation="update",
                )
            ],
        )
        second = merge_shared_document(
            self.store,
            self.binding(),
            backlog.file,
            stale_version,
            [
                DocumentEdit(
                    target_uid=item["object_uid"],
                    field="why",
                    before_digest=field_digest(item["why"]),
                    new_value="Second concurrent edit",
                    operation="update",
                )
            ],
        )
        self.assertEqual(first.phase, "committed")
        self.assertEqual(second.phase, "committed")
        merged = backlog.item(item["id"])
        self.assertEqual(merged["what"], "First concurrent edit")
        self.assertEqual(merged["why"], "Second concurrent edit")

    def test_shared_document_rejects_cached_binding_after_controller_transition(self):
        backlog, item = self.backlog_item()
        version = digest(backlog.file)
        stale = self.binding()
        new_session = self.store.allocate(
            "session",
            None,
            request(
                self.store.project_uid(),
                "journal-session-next",
                work_uid=self.work.uid,
                operation_kind="allocate-session",
            ),
        )
        new = replace(
            stale,
            binding_uid=new_session.uid + "-binding",
            session=new_session,
            host_thread_id="thread-next",
            handle_path=".gsp/runs/work/sessions/S02.json",
        )
        record_binding_authoritative(self.store, new)
        self.store.transition_controller_bindings(
            stale.binding_uid, stale.controller_generation,
            new.binding_uid, new.controller_generation,
            stale.controller_generation + 1,
        )
        operations = [
            DocumentEdit(
                target_uid=item["object_uid"],
                field="what",
                before_digest=field_digest(item["what"]),
                new_value="Stale writer must not merge",
                operation="update",
            )
        ]
        with self.assertRaisesRegex(
            IdentityStoreError, "SESSION_BINDING_STALE|CONTROL_VERSION_CHANGED",
        ):
            merge_shared_document(
                self.store, stale, backlog.file, version, operations,
            )
        self.assertEqual(backlog.item(item["id"])["what"], item["what"])
        self.assertEqual(self.store.operation_count(), 0)

    def test_normal_transition_rejects_reloaded_cancelled_binding_without_mutation(self):
        backlog, item = self.backlog_item()
        version = digest(backlog.file)
        stale = self.binding()
        new_session = self.store.allocate(
            "session",
            None,
            request(
                self.store.project_uid(),
                "journal-session-reloaded",
                work_uid=self.work.uid,
                operation_kind="allocate-session",
            ),
        )
        new = replace(
            stale,
            binding_uid=new_session.uid + "-binding",
            session=new_session,
            host_thread_id="thread-reloaded",
            handle_path=".gsp/runs/work/sessions/S02.json",
        )
        record_binding_authoritative(self.store, new)
        transition = self.store.transition_controller_bindings(
            stale.binding_uid, stale.controller_generation,
            new.binding_uid, new.controller_generation,
            stale.controller_generation + 1,
        )
        reloaded_stale = replace(
            stale,
            controller_generation=transition["old"]["controller_generation"],
            cancel_generation=transition["old"]["cancel_generation"],
            cancelled=transition["old"]["cancelled"],
        )
        self.assertTrue(reloaded_stale.cancelled)
        operations = [
            DocumentEdit(
                target_uid=item["object_uid"],
                field="what",
                before_digest=field_digest(item["what"]),
                new_value="Cancelled writer must not merge",
                operation="update",
            )
        ]
        before_document = backlog.file.read_bytes()
        before_operations = self.store.operation_count()
        before_events = self.store.event_count()
        with self.assertRaisesRegex(IdentityStoreError, "SESSION_BINDING_STALE"):
            merge_shared_document(
                self.store, reloaded_stale, backlog.file, version, operations,
            )
        self.assertEqual(backlog.file.read_bytes(), before_document)
        self.assertEqual(backlog.item(item["id"])["what"], item["what"])
        self.assertEqual(self.store.operation_count(), before_operations)
        self.assertEqual(self.store.event_count(), before_events)

    def test_shared_document_commit_lost_response_is_recoverable(self):
        backlog, item = self.backlog_item()
        stale_version = digest(backlog.file)
        operations = [
            DocumentEdit(
                target_uid=item["object_uid"],
                field="context",
                before_digest=field_digest(item["context"]),
                new_value="Published before the response was lost",
                operation="update",
            )
        ]
        original_commit = Backlog.commit

        def publish_then_raise(instance, text, records, event, operation_uid=None):
            original_commit(instance, text, records, event,
                            operation_uid=operation_uid)
            raise OSError("response lost after publication")

        with patch.object(Backlog, "commit", publish_then_raise):
            with self.assertRaisesRegex(OSError, "response lost"):
                merge_shared_document(
                    self.store,
                    self.binding(),
                    backlog.file,
                    stale_version,
                    operations,
                )
        self.assertEqual(
            backlog.item(item["id"])["context"],
            "Published before the response was lost",
        )
        recovered = merge_shared_document(
            self.store,
            self.binding(),
            backlog.file,
            stale_version,
            operations,
        )
        self.assertEqual(recovered.phase, "committed")
        self.assertEqual(self.store.operation_count(), 1)

    def test_shared_todo_pending_transaction_recovers_before_journal_commit(self):
        backlog, item = self.backlog_item()
        stale_version = digest(backlog.file)
        operations = [
            DocumentEdit(
                target_uid=item["object_uid"],
                field="what",
                before_digest=field_digest(item["what"]),
                new_value="Recovered backlog field",
                operation="update",
            )
        ]

        def fail_records(path, value):
            if Path(path).resolve() == backlog.records_file.resolve():
                raise OSError("records projection interrupted")
            return atomic(path, value)

        with patch("gsp.todos.atomic", side_effect=fail_records):
            with self.assertRaisesRegex(OSError, "records projection"):
                merge_shared_document(
                    self.store,
                    self.binding(),
                    backlog.file,
                    stale_version,
                    operations,
                )
        self.assertTrue(backlog.transaction.exists())
        with closing(sqlite3.connect(self.store.path)) as db:
            phase = db.execute(
                "SELECT phase FROM operations WHERE state_path=?",
                (backlog.file.relative_to(self.root).as_posix(),),
            ).fetchone()[0]
        self.assertIn(phase, ("prepared", "file-written"))
        with self.assertRaisesRegex(WorkflowError, "BACKLOG_RECOVERY_REQUIRED"):
            backlog.load()

        recovered = merge_shared_document(
            self.store,
            self.binding(),
            backlog.file,
            stale_version,
            operations,
        )
        self.assertEqual(recovered.phase, "committed")
        self.assertFalse(backlog.transaction.exists())
        self.assertEqual(backlog.item(item["id"])["what"], "Recovered backlog field")
        backlog.load()

    def test_shared_json_document_pending_transaction_recovers_before_journal_commit(self):
        governance = Governance(self.root)
        work = governance.create(
            "Shared JSON",
            "Exercise adapter recovery",
            "journal-test",
            run_id="shared-json-run",
        )
        path = self.root / work["folder"] / "record.json"
        version = digest(path)
        operations = [
            DocumentEdit(
                target_uid=work["uuid"],
                field="summary",
                before_digest=digest_value(work["summary"]),
                new_value="Recovered JSON summary",
                operation="update",
            )
        ]
        with patch.object(
            Governance,
            "_upsert",
            side_effect=OSError("index update interrupted"),
        ):
            with self.assertRaisesRegex(OSError, "index update"):
                merge_shared_document(
                    self.store,
                    self.binding(),
                    path,
                    version,
                    operations,
                )
        self.assertTrue(governance.pending.exists())
        with self.assertRaisesRegex(WorkflowError, "DOCUMENT_TRANSACTION_PENDING"):
            governance.get(work["uuid"])

        recovered = merge_shared_document(
            self.store,
            self.binding(),
            path,
            version,
            operations,
        )
        self.assertEqual(recovered.phase, "committed")
        self.assertFalse(governance.pending.exists())
        self.assertEqual(governance.get(work["uuid"])["summary"], "Recovered JSON summary")

    def test_shared_document_recovery_conflict_keeps_journal_pending(self):
        backlog, item = self.backlog_item()
        stale_version = digest(backlog.file)
        operations = [
            DocumentEdit(
                target_uid=item["object_uid"],
                field="what",
                before_digest=field_digest(item["what"]),
                new_value="Blocked by conflicting recovery",
                operation="update",
            )
        ]

        def fail_records(path, value):
            if Path(path).resolve() == backlog.records_file.resolve():
                raise OSError("records projection interrupted")
            return atomic(path, value)

        with patch("gsp.todos.atomic", side_effect=fail_records):
            with self.assertRaisesRegex(OSError, "records projection"):
                merge_shared_document(
                    self.store,
                    self.binding(),
                    backlog.file,
                    stale_version,
                    operations,
                )
        atomic(backlog.records_file, {"schema": 2, "items": {}, "document": {}})
        with self.assertRaisesRegex(WorkflowError, "BACKLOG_RECOVERY_DIVERGED"):
            merge_shared_document(
                self.store,
                self.binding(),
                backlog.file,
                stale_version,
                operations,
            )
        with closing(sqlite3.connect(self.store.path)) as db:
            phase = db.execute(
                "SELECT phase FROM operations WHERE state_path=?",
                (backlog.file.relative_to(self.root).as_posix(),),
            ).fetchone()[0]
        self.assertIn(phase, ("prepared", "file-written"))

    def test_shared_document_expected_version_requires_sha256(self):
        backlog, item = self.backlog_item()
        with self.assertRaisesRegex(IdentityStoreError, "DOCUMENT_VERSION_REQUIRED"):
            merge_shared_document(
                self.store,
                self.binding(),
                backlog.file,
                "stale-version",
                [
                    DocumentEdit(
                        target_uid=item["object_uid"],
                        field="why",
                        before_digest=field_digest(item["why"]),
                        new_value="Should not apply",
                        operation="update",
                    )
                ],
            )
        self.assertEqual(backlog.item(item["id"])["why"], item["why"])

    def test_shared_todo_recovery_rejects_unrelated_pending_transaction(self):
        backlog, item = self.backlog_item()
        stale_version = digest(backlog.file)
        operations = [
            DocumentEdit(
                target_uid=item["object_uid"],
                field="context",
                before_digest=field_digest(item["context"]),
                new_value="Prepared before an unrelated transaction",
                operation="update",
            )
        ]

        def prepare_then_stop(instance, text, records, event, operation_uid=None):
            raise OSError("response lost before adapter publication")

        with patch.object(Backlog, "commit", prepare_then_stop):
            with self.assertRaisesRegex(OSError, "response lost"):
                merge_shared_document(
                    self.store,
                    self.binding(),
                    backlog.file,
                    stale_version,
                    operations,
                )

        with closing(sqlite3.connect(self.store.path)) as db:
            row = db.execute(
                "SELECT operation_uid FROM operations ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(row)
        operation_uid = row[0]
        current = backlog.file.read_text(encoding="utf-8-sig")
        records = backlog.records()
        atomic(
            backlog.transaction,
            {
                "document": backlog.file.name,
                "before": current,
                "after": current,
                "old_records": (
                    backlog.records_file.read_text(encoding="utf-8")
                    if backlog.records_file.exists()
                    else None
                ),
                "records": records,
                "event": "unrelated transaction",
                "operation_uid": "another-operation",
                "at": self.store.operation_details(operation_uid)["created_at"],
            },
        )

        with self.assertRaisesRegex(
            WorkflowError, "BACKLOG_RECOVERY_OWNERSHIP_MISMATCH"
        ):
            backlog.recover("direct recovery must not bypass owner")
        with self.assertRaisesRegex(
            WorkflowError, "BACKLOG_RECOVERY_OWNERSHIP_MISMATCH"
        ):
            merge_shared_document(
                self.store,
                self.binding(),
                backlog.file,
                stale_version,
                operations,
            )
        self.assertTrue(backlog.transaction.exists())
        self.assertEqual(
            self.store.get_operation(operation_uid).phase,
            "prepared",
        )

    def test_shared_json_recovery_rejects_unrelated_pending_transaction(self):
        from gsp.documents import Governance

        governance = Governance(self.root)
        record = governance.create(
            "Merge document",
            "Shared JSON merge fixture",
            "journal-json-fixture",
            run_id="merge-document",
        )
        path = self.root / record["folder"] / "record.json"
        expected = state_digest(path)
        operations = [
            DocumentEdit(
                target_uid=record["uuid"],
                field="title",
                before_digest=digest_value(record["title"]),
                new_value="Prepared title",
                operation="update",
            )
        ]

        def prepare_then_stop(instance, changes, operation_uid=None):
            raise OSError("response lost before document publication")

        with patch.object(Governance, "_commit", prepare_then_stop):
            with self.assertRaisesRegex(OSError, "response lost"):
                merge_shared_document(
                    self.store,
                    self.binding(),
                    path,
                    expected,
                    operations,
                )

        with closing(sqlite3.connect(self.store.path)) as db:
            row = db.execute(
                "SELECT operation_uid FROM operations ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(row)
        operation_uid = row[0]
        current = path.read_text(encoding="utf-8-sig")
        atomic(
            governance.pending,
            {
                "id": uuid.uuid4().hex,
                "created": self.store.operation_details(operation_uid)["created_at"],
                "operation_uid": "another-operation",
                "target_path": path.relative_to(self.root).as_posix(),
                "operations": [
                    {
                        "path": path.relative_to(self.root).as_posix(),
                        "before": expected,
                        "after": expected,
                        "text": current,
                    }
                ],
            },
        )

        with self.assertRaisesRegex(
            WorkflowError, "DOCUMENT_RECOVERY_OWNERSHIP_MISMATCH"
        ):
            governance.recover()
        with self.assertRaisesRegex(
            WorkflowError, "DOCUMENT_RECOVERY_OWNERSHIP_MISMATCH"
        ):
            merge_shared_document(
                self.store,
                self.binding(),
                path,
                expected,
                operations,
            )
        self.assertTrue(governance.pending.exists())
        self.assertEqual(
            self.store.get_operation(operation_uid).phase,
            "prepared",
        )

    def test_shared_todo_same_field_conflict_does_not_overwrite(self):
        backlog, item = self.backlog_item()
        stale_version = digest(backlog.file)
        edit = DocumentEdit(
            target_uid=item["object_uid"],
            field="what",
            before_digest=field_digest(item["what"]),
            new_value="First winner",
            operation="update",
        )
        merge_shared_document(
            self.store,
            self.binding(),
            backlog.file,
            stale_version,
            [edit],
        )
        conflict = DocumentEdit(
            target_uid=item["object_uid"],
            field="what",
            before_digest=field_digest(item["what"]),
            new_value="Stale loser",
            operation="update",
        )
        with self.assertRaisesRegex(WorkflowError, "DOCUMENT_EDIT_CONFLICT"):
            merge_shared_document(
                self.store,
                self.binding(),
                backlog.file,
                stale_version,
                [conflict],
            )
        self.assertEqual(backlog.item(item["id"])["what"], "First winner")

    def test_shared_todo_rejects_lifecycle_and_completion_bypass(self):
        backlog, item = self.backlog_item()
        version = digest(backlog.file)
        for field in ("evidence", "status", "history", "blocking"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    WorkflowError,
                    "DOCUMENT_LIFECYCLE_FORBIDDEN",
                ):
                    merge_shared_document(
                        self.store,
                        self.binding(),
                        backlog.file,
                        version,
                        [
                            DocumentEdit(
                                target_uid=item["id"],
                                field=field,
                                before_digest=digest_text(""),
                                new_value="forged completion",
                                operation="update",
                            )
                        ],
                    )

        backlog.done(
            item["id"],
            "evidence/fixture",
            "run-fixture",
            "T01",
            "verified by evidence",
            version="0.0.0",
        )
        with self.assertRaisesRegex(WorkflowError, "DOCUMENT_ITEM_NOT_EDITABLE"):
            merge_shared_document(
                self.store,
                self.binding(),
                backlog.file,
                digest(backlog.file),
                [
                    DocumentEdit(
                        target_uid=item["id"],
                        field="title",
                        before_digest=field_digest(item["title"]),
                        new_value="Completion bypass",
                        operation="update",
                    )
                ],
            )

    def test_project_write_keeps_state_authoritative_and_events_compatible(self):
        project = Project(self.root)
        project.initialize()
        run = self.root / ".gsp/runs/journal-runtime"
        run.mkdir(parents=True)
        state_file = run / "state.json"
        state = {
            "id": "journal-runtime",
            "title": "Journal runtime",
            "status": "design",
            "project_id": project.identity()["id"],
            "root": str(self.root),
            "tasks": [],
            "evidence": [],
            "inflight": {"task": "T01"},
        }
        atomic(state_file, state)
        project.write(state, "design-updated", "fixture")
        written = read(state_file)
        self.assertEqual(written["status"], "design")
        self.assertEqual(self.store.operation_count(), 1)
        self.assertEqual(self.store.event_count(), 1)
        projection = self.store.event_projection(state_file)
        self.assertIsNone(projection[0]["task_uid"])
        self.assertIsNone(projection[0]["dispatch_uid"])
        lines = (run / "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)

        project.write(written, "design-updated", "fixture")
        self.assertEqual(self.store.operation_count(), 1)
        self.assertEqual(self.store.event_count(), 1)
        lines = (run / "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)

    def drop_candidate_sidecar(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("DROP TABLE IF EXISTS operation_candidates")
            db.commit()

    def test_legacy_prepared_without_candidate_adopts_published_intended_bytes(self):
        path, initial, _, operation = self.begin_state_operation("legacy-adopt")
        new_state = {
            "status": "review",
            "number": 2,
            "updated": "2026-11-25T00:01:00+00:00",
        }
        intended_digest = self.store.prepare_operation(
            operation.operation_uid,
            path,
            canonical_state_digest(new_state),
        )
        atomic(path, new_state)
        self.assertEqual(state_digest(path), intended_digest)
        self.drop_candidate_sidecar()
        self.assertIsNone(self.store.get_operation_candidate(operation.operation_uid))

        recovered = recover_operation(self.store, operation.operation_uid)

        self.assertEqual(recovered.phase, "committed")
        self.assertEqual(recovered.result_ref, intended_digest)
        self.assertEqual(read(path), new_state)
        self.assertNotEqual(read(path), initial)
        candidate = self.store.get_operation_candidate(operation.operation_uid)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.candidate_digest, intended_digest)
        self.assertEqual(
            candidate.semantic_digest,
            operation.request.input_digest,
        )

    def test_legacy_prepared_without_candidate_rejects_unprovable_timestamp_variant(self):
        path, initial, _, operation = self.begin_state_operation("legacy-pending")
        first = {
            **initial,
            "updated": "2026-11-25T00:01:01+00:00",
        }
        second = {
            **initial,
            "updated": "2026-11-25T00:01:02+00:00",
        }
        intended_digest = self.store.prepare_operation(
            operation.operation_uid,
            path,
            canonical_state_digest(first),
        )
        original_bytes = path.read_bytes()
        self.drop_candidate_sidecar()

        with self.assertRaisesRegex(
            IdentityStoreError,
            "OPERATION_CANDIDATE_UNAVAILABLE",
        ):
            commit_state(
                self.store,
                operation.operation_uid,
                path,
                second,
            )

        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(get_operation(self.store, operation.operation_uid).phase, "prepared")
        self.assertEqual(
            self.store.operation_details(operation.operation_uid)["new_state_digest"],
            intended_digest,
        )
        self.assertIsNone(self.store.get_operation_candidate(operation.operation_uid))
        self.assertEqual(self.store.event_count(), 0)

    def test_legacy_corrupt_published_bytes_remain_pending_without_candidate(self):
        path, _, _, operation = self.begin_state_operation("legacy-corrupt")
        self.drop_candidate_sidecar()
        corrupt_bytes = b"{not-json\n"
        path.write_bytes(corrupt_bytes)
        intended_digest = self.store.prepare_operation(
            operation.operation_uid,
            path,
            state_digest(path),
        )

        with self.assertRaisesRegex(
            IdentityStoreError,
            "OPERATION_CANDIDATE_UNAVAILABLE",
        ):
            recover_operation(self.store, operation.operation_uid)

        self.assertEqual(path.read_bytes(), corrupt_bytes)
        self.assertEqual(get_operation(self.store, operation.operation_uid).phase, "prepared")
        self.assertEqual(
            self.store.operation_details(operation.operation_uid)["new_state_digest"],
            intended_digest,
        )
        self.assertIsNone(self.store.get_operation_candidate(operation.operation_uid))
        self.assertEqual(self.store.event_count(), 0)

    def test_shared_document_recovery_rejects_forged_operation_target(self):
        path = self.state_file("forged-shared-document")
        initial = {"status": "execute", "number": 1}
        atomic(path, initial)
        operation = begin_intent(
            self.store,
            self.journal_request(
                "forged-shared-document",
                operation_kind="document-write",
            ),
            state_digest(path),
        )
        target = {"status": "review", "number": 2}
        target_digest = canonical_state_digest(target)
        self.store.prepare_operation(
            operation.operation_uid,
            path,
            target_digest,
        )
        escaped = self.root.parent / "escaped-shared-document.json"
        escaped.write_bytes(
            (json.dumps(target, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            )
        )
        self.assertEqual(state_digest(escaped), target_digest)
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("DROP TRIGGER IF EXISTS operations_targets_immutable")
            db.execute("DELETE FROM metadata WHERE key='operations_target_guard'")
            db.execute(
                "UPDATE operations SET state_path=? WHERE operation_uid=?",
                ("../" + escaped.name, operation.operation_uid),
            )
            db.commit()

        with self.assertRaisesRegex(
            IdentityStoreError,
            "(OPERATION_PATH_ESCAPE|IDENTITY_AUTHORITY_CORRUPT)",
        ):
            recover_operation(self.store, operation.operation_uid)

        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT phase FROM operations WHERE operation_uid=?",
                    (operation.operation_uid,),
                ).fetchone()[0],
                "prepared",
            )
        self.assertEqual(self.store.event_count(), 0)

    def test_semantic_retry_rejects_type_collapsed_json_values(self):
        cases = (
            ("int-to-bool", {"value": 1}, {"value": True}),
            ("int-to-float", {"value": 1}, {"value": 1.0}),
            ("bool-to-int", {"value": False}, {"value": 0}),
            (
                "nested-dict",
                {"nested": {"value": 1}},
                {"nested": {"value": True}},
            ),
            (
                "nested-list",
                {"items": [1]},
                {"items": [True]},
            ),
        )
        for name, expected_fields, changed_fields in cases:
            with self.subTest(name=name):
                path = self.state_file("typed-" + name)
                initial = {
                    **expected_fields,
                    "updated": "2026-11-25T00:02:00+00:00",
                }
                intended = {
                    **expected_fields,
                    "updated": "2026-11-25T00:02:01+00:00",
                }
                incoming = {
                    **changed_fields,
                    "updated": "2026-11-25T00:02:02+00:00",
                }
                atomic(path, initial)
                operation = begin_intent(
                    self.store,
                    self.journal_request("typed-" + name),
                    state_digest(path),
                )
                candidate = self.store.freeze_operation_candidate(
                    operation.operation_uid,
                    path,
                    json.dumps(intended, ensure_ascii=False, indent=2) + "\n",
                    operation.request.input_digest,
                )
                original_bytes = path.read_bytes()

                with self.assertRaisesRegex(
                    IdentityStoreError,
                    "REQUEST_INPUT_CHANGED",
                ):
                    commit_state(
                        self.store,
                        operation.operation_uid,
                        path,
                        incoming,
                        semantic_digest=operation.request.input_digest,
                    )

                self.assertEqual(path.read_bytes(), original_bytes)
                self.assertEqual(
                    self.store.get_operation_candidate(operation.operation_uid),
                    candidate,
                )
                self.assertEqual(
                    get_operation(self.store, operation.operation_uid).phase,
                    "prepared",
                )
                self.assertEqual(self.store.event_count(), 0)

    def test_generic_commit_state_never_reuses_a_timestamp_variant(self):
        path = self.state_file("generic-strict")
        initial = {
            "status": "execute",
            "updated": "2026-11-25T00:00:50+00:00",
        }
        atomic(path, initial)
        operation = begin_intent(
            self.store,
            self.journal_request("generic-strict"),
            state_digest(path),
        )
        first = {**initial, "updated": "2026-11-25T00:00:51+00:00"}
        second = {**initial, "updated": "2026-11-25T00:00:52+00:00"}
        with patch("gsp.runtime.atomic", side_effect=OSError("before file")):
            with self.assertRaisesRegex(OSError, "before file"):
                commit_state(
                    self.store,
                    operation.operation_uid,
                    path,
                    first,
                )
        with self.assertRaisesRegex(IdentityStoreError, "REQUEST_INPUT_CHANGED"):
            commit_state(
                self.store,
                operation.operation_uid,
                path,
                second,
            )
        committed = commit_state(
            self.store,
            operation.operation_uid,
            path,
            first,
        )
        self.assertEqual(committed.phase, "committed")
        self.assertEqual(read(path), first)

    def test_committed_replay_compares_requested_candidate_before_success(self):
        path, initial, expected, operation = self.begin_state_operation("sol-committed-replay")
        value = {"status": "execute", "number": 2, "updated": "original"}
        committed = commit_state(self.store, operation.operation_uid, path, value)
        raw = path.read_bytes()
        cases = [(dict(value, number=999), None),
                 (dict(value, updated="changed"), None),
                 (dict(value, number=2.0), committed.request.input_digest)]
        for changed, semantic in cases:
            with self.subTest(changed=changed, semantic=semantic):
                with self.assertRaisesRegex(IdentityStoreError, "REQUEST_INPUT_CHANGED"):
                    commit_state(self.store, operation.operation_uid, path, changed, semantic_digest=semantic)
                self.assertEqual(path.read_bytes(), raw)
                self.assertEqual(self.store.operation_count(), 1)
                self.assertEqual(self.store.event_count(), 0)
        self.assertEqual(commit_state(self.store, operation.operation_uid, path, value).phase, "committed")
        self.drop_candidate_sidecar()
        self.assertEqual(commit_state(self.store, operation.operation_uid, path, value).phase, "committed")
        self.assertIsNone(self.store.get_operation_candidate(operation.operation_uid))
        with self.assertRaisesRegex(IdentityStoreError, "REQUEST_INPUT_CHANGED"):
            commit_state(self.store, operation.operation_uid, path, dict(value, number=999))
        self.assertEqual(path.read_bytes(), raw)
        self.assertIsNone(self.store.get_operation_candidate(operation.operation_uid))

    def _sol_projection_fixture(self, key):
        path, initial, expected, operation = self.begin_state_operation(key)
        commit_state(self.store, operation.operation_uid, path, {"status": "execute", "number": 2})
        event = EventEnvelope(
            event_uid=key + "-event", kind="state-updated", project_uid=self.store.project_uid(),
            work_uid=self.work.uid, task_uid=None, dispatch_uid=None, attempt_uid="attempt-a",
            contract_uid="contract-1", contract_version=1, contract_digest="a" * 64,
            controller_generation=2, cancel_generation=3, payload={"what": key},
        )
        emit_event(self.store, operation.operation_uid, event)
        self.assertEqual(len(self.store.event_projection(path)), 1)
        return path, operation

    def test_event_projection_rejects_corrupt_joined_operation(self):
        path, operation = self._sol_projection_fixture("sol-corrupt-projection-parent")
        raw = path.read_bytes()
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("UPDATE operations SET updated_at='' WHERE operation_uid=?", (operation.operation_uid,))
            db.commit()
        with self.assertRaisesRegex(IdentityStoreError, "IDENTITY_AUTHORITY_CORRUPT"):
            self.store.operation_details(operation.operation_uid)
        with self.assertRaisesRegex(IdentityStoreError, "IDENTITY_AUTHORITY_CORRUPT"):
            self.store.event_projection(path)
        self.assertEqual(path.read_bytes(), raw)

    def test_event_projection_rejects_inconsistent_stored_envelope(self):
        path, operation = self._sol_projection_fixture("sol-corrupt-projection-envelope")
        raw = path.read_bytes()
        with closing(sqlite3.connect(self.store.path)) as db:
            db.row_factory = sqlite3.Row
            value = dict(db.execute("SELECT * FROM events").fetchone())
            value.update(event_uid="forged-envelope", envelope_json="{}",
                         envelope_digest=digest_value({}), semantic_key=digest_value({"forged": True}))
            keys = list(value)
            db.execute("INSERT INTO events(" + ",".join(keys) + ") VALUES(" + ",".join("?" for _ in keys) + ")",
                       tuple(value[k] for k in keys))
            db.commit()
        with self.assertRaisesRegex(IdentityStoreError, "EVENT_PROJECTION_INVALID"):
            self.store.event_projection(path)
        self.assertEqual(path.read_bytes(), raw)

    def test_operation_projection_rejects_consistent_foreign_project_rows(self):
        path, operation = self._sol_projection_fixture("sol-foreign-projection")
        raw = path.read_bytes()
        with closing(sqlite3.connect(self.store.path)) as db:
            db.row_factory = sqlite3.Row
            parent = dict(db.execute("SELECT * FROM operations WHERE operation_uid=?", (operation.operation_uid,)).fetchone())
            parent.update(operation_uid=parent["operation_uid"] + "-foreign", request_uid=parent["request_uid"] + "-foreign",
                          logical_key=parent["logical_key"] + "-foreign", project_uid="foreign-project")
            keys = list(parent)
            db.execute("INSERT INTO operations(" + ",".join(keys) + ") VALUES(" + ",".join("?" for _ in keys) + ")", tuple(parent[k] for k in keys))
            row = dict(db.execute("SELECT * FROM events").fetchone())
            envelope = json.loads(row["envelope_json"])
            envelope.update(event_uid="sol-foreign-event", project_uid="foreign-project")
            row.update(event_uid=envelope["event_uid"], operation_uid=parent["operation_uid"], project_uid="foreign-project",
                       envelope_json=json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                       envelope_digest=digest_value(envelope),
                       semantic_key=digest_value({k: v for k, v in envelope.items() if k != "event_uid"}))
            keys = list(row)
            db.execute("INSERT INTO events(" + ",".join(keys) + ") VALUES(" + ",".join("?" for _ in keys) + ")", tuple(row[k] for k in keys))
            db.commit()
        for kind in ("operation-details", "event-projection"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(IdentityStoreError, "IDENTITY_AUTHORITY_CORRUPT"):
                    if kind == "operation-details":
                        self.store.operation_details(parent["operation_uid"])
                    else:
                        self.store.event_projection(path)
        self.assertEqual(path.read_bytes(), raw)

    def test_shared_json_prepared_retry_reuses_frozen_cross_second_candidate(self):
        from gsp.operation_journal import _merge_request
        book = Governance(self.root)
        work = book.create("Shared JSON clock", "Before", "clock-fixture",
                           run_id="shared-clock")
        path = self.root / work["folder"] / "record.json"
        version = state_digest(path)
        edits = [DocumentEdit(target_uid=work["uuid"], field="summary",
                              before_digest=digest_value(work["summary"]),
                              new_value="After", operation="update")]
        original = path.read_bytes()
        count = self.store.operation_count()
        with patch("gsp.documents.now", return_value="2026-11-26T00:00:01+00:00"):
            with patch.object(Governance, "_commit", side_effect=OSError("before-adapter")):
                with self.assertRaisesRegex(OSError, "before-adapter"):
                    merge_shared_document(self.store, self.binding(), path, version, edits)
        request_ref = _merge_request(self.binding(), path, version, edits)
        pending = self.store.operation_for_logical_key(request_ref.logical_key)
        self.assertEqual(pending["phase"], "prepared")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.store.operation_count(), count + 1)
        try:
            with patch("gsp.documents.now", return_value="2026-11-26T00:00:02+00:00"):
                committed = merge_shared_document(self.store, self.binding(), path, version, edits)
        except IdentityStoreError as exc:
            self.fail("Prepared shared JSON must replay across seconds: " + str(exc))
        self.assertEqual(committed.operation_uid, pending["operation_uid"])
        self.assertEqual(committed.phase, "committed")
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["summary"], "After")
        self.assertEqual(record["updated"], "2026-11-26T00:00:01+00:00")
        self.assertEqual(book.get(work["uuid"])["summary"], "After")
        self.assertEqual(self.store.operation_count(), count + 1)
        frozen = self.store.get_operation_candidate(committed.operation_uid)
        self.assertEqual(frozen.candidate_digest, state_digest(path))
        published = path.read_bytes()
        with patch("gsp.documents.now", return_value="2026-11-26T00:00:03+00:00"):
            repeated = merge_shared_document(self.store, self.binding(), path, version, edits)
        self.assertEqual(repeated.operation_uid, committed.operation_uid)
        self.assertEqual(path.read_bytes(), published)
        self.assertEqual(self.store.operation_count(), count + 1)


if __name__ == "__main__":
    unittest.main()
