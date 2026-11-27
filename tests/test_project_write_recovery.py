from contextlib import closing
import sqlite3
from gsp.identity_store import IdentityStore, IdentityStoreError
from gsp.operation_journal import begin_intent, commit_state, get_operation, recover_operation, state_digest
from gsp.orchestration_types import RequestRef, digest_value
"""Project.write recovery tests using APIs available in the T01 baseline."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from gsp.runtime import Project, WorkflowError, atomic, parser, read, request_action_uid


class ProjectWriteRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-project-write-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = Project(self.root)
        self.project.initialize()

    def seed_state(self, run_id="recovery-run", status="design", updated=None):
        run = self.root / ".gsp" / "runs" / run_id
        run.mkdir(parents=True, exist_ok=True)
        state = {
            "id": run_id,
            "title": "Recovery run",
            "status": status,
            "project_id": self.project.identity()["id"],
            "root": str(self.root),
            "tasks": [],
            "evidence": [],
        }
        if updated is not None:
            state["updated"] = updated
        state_file = run / "state.json"
        atomic(state_file, state)
        return state, state_file, run / "events.jsonl"

    def assert_complete_projection(self, event_file, event, detail):
        lines = event_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1, lines)
        records = [json.loads(line) for line in lines]
        self.assertEqual(records[0]["event"], event)
        self.assertEqual(records[0]["detail"], detail)
        self.assertIn("event_uid", records[0])

    def test_action_uid_freezes_pre_action_state_not_candidate_result(self):
        state, _, _ = self.seed_state()
        action_state = copy.deepcopy(state)
        args = SimpleNamespace(task="T1")
        action_uid = request_action_uid("task-start", args, action_state)

        first = copy.deepcopy(state)
        changed_candidate = copy.deepcopy(state)
        changed_candidate["status"] = "review"
        self.project.write(first, "task-start", "start", action_uid=action_uid)
        with self.assertRaisesRegex(WorkflowError, "REQUEST_INPUT_CHANGED"):
            Project(self.root).write(
                changed_candidate, "task-start", "start", action_uid=action_uid
            )

        later_pre_state = copy.deepcopy(action_state)
        later_pre_state["status"] = "review"
        self.assertNotEqual(
            action_uid,
            request_action_uid("task-start", args, later_pre_state),
        )

    def test_session_registration_action_uid_includes_identity_inputs(self):
        state, _, _ = self.seed_state()
        first_args = SimpleNamespace(
            id="recovery-run", session=None, role="controller", host="codex",
            thread="controller-thread", task=None, branch=None,
            worktree=str(self.root), write_mode="write", path=["."],
            context=str(self.root / "controller.json"), takeover=False,
            reason="",
        )
        second_args = SimpleNamespace(**{
            **vars(first_args),
            "role": "observer",
            "thread": "spare-thread",
            "write_mode": "read-only",
            "path": [],
            "context": str(self.root / "spare.json"),
        })
        self.assertNotEqual(
            request_action_uid("session-register", first_args, state),
            request_action_uid("session-register", second_args, state),
        )

    def test_session_report_action_uid_uses_complete_semantic_cli_contract(self):
        state, _, _ = self.seed_state()
        first = parser().parse_args([
            "session-report", "recovery-run", "T01", "--session", "S02",
            "--status", "DONE", "--summary", "first semantic report",
            "--files", "src/one.py", "--tests", "1 passed",
        ])
        changed_summary = parser().parse_args([
            "session-report", "recovery-run", "T01", "--session", "S02",
            "--status", "DONE", "--summary", "second semantic report",
            "--files", "src/one.py", "--tests", "1 passed",
        ])
        changed_status = parser().parse_args([
            "session-report", "recovery-run", "T01", "--session", "S02",
            "--status", "CHANGE_REQUEST", "--summary", "first semantic report",
            "--files", "src/one.py", "--tests", "1 passed",
        ])

        first_uid = request_action_uid("session-report", first, state)
        self.assertNotEqual(
            first_uid,
            request_action_uid("session-report", changed_summary, state),
        )
        self.assertNotEqual(
            first_uid,
            request_action_uid("session-report", changed_status, state),
        )

        ephemeral = SimpleNamespace(**vars(first))
        ephemeral._handler = object()
        ephemeral.func = object()
        self.assertEqual(
            first_uid,
            request_action_uid("session-report", ephemeral, state),
        )
        ephemeral.argv = []
        self.assertEqual(
            first_uid,
            request_action_uid("session-report", ephemeral, state),
        )
        ephemeral.argv = ["python", "-V"]
        self.assertNotEqual(
            first_uid,
            request_action_uid("session-report", ephemeral, state),
        )
        self.assertNotEqual(
            request_action_uid(
                "session-report", first, state,
                context={"binding_uid": "binding-one", "controller_generation": 1},
            ),
            request_action_uid(
                "session-report", first, state,
                context={"binding_uid": "binding-two", "controller_generation": 1},
            ),
        )

    def test_compiled_report_contract_does_not_rehash_legacy_plan_projections(self):
        from gsp.sessions import _report_request_uid
        state, _, _ = self.seed_state()
        state['current_contract'] = {
            'contract_uid': 'compiled-t01', 'version': 3, 'content_digest': 'a' * 64,
        }
        state['tasks'] = [
            {'id': 'T01', 'title': 'frozen task', 'acceptance': 'frozen', 'depends': []},
            {'id': 'T02', 'title': 'unrelated task', 'acceptance': 'other', 'depends': []},
        ]
        state['plan_seal'] = {'sha256': 'b' * 64, 'tasks_sha256': 'c' * 64}
        state['design_approval'] = {'sha256': 'd' * 64}
        args = parser().parse_args([
            'session-report', 'recovery-run', 'T01', '--session', 'S02',
            '--status', 'DONE', '--summary', 'same compiled contract',
        ])
        changed = copy.deepcopy(state)
        changed['tasks'][1]['title'] = 'unrelated task revised'
        changed['plan_seal'] = {'sha256': 'e' * 64, 'tasks_sha256': 'f' * 64}
        changed['design_approval'] = {'sha256': '1' * 64}
        for label, identity in [
            ('generic', lambda s: request_action_uid('session-report', args, s)),
            ('service', lambda s: _report_request_uid(self.project, s, args, None)),
        ]:
            with self.subTest(identity=label):
                self.assertEqual(identity(state), identity(changed))
                newer = copy.deepcopy(changed)
                newer['current_contract']['version'] = 4
                newer['current_contract']['content_digest'] = '2' * 64
                self.assertNotEqual(identity(state), identity(newer))

    def test_legacy_report_identity_binds_real_seal_and_approval(self):
        state, _, _ = self.seed_state()
        self.assertNotIn('current_contract', state)
        args = parser().parse_args([
            'session-report', 'recovery-run', 'T01', '--session', 'S02',
            '--status', 'DONE', '--summary', 'legacy sealed input',
        ])
        first = request_action_uid('session-report', args, state)
        for key, value in [
            ('plan_seal', {'sha256': 'b' * 64, 'tasks_sha256': 'c' * 64}),
            ('design_approval', {'sha256': 'd' * 64, 'quote': 'approved'}),
        ]:
            changed = copy.deepcopy(state)
            changed[key] = value
            with self.subTest(key=key):
                self.assertNotEqual(first, request_action_uid('session-report', args, changed))
        projected = copy.deepcopy(state)
        projected['status'] = 'needs-verification'
        projected['updated'] = '2030-01-01T00:00:00+00:00'
        self.assertEqual(first, request_action_uid('session-report', args, projected))

    def test_session_report_identity_ignores_its_own_workflow_state_transition(self):
        state, _, _ = self.seed_state(status="ready")
        args = parser().parse_args([
            "session-report", "recovery-run", "T01", "--session", "S02",
            "--status", "DONE", "--summary", "stable response-loss request",
            "--files", "src/one.py", "--tests", "1 passed",
        ])
        caller = {
            "binding_uid": "binding-one",
            "work_uid": state.get("work_uuid"),
            "current_contract": None,
            "attempt_uid": None,
            "verification_epoch": 4,
        }
        first = request_action_uid("session-report", args, state, context=caller)

        transitioned = copy.deepcopy(state)
        transitioned["status"] = "needs-verification"
        self.assertEqual(
            first,
            request_action_uid(
                "session-report", args, transitioned, context=caller,
            ),
        )

        changed_epoch = copy.deepcopy(state)
        changed_epoch["verification_epoch"] = 5
        self.assertNotEqual(
            first,
            request_action_uid(
                "session-report", args, changed_epoch, context=caller,
            ),
        )

        changed_contract = copy.deepcopy(state)
        changed_contract["current_contract"] = {
            "contract_uid": "contract-two",
            "version": 2,
            "content_digest": "b" * 64,
        }
        self.assertNotEqual(
            first,
            request_action_uid(
                "session-report", args, changed_contract, context=caller,
            ),
        )

    def test_response_loss_and_partial_projection_rebuild_one_event(self):
        state, state_file, event_file = self.seed_state()
        original_projection = Project.finish_write_projection

        def lose_after_publish(project, state_file, updated, committed):
            original_projection(project, state_file, updated, committed)
            raise OSError("response lost after event publication")

        with patch.object(Project, "finish_write_projection", lose_after_publish):
            with self.assertRaisesRegex(OSError, "response lost"):
                self.project.write(state, "stage-updated", "stable detail")

        process_boundary = Project(self.root)
        process_boundary.write(
            read(state_file), "stage-updated", "stable detail"
        )
        self.assert_complete_projection(
            event_file, "stage-updated", "stable detail"
        )

        complete = event_file.read_text(encoding="utf-8").rstrip("\n")
        event_file.write_text(complete[:20], encoding="utf-8")
        Project(self.root).write(
            read(state_file), "stage-updated", "stable detail"
        )
        self.assert_complete_projection(
            event_file, "stage-updated", "stable detail"
        )

    def test_legacy_projection_is_preserved_and_partial_tail_is_quarantined(self):
        state, _, event_file = self.seed_state()
        legacy = {
            "at": "2026-11-01T00:00:00+00:00",
            "event": "legacy-before-journal",
            "detail": "historical evidence",
        }
        partial_tail = '{"at":"2026-11-02T00:00:00+00:00","event":"partial'
        event_file.write_text(
            json.dumps(legacy, ensure_ascii=False) + "\n" + partial_tail,
            encoding="utf-8",
        )
        state["journal_action_uid"] = "legacy-projection-upgrade"

        self.project.write(state, "journal-upgrade", "new durable event")

        lines = event_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2, lines)
        records = [json.loads(line) for line in lines]
        self.assertEqual(records[0], legacy)
        self.assertEqual(records[1]["event"], "journal-upgrade")
        quarantine = event_file.parent / "events-quarantine"
        quarantined = list(quarantine.glob("*.jsonl"))
        self.assertEqual(len(quarantined), 1, quarantined)
        self.assertIn(partial_tail, quarantined[0].read_text(encoding="utf-8"))

    def test_changed_detail_for_same_logical_write_is_rejected(self):
        state, state_file, event_file = self.seed_state()
        state["journal_action_uid"] = "changed-detail-action"
        self.project.write(state, "stage-updated", "original detail")
        with self.assertRaisesRegex(WorkflowError, "REQUEST_INPUT_CHANGED"):
            changed = read(state_file)
            Project(self.root).write(
                changed, "stage-updated", "changed detail"
            )
        self.assert_complete_projection(
            event_file, "stage-updated", "original detail"
        )

    def test_changed_semantic_result_for_same_action_is_rejected(self):
        state, state_file, event_file = self.seed_state()
        state["journal_action_uid"] = "stable-action"
        self.project.write(state, "stage-updated", "original detail")

        changed = read(state_file)
        changed["status"] = "review"
        with self.assertRaisesRegex(WorkflowError, "REQUEST_INPUT_CHANGED"):
            Project(self.root).write(
                changed, "stage-updated", "original detail"
            )
        self.assert_complete_projection(
            event_file, "stage-updated", "original detail"
        )

    def test_same_second_empty_update_commits_and_emits_once(self):
        fixed = "2026-11-25T00:00:00+00:00"
        state, state_file, event_file = self.seed_state(updated=fixed)
        with patch("gsp.runtime.now", return_value=fixed):
            self.project.write(state, "unchanged-write", "same second")
        self.assert_complete_projection(
            event_file, "unchanged-write", "same second"
        )
        self.assertEqual(read(state_file)["updated"], fixed)

    def test_first_projection_write_failure_is_repaired_on_retry(self):
        state, state_file, event_file = self.seed_state()

        def fail_projection(path, value):
            if Path(path).resolve() == event_file.resolve():
                raise OSError("ENOSPC while replacing projection")
            return atomic(path, value)

        with patch("gsp.runtime.atomic", side_effect=fail_projection):
            with self.assertRaisesRegex(OSError, "ENOSPC"):
                self.project.write(state, "projection-retry", "durable")
        Project(self.root).write(
            read(state_file), "projection-retry", "durable"
        )
        self.assert_complete_projection(
            event_file, "projection-retry", "durable"
        )

    def test_event_projections_are_isolated_by_run(self):
        first, _, first_events = self.seed_state(run_id="run-a")
        second, _, second_events = self.seed_state(run_id="run-b")
        self.project.write(first, "run-a-event", "first")
        self.project.write(second, "run-b-event", "second")
        self.assert_complete_projection(first_events, "run-a-event", "first")
        self.assert_complete_projection(second_events, "run-b-event", "second")

    def seed_ready_delivery(self, run_id="recovery-run"):
        state, state_file, event_file = self.seed_state(
            run_id,
            status="ready",
            updated="2026-11-25T00:00:00+00:00",
        )
        _, doc = self.project.paths(run_id)
        doc.mkdir(parents=True, exist_ok=True)
        delivery = doc / "delivery.md"
        delivery.write_text(
            "# Delivery\n\nREADY：fixture delivery\n",
            encoding="utf-8",
        )
        return state, state_file, event_file, doc

    def pending_ready_write(
        self,
        *,
        event="pending-write",
        detail="stable detail",
        action_uid="pending-write-action",
        updated="2026-11-25T00:00:01+00:00",
    ):
        state, state_file, event_file, doc = self.seed_ready_delivery()
        candidate = copy.deepcopy(state)
        candidate["status"] = "execute"
        original_candidate = copy.deepcopy(candidate)
        original_bytes = state_file.read_bytes()
        delivery_bytes = (doc / "delivery.md").read_bytes()
        history_before = {
            path.name: path.read_bytes()
            for path in doc.glob("delivery-history-*.md")
        }

        real_atomic = atomic

        def fail_state_publish(path, value):
            if Path(path).resolve() == state_file.resolve():
                raise OSError("crash before state publication")
            return real_atomic(path, value)

        with patch("gsp.runtime.now", return_value=updated), patch(
            "gsp.runtime.atomic",
            side_effect=fail_state_publish,
        ):
            with self.assertRaisesRegex(OSError, "crash before"):
                self.project.write(
                    candidate,
                    event,
                    detail,
                    action_uid=action_uid,
                )

        self.assertEqual(candidate, original_candidate)
        self.assertEqual(state_file.read_bytes(), original_bytes)
        self.assertEqual((doc / "delivery.md").read_bytes(), delivery_bytes)
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in doc.glob("delivery-history-*.md")
            },
            history_before,
        )
        store, operation_uid = self.operation_for_path(state_file)
        self.assertEqual(get_operation(store, operation_uid).phase, "prepared")
        self.assertEqual(store.event_count(), 0)
        return {
            "candidate": original_candidate,
            "delivery_bytes": delivery_bytes,
            "doc": doc,
            "event": event,
            "event_file": event_file,
            "history_before": history_before,
            "operation_uid": operation_uid,
            "original_bytes": original_bytes,
            "state_file": state_file,
            "store": store,
            "detail": detail,
            "action_uid": action_uid,
        }

    def operation_for_path(self, state_file):
        store = IdentityStore(self.root)
        relative = Path(state_file).resolve().relative_to(self.root).as_posix()
        with closing(sqlite3.connect(store.path)) as db:
            row = db.execute(
                "SELECT operation_uid FROM operations WHERE state_path=?",
                (relative,),
            ).fetchone()
        self.assertIsNotNone(row)
        return store, row[0]

    def test_rejected_project_write_has_no_caller_or_delivery_effect(self):
        state, state_file, event_file, doc = self.seed_ready_delivery()
        candidate = copy.deepcopy(state)
        candidate["status"] = "execute"
        original_candidate = copy.deepcopy(candidate)
        original_state = state_file.read_bytes()
        original_delivery = (doc / "delivery.md").read_bytes()
        real_atomic = atomic

        def fail_state_publish(path, value):
            if Path(path).resolve() == state_file.resolve():
                raise OSError("crash before state publication")
            return real_atomic(path, value)

        with patch("gsp.runtime.now", return_value="2026-11-25T00:00:01+00:00"), patch(
            "gsp.runtime.atomic",
            side_effect=fail_state_publish,
        ):
            with self.assertRaisesRegex(OSError, "crash before"):
                self.project.write(
                    candidate,
                    "rejected-write",
                    "stable detail",
                    action_uid="rejected-write-action",
                )

        self.assertEqual(candidate, original_candidate)
        self.assertEqual(state_file.read_bytes(), original_state)
        self.assertEqual((doc / "delivery.md").read_bytes(), original_delivery)
        self.assertEqual(list(doc.glob("delivery-history-*.md")), [])
        self.assertFalse(event_file.exists())
        store, operation_uid = self.operation_for_path(state_file)
        self.assertEqual(get_operation(store, operation_uid).phase, "prepared")
        self.assertEqual(store.event_count(), 0)

    def test_changed_detail_rejection_preserves_pending_artifacts(self):
        pending = self.pending_ready_write()
        store = pending["store"]
        operation_uid = pending["operation_uid"]
        operation_before = store.operation_details(operation_uid)
        candidate_before = store.get_operation_candidate(operation_uid)
        caller = copy.deepcopy(pending["candidate"])

        with self.assertRaisesRegex(WorkflowError, "REQUEST_INPUT_CHANGED"):
            Project(self.root).write(
                caller,
                pending["event"],
                "changed detail",
                action_uid=pending["action_uid"],
            )

        self.assertEqual(caller, pending["candidate"])
        self.assertEqual(
            pending["state_file"].read_bytes(),
            pending["original_bytes"],
        )
        self.assertEqual(
            (pending["doc"] / "delivery.md").read_bytes(),
            pending["delivery_bytes"],
        )
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in pending["doc"].glob("delivery-history-*.md")
            },
            pending["history_before"],
        )
        self.assertEqual(store.operation_details(operation_uid), operation_before)
        self.assertEqual(
            store.get_operation_candidate(operation_uid),
            candidate_before,
        )
        self.assertEqual(store.event_count(), 0)

    def test_unrelated_cas_conflict_preserves_delivery_and_pending_state(self):
        pending = self.pending_ready_write()
        store = pending["store"]
        operation_uid = pending["operation_uid"]
        operation_before = store.operation_details(operation_uid)
        candidate_before = store.get_operation_candidate(operation_uid)
        caller = copy.deepcopy(pending["candidate"])
        unrelated = read(pending["state_file"])
        unrelated["foreign"] = True
        unrelated["updated"] = "2026-11-25T00:00:02+00:00"
        atomic(pending["state_file"], unrelated)
        unrelated_bytes = pending["state_file"].read_bytes()

        with self.assertRaisesRegex(WorkflowError, "STATE_CAS_MISMATCH"):
            Project(self.root).write(
                caller,
                pending["event"],
                pending["detail"],
                action_uid=pending["action_uid"],
            )

        self.assertEqual(caller, pending["candidate"])
        self.assertEqual(pending["state_file"].read_bytes(), unrelated_bytes)
        self.assertEqual(
            (pending["doc"] / "delivery.md").read_bytes(),
            pending["delivery_bytes"],
        )
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in pending["doc"].glob("delivery-history-*.md")
            },
            pending["history_before"],
        )
        self.assertEqual(store.operation_details(operation_uid), operation_before)
        self.assertEqual(
            store.get_operation_candidate(operation_uid),
            candidate_before,
        )
        self.assertEqual(store.event_count(), 0)

    def test_delivery_interruption_is_repaired_by_exact_replay(self):
        state, state_file, event_file, doc = self.seed_ready_delivery()
        candidate = copy.deepcopy(state)
        candidate["status"] = "execute"
        delivery = doc / "delivery.md"
        real_atomic = atomic
        failed = False

        def fail_delivery_once(path, value):
            nonlocal failed
            if Path(path).resolve() == delivery.resolve() and not failed:
                failed = True
                raise OSError("delivery publication interrupted")
            return real_atomic(path, value)

        with patch(
            "gsp.runtime.atomic",
            side_effect=fail_delivery_once,
        ):
            with self.assertRaisesRegex(OSError, "delivery publication"):
                self.project.write(
                    candidate,
                    "delivery-repair",
                    "stable detail",
                    action_uid="delivery-repair-action",
                )

        store, operation_uid = self.operation_for_path(state_file)
        self.assertEqual(get_operation(store, operation_uid).phase, "committed")
        self.assertEqual(read(state_file)["status"], "execute")
        self.assertIn("READY：", delivery.read_text(encoding="utf-8"))
        histories = list(doc.glob("delivery-history-*.md"))
        self.assertEqual(len(histories), 1)
        history_bytes = histories[0].read_bytes()
        self.assertEqual(store.event_count(), 0)

        Project(self.root).write(
            read(state_file),
            "delivery-repair",
            "stable detail",
            action_uid="delivery-repair-action",
        )

        self.assertNotIn("\n\nREADY：", delivery.read_text(encoding="utf-8"))
        histories = list(doc.glob("delivery-history-*.md"))
        self.assertEqual(len(histories), 1)
        self.assertEqual(histories[0].read_bytes(), history_bytes)
        self.assertEqual(store.event_count(), 1)
        self.assert_complete_projection(
            event_file,
            "delivery-repair",
            "stable detail",
        )

        complete = event_file.read_text(encoding="utf-8").rstrip("\n")
        event_file.write_text(complete[:20], encoding="utf-8")
        Project(self.root).write(
            read(state_file), "stage-updated", "stable detail"
        )
        records = [
            json.loads(line)
            for line in event_file.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [record["event"] for record in records],
            ["delivery-repair", "stage-updated"],
        )
        self.assertEqual(
            [record["detail"] for record in records],
            ["stable detail", "stable detail"],
        )

    def test_prepared_before_file_reuses_frozen_candidate_across_clock_seconds(self):
        t1 = "2026-11-25T00:00:01+00:00"
        t2 = "2026-11-25T00:00:02+00:00"
        state, state_file, event_file = self.seed_state(
            updated="2026-11-25T00:00:00+00:00"
        )
        original_bytes = state_file.read_bytes()
        real_atomic = atomic
        failed = False

        def fail_before_publication(path, value):
            nonlocal failed
            if Path(path).resolve() == state_file.resolve() and not failed:
                failed = True
                raise OSError("crash before state publication")
            return real_atomic(path, value)

        with patch("gsp.runtime.now", side_effect=[t1, t2]), patch(
            "gsp.runtime.atomic", side_effect=fail_before_publication
        ):
            with self.assertRaisesRegex(OSError, "crash before"):
                self.project.write(
                    state,
                    "clock-retry",
                    "stable detail",
                    action_uid="clock-retry-action",
                )
            store, operation_uid = self.operation_for_path(state_file)
            self.assertEqual(get_operation(store, operation_uid).phase, "prepared")
            candidate = store.get_operation_candidate(operation_uid)
            self.assertEqual(json.loads(candidate.candidate_text)["updated"], t1)
            self.assertEqual(state_file.read_bytes(), original_bytes)
            self.assertEqual(store.event_count(), 0)

            retry_state = read(state_file)
            self.project.write(
                retry_state,
                "clock-retry",
                "stable detail",
                action_uid="clock-retry-action",
            )

        frozen = store.get_operation_candidate(operation_uid)
        self.assertEqual(frozen.candidate_digest, candidate.candidate_digest)
        self.assertEqual(
            get_operation(store, operation_uid).phase,
            "committed",
        )
        self.assertEqual(read(state_file)["updated"], t1)
        self.assertEqual(retry_state["updated"], t1)
        self.assert_complete_projection(
            event_file,
            "clock-retry",
            "stable detail",
        )
        self.assertEqual(store.operation_count(), 1)

    def test_file_written_before_db_commit_reconciles_frozen_candidate(self):
        t1 = "2026-11-25T00:00:11+00:00"
        t2 = "2026-11-25T00:00:12+00:00"
        state, state_file, event_file = self.seed_state(
            updated="2026-11-25T00:00:10+00:00"
        )
        store = IdentityStore(self.root)
        original_mark = IdentityStore.mark_operation_written
        state_publishes = 0
        real_atomic = atomic

        def fail_after_publication_once(identity_store, operation_uid):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("crash after state publication")
            return original_mark(identity_store, operation_uid)

        failed = False

        def count_state_publish(path, value):
            nonlocal state_publishes
            if Path(path).resolve() == state_file.resolve():
                state_publishes += 1
            return real_atomic(path, value)

        with patch("gsp.runtime.now", side_effect=[t1, t2]), patch.object(
            IdentityStore,
            "mark_operation_written",
            autospec=True,
            side_effect=fail_after_publication_once,
        ), patch("gsp.runtime.atomic", side_effect=count_state_publish):
            with self.assertRaisesRegex(OSError, "crash after"):
                self.project.write(
                    state,
                    "file-first",
                    "stable detail",
                    action_uid="file-first-action",
                )
            store, operation_uid = self.operation_for_path(state_file)
            self.assertEqual(get_operation(store, operation_uid).phase, "prepared")
            candidate = store.get_operation_candidate(operation_uid)
            self.assertEqual(json.loads(state_file.read_text(encoding="utf-8"))["updated"], t1)
            self.assertEqual(state_digest(state_file), candidate.candidate_digest)
            self.assertEqual(store.event_count(), 0)

            retry_state = read(state_file)
            self.project.write(
                retry_state,
                "file-first",
                "stable detail",
                action_uid="file-first-action",
            )

        self.assertEqual(state_publishes, 1)
        self.assertEqual(read(state_file)["updated"], t1)
        self.assertEqual(get_operation(store, operation_uid).phase, "committed")
        self.assert_complete_projection(event_file, "file-first", "stable detail")
        self.assertEqual(store.operation_count(), 1)

    def test_semantic_digest_still_rejects_changed_candidate_fields(self):
        t1 = "2026-11-25T00:00:21+00:00"
        t2 = "2026-11-25T00:00:22+00:00"
        state, state_file, event_file = self.seed_state(
            updated="2026-11-25T00:00:20+00:00"
        )
        store = IdentityStore(self.root)
        original_bytes = state_file.read_bytes()
        request_input = {
            "state": {key: value for key, value in state.items() if key != "updated"},
            "event": "strict-reuse",
            "detail": "stable detail",
        }
        request = RequestRef(
            request_uid=digest_value(
                {"project_uid": store.project_uid(), "logical_key": "strict-reuse"}
            ),
            logical_key="strict-reuse",
            input_digest=digest_value(request_input),
            project_uid=store.project_uid(),
            work_uid=None,
            attempt_uid=None,
            operation_kind="state-write",
        )
        operation = begin_intent(store, request, state_digest(state_file))
        first = copy.deepcopy(state)
        first["updated"] = t1
        with patch(
            "gsp.runtime.atomic",
            side_effect=OSError("prepared before file"),
        ):
            with self.assertRaisesRegex(OSError, "prepared before"):
                commit_state(
                    store,
                    operation.operation_uid,
                    state_file,
                    first,
                    semantic_digest=request.input_digest,
                )
        candidate = store.get_operation_candidate(operation.operation_uid)

        changed = copy.deepcopy(state)
        changed["updated"] = t2
        changed["status"] = "review"
        with self.assertRaisesRegex(IdentityStoreError, "REQUEST_INPUT_CHANGED"):
            commit_state(
                store,
                operation.operation_uid,
                state_file,
                changed,
                semantic_digest=request.input_digest,
            )
        self.assertEqual(state_file.read_bytes(), original_bytes)
        self.assertEqual(store.get_operation_candidate(operation.operation_uid), candidate)
        self.assertEqual(get_operation(store, operation.operation_uid).phase, "prepared")
        self.assertEqual(store.event_count(), 0)

    def test_project_write_changed_detail_cannot_reuse_frozen_candidate(self):
        t1 = "2026-11-25T00:00:31+00:00"
        t2 = "2026-11-25T00:00:32+00:00"
        state, state_file, event_file = self.seed_state(
            updated="2026-11-25T00:00:30+00:00"
        )
        original_bytes = state_file.read_bytes()
        with patch("gsp.runtime.now", return_value=t1), patch(
            "gsp.runtime.atomic",
            side_effect=OSError("prepared before file"),
        ):
            with self.assertRaisesRegex(OSError, "prepared before"):
                self.project.write(
                    state,
                    "detail-reuse",
                    "original detail",
                    action_uid="detail-reuse-action",
                )
        store, operation_uid = self.operation_for_path(state_file)
        candidate = store.get_operation_candidate(operation_uid)

        with patch("gsp.runtime.now", return_value=t2):
            with self.assertRaisesRegex(WorkflowError, "REQUEST_INPUT_CHANGED"):
                Project(self.root).write(
                    read(state_file),
                    "detail-reuse",
                    "changed detail",
                    action_uid="detail-reuse-action",
                )
        self.assertEqual(state_file.read_bytes(), original_bytes)
        self.assertEqual(store.get_operation_candidate(operation_uid), candidate)
        self.assertEqual(get_operation(store, operation_uid).phase, "prepared")
        self.assertEqual(store.event_count(), 0)

    def test_unrelated_state_conflict_stays_recoverable(self):
        t1 = "2026-11-25T00:00:41+00:00"
        t2 = "2026-11-25T00:00:42+00:00"
        t3 = "2026-11-25T00:00:43+00:00"
        state, state_file, event_file = self.seed_state(
            updated="2026-11-25T00:00:40+00:00"
        )
        original_bytes = state_file.read_bytes()
        with patch("gsp.runtime.now", return_value=t1), patch(
            "gsp.runtime.atomic",
            side_effect=OSError("prepared before file"),
        ):
            with self.assertRaisesRegex(OSError, "prepared before"):
                self.project.write(
                    state,
                    "conflict-retry",
                    "stable detail",
                    action_uid="conflict-retry-action",
                )
        store, operation_uid = self.operation_for_path(state_file)
        candidate = store.get_operation_candidate(operation_uid)
        unrelated = {**state, "foreign": True, "updated": t2}
        atomic(state_file, unrelated)
        unrelated_bytes = state_file.read_bytes()

        with self.assertRaisesRegex(WorkflowError, "REQUEST_INPUT_CHANGED"):
            Project(self.root).write(
                unrelated,
                "conflict-retry",
                "stable detail",
                action_uid="conflict-retry-action",
            )
        self.assertEqual(state_file.read_bytes(), unrelated_bytes)
        self.assertEqual(get_operation(store, operation_uid).phase, "prepared")
        self.assertEqual(store.get_operation_candidate(operation_uid), candidate)
        self.assertEqual(store.event_count(), 0)

        state_file.write_bytes(original_bytes)
        with patch("gsp.runtime.now", return_value=t3):
            self.project.write(
                read(state_file),
                "conflict-retry",
                "stable detail",
                action_uid="conflict-retry-action",
            )
        self.assertEqual(get_operation(store, operation_uid).phase, "committed")
        self.assertEqual(read(state_file)["updated"], t1)
        self.assert_complete_projection(
            event_file,
            "conflict-retry",
            "stable detail",
        )

    def test_committed_recovery_rejects_changed_authoritative_state(self):
        state, state_file, _ = self.seed_state()
        self.project.write(
            state,
            "committed-authority",
            "stable detail",
            action_uid="committed-authority-action",
        )
        store, operation_uid = self.operation_for_path(state_file)
        committed = get_operation(store, operation_uid)
        atomic(state_file, {**read(state_file), "status": "review"})
        with self.assertRaisesRegex(IdentityStoreError, "STATE_AUTHORITY_MISMATCH"):
            recover_operation(store, committed.operation_uid)
        self.assertEqual(get_operation(store, operation_uid).phase, "committed")

    def _sol_cli_seed(self, command, run_id="sol-cli"):
        from process_helpers import session_binding
        from gsp.documents import Governance
        from gsp.runtime import digest, git_identity
        # CLI recovery uses a real work identity and registered controller.
        # The small synthetic ledgers used by lower-level CAS tests remain
        # deliberately independent of CLI authentication.
        # Allocate the real work through its owning API without adding an
        # unrelated Project.write operation to the recovery count assertions.
        record = Governance(self.root).create(
            "Recovery CLI fixture", "Exercise the normal recovery CLI.",
            "Synthetic test fixture", run_id=run_id,
        )
        state, state_file, event_file = self.seed_state(run_id)
        state.update(work_uuid=record["uuid"], number=record["number"],
                     doc_dir=record["folder"])
        doc = self.root / record["folder"]
        (doc / "delivery.md").write_text(
            "# Delivery\n\nREADY：fixture delivery\n", encoding="utf-8",
        )
        state.update(status="execute", scope={"mode": "off"}, git=git_identity(self.root),
                     verification_epoch=0, controller_required=False, review=None)
        state["tasks"] = [{"id": "T01", "title": "Fixture behavior", "acceptance": "One real unittest passes",
                           "depends": [], "status": "done", "history": []}]
        spec = doc / "spec.md"
        plan = doc / "plan.md"
        spec.write_text("# Fixture design\n\nPreserve the observed CLI and journal behavior.\n", encoding="utf-8")
        plan.write_text("# Fixture plan\n\nT01 verifies the fixture behavior.\n", encoding="utf-8")
        state["documents"] = {"design": spec.relative_to(self.root).as_posix(),
                              "plan": plan.relative_to(self.root).as_posix()}
        state["design_approval"] = {"sha256": digest(spec), "quote": "fixture"}
        state["plan_seal"] = {"sha256": digest(plan), "tasks_sha256": self.project.task_definition_hash(state)}
        (doc / "decisions.md").write_text("# Decisions\n\nFixture history is retained.\n", encoding="utf-8")
        atomic(state_file, state)
        binding = session_binding(self.root, state["work_uuid"])
        if not hasattr(self, "_sol_cli_contexts"):
            self._sol_cli_contexts = {}
        self._sol_cli_contexts[run_id] = str(self.root / binding.handle_path)
        state = read(state_file)
        options = ["--project", str(self.root), command, run_id]
        if command == "task-reopen":
            options += ["T01", "--reason", "confirmed regression needs repair"]
        elif command == "documents-adopt":
            options += ["--spec", state["documents"]["design"], "--plan", state["documents"]["plan"],
                        "--reason", "same canonical paths receive revised contracts"]
        elif command == "recover-run":
            state["inflight"] = {"id": "unknown-command", "task": "all", "argv": ["fixture"]}
            report = self.root / "recovery-report.md"
            report.write_text("# Recovery\n\nFixture command did not run.\n", encoding="utf-8")
            options += ["--evidence", report.name, "--outcome", "not-run"]
        atomic(state_file, state)
        return state, state_file, event_file, doc, options

    def _sol_cli_call(self, argv):
        import contextlib
        import io
        from process_helpers import temporary_env
        from gsp.runtime import main, parser
        separator = argv.index("--") if "--" in argv else len(argv)
        args = parser().parse_args(argv[:separator])
        context = self._sol_cli_contexts[getattr(args, "id")]
        output, error = io.StringIO(), io.StringIO()
        with (temporary_env({"GSP_SESSION_CONTEXT": context}),
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(error)):
            code = main(argv)
        return code, output.getvalue(), error.getvalue()

    def test_cli_rejected_write_has_no_precommit_delivery_or_decision_effect(self):
        from gsp.runtime import parser
        for command in ("task-reopen", "documents-adopt", "recover-run"):
            with self.subTest(command=command):
                state, state_file, _, doc, argv = self._sol_cli_seed(command, "sol-reject-" + command)
                args = parser().parse_args(argv)
                action_uid = request_action_uid(command, args, state)
                wrong = copy.deepcopy(state)
                wrong["status"] = "blocked"
                real_atomic = atomic
                def reject_publication(path, value):
                    if Path(path).resolve() == state_file.resolve():
                        raise OSError("freeze conflicting request without publication")
                    return real_atomic(path, value)
                with patch("gsp.runtime.atomic", side_effect=reject_publication):
                    with self.assertRaisesRegex(OSError, "without publication"):
                        self.project.write(wrong, command, "", action_uid=action_uid)
                before_state = state_file.read_bytes()
                before_docs = {p.name: p.read_bytes() for p in doc.iterdir() if p.is_file()}
                code, out, err = self._sol_cli_call(argv)
                self.assertEqual(code, 1, (out, err))
                self.assertIn("REQUEST_INPUT_CHANGED", err)
                self.assertEqual(state_file.read_bytes(), before_state)
                self.assertEqual({p.name: p.read_bytes() for p in doc.iterdir() if p.is_file()}, before_docs)

    def test_cli_task_reopen_replays_committed_projections_after_response_loss(self):
        state, state_file, _, doc, argv = self._sol_cli_seed("task-reopen", "sol-reopen-replay")
        real_projection = Project.finish_write_projection
        def lose_response(project, state_file, updated, committed):
            real_projection(project, state_file, updated, committed)
            raise OSError("response lost after committed reopen")
        with patch.object(Project, "finish_write_projection", lose_response):
            code, out, err = self._sol_cli_call(argv)
        self.assertEqual(code, 1, (out, err))
        self.assertIn("response lost", err)
        committed = state_file.read_bytes()
        decisions = (doc / "decisions.md").read_bytes()
        history = {p.name: p.read_bytes() for p in doc.glob("delivery-history-*.md")}
        store = IdentityStore(self.root)
        operations, events = store.operation_count(), store.event_count()
        code, out, err = self._sol_cli_call(argv)
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(state_file.read_bytes(), committed)
        self.assertEqual((doc / "decisions.md").read_bytes(), decisions)
        self.assertEqual({p.name: p.read_bytes() for p in doc.glob("delivery-history-*.md")}, history)
        self.assertEqual((store.operation_count(), store.event_count()), (operations, events))
        self.assertEqual(decisions.decode("utf-8").count("[rework]"), 1)
        self.assertIn("NOT_READY", (doc / "delivery.md").read_text(encoding="utf-8"))

    def test_cli_reopen_rebuilds_decisions_after_postcommit_projection_interruption(self):
        state, state_file, _, doc, argv = self._sol_cli_seed("task-reopen", "sol-reopen-projection")
        original_decisions = (doc / "decisions.md").read_bytes()
        real_invalidate = __import__("gsp.runtime", fromlist=["invalidate_delivery"]).invalidate_delivery
        def lose_projection(path, reason):
            if Path(path).resolve() == doc.resolve():
                raise OSError("interrupted delivery projection")
            return real_invalidate(path, reason)
        with patch("gsp.runtime.invalidate_delivery", side_effect=lose_projection):
            code, out, err = self._sol_cli_call(argv)
        self.assertEqual(code, 1, (out, err))
        self.assertIn("interrupted delivery projection", err)
        self.assertEqual(read(state_file)["tasks"][0]["status"], "pending")
        self.assertEqual((doc / "decisions.md").read_bytes(), original_decisions)
        code, out, err = self._sol_cli_call(argv)
        self.assertEqual(code, 0, (out, err))
        self.assertEqual((doc / "decisions.md").read_text(encoding="utf-8").count("[rework]"), 1)
        self.assertIn("NOT_READY", (doc / "delivery.md").read_text(encoding="utf-8"))

    def test_cli_revalidation_distinct_cycles_do_not_alias_requests(self):
        import sys
        for controller in (False, True):
            with self.subTest(controller=controller):
                run_id = "sol-cycles-" + str(int(controller))
                state, state_file, _, _, _ = self._sol_cli_seed("task-reopen", run_id)
                state["tasks"][0]["status"] = "running"
                atomic(state_file, state)
                # Real test process and real GSP receipt. This does not fabricate a
                # pass or stub the evidence selector being exercised by revalidate.
                script = self.root / "test_cycle_fixture.py"
                script.write_text("import unittest\nclass Behavior(unittest.TestCase):\n def test_value(self): self.assertEqual(2 + 2, 4)\nif __name__ == '__main__': unittest.main()\n", encoding="utf-8")
                code, out, err = self._sol_cli_call(["--project", str(self.root), "run", run_id, "--task", "T01", "--", sys.executable, "-X", "utf8", "-B", str(script)])
                self.assertEqual(code, 0, (out, err))
                code, out, err = self._sol_cli_call(["--project", str(self.root), "task-done", run_id, "T01"])
                self.assertEqual(code, 0, (out, err))
                for cycle in range(3):
                    state = read(state_file)
                    state.update(controller_required=controller, verification_epoch=cycle,
                                 status="needs-verification" if controller else "execute")
                    state["revalidation"] = [{"task": "T01", "reason": "same incident description", "event": "fixture", "at": "fixed-for-all-cycles"}]
                    self.project.write(state, "fixture-cycle", str(cycle), action_uid="fixture-cycle-" + str(cycle))
                    code, out, err = self._sol_cli_call(["--project", str(self.root), "revalidate", run_id])
                    self.assertEqual(code, 0, (controller, cycle, out, err))
                    self.assertEqual(read(state_file)["revalidation"], [])
                    self.assertEqual(read(state_file)["tasks"][0]["status"], "done")
                    before = (state_file.read_bytes(), IdentityStore(self.root).operation_count())
                    code, out, err = self._sol_cli_call(["--project", str(self.root), "revalidate", run_id])
                    self.assertEqual(code, 0, (out, err))
                    self.assertEqual((state_file.read_bytes(), IdentityStore(self.root).operation_count()), before)

    def test_cli_next_write_completes_previous_committed_projections(self):
        state, state_file, _, doc, argv = self._sol_cli_seed("task-reopen", "sol-reopen-next-command")
        real_invalidate = __import__("gsp.runtime", fromlist=["invalidate_delivery"]).invalidate_delivery
        def interrupt_projection(path, reason):
            if Path(path).resolve() == doc.resolve():
                raise OSError("interrupt committed delivery projection")
            return real_invalidate(path, reason)
        with patch("gsp.runtime.invalidate_delivery", side_effect=interrupt_projection):
            code, out, err = self._sol_cli_call(argv)
        self.assertEqual(code, 1, (out, err))
        self.assertIn("interrupt committed", err)
        self.assertEqual(read(state_file)["tasks"][0]["status"], "pending")
        self.assertNotIn("[rework]", (doc / "decisions.md").read_text(encoding="utf-8"))
        code, out, err = self._sol_cli_call(["--project", str(self.root), "task-start", state["id"], "T01"])
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(read(state_file)["tasks"][0]["status"], "running")
        self.assertEqual((doc / "decisions.md").read_text(encoding="utf-8").count("[rework]"), 1)
        rows = IdentityStore(self.root).event_projection(state_file)
        self.assertEqual([row["payload"]["event"] for row in rows].count("task-reopen"), 1)
        self.assertEqual([row["payload"]["event"] for row in rows].count("task-start"), 1)

    def test_cli_run_start_rejection_does_not_revoke_delivery(self):
        import sys
        from gsp.runtime import stable_action_uid
        state, state_file, _, doc, _ = self._sol_cli_seed("task-reopen", "sol-run-start-reject")
        evidence_uid = "sol-fixed-run-start-request"
        wrong = copy.deepcopy(state)
        wrong["status"] = "blocked"
        real_atomic = atomic
        def freeze_only(path, value):
            if Path(path).resolve() == state_file.resolve():
                raise OSError("prepare run-start conflict without publication")
            return real_atomic(path, value)
        with patch("gsp.runtime.atomic", side_effect=freeze_only):
            with self.assertRaisesRegex(OSError, "without publication"):
                self.project.write(wrong, "run-start", evidence_uid,
                                   action_uid=stable_action_uid("run-start", evidence_uid))
        before_state = state_file.read_bytes()
        before_docs = {p.name: p.read_bytes() for p in doc.iterdir() if p.is_file()}
        sentinel = self.root / "should-not-execute"
        command = ["--project", str(self.root), "run", state["id"], "--task", "all", "--",
                   sys.executable, "-c", "from pathlib import Path; Path(" + repr(str(sentinel)) + ").write_text('ran')"]
        with patch("gsp.runtime.uuid.uuid4", return_value=SimpleNamespace(hex=evidence_uid)):
            code, out, err = self._sol_cli_call(command)
        self.assertEqual(code, 1, (out, err))
        self.assertIn("REQUEST_INPUT_CHANGED", err)
        self.assertFalse(sentinel.exists())
        self.assertEqual(state_file.read_bytes(), before_state)
        self.assertEqual({p.name: p.read_bytes() for p in doc.iterdir() if p.is_file()}, before_docs)

    def test_cli_recover_run_rebuilds_committed_run_start_once(self):
        import sys
        state, state_file, _, doc, _ = self._sol_cli_seed("task-reopen", "sol-run-start-recover")
        original_invalidate = __import__("gsp.runtime", fromlist=["invalidate_delivery"]).invalidate_delivery
        def interrupt_projection(path, reason):
            if Path(path).resolve() == doc.resolve():
                raise OSError("run-start committed before delivery projection")
            return original_invalidate(path, reason)
        with patch("gsp.runtime.invalidate_delivery", side_effect=interrupt_projection):
            code, out, err = self._sol_cli_call(["--project", str(self.root), "run", state["id"], "--task", "all", "--",
                                                 sys.executable, "-c", "raise AssertionError('must not launch')"])
        self.assertEqual(code, 1, (out, err))
        self.assertIn("run-start committed", err)
        self.assertIsNotNone(read(state_file).get("inflight"))
        store = IdentityStore(self.root)
        self.assertEqual(store.operation_count(), 1)
        self.assertEqual(store.event_count(), 0)
        report = self.root / "run-start-recovery.md"
        report.write_text("# Investigation\n\nThe command did not launch; only state publication completed.\n", encoding="utf-8")
        argv = ["--project", str(self.root), "recover-run", state["id"], "--evidence", report.name, "--outcome", "not-run"]
        projection_run_start = []
        original_projection = Project.finish_write_projection
        def observe_projection(project, state_file, updated, committed):
            if updated and updated.get("inflight"):
                projection_run_start.append(updated["inflight"]["id"])
            return original_projection(project, state_file, updated, committed)
        with patch.object(Project, "finish_write_projection", observe_projection):
            code, out, err = self._sol_cli_call(argv)
        self.assertEqual(code, 0, (out, err))
        events = store.event_projection(state_file)
        self.assertEqual([row["payload"]["event"] for row in events].count("run-start"), 1)
        self.assertEqual([row["payload"]["event"] for row in events].count("recover-run"), 1)
        self.assertEqual(len(projection_run_start), 1)
        self.assertEqual(len(list(doc.glob("delivery-history-*.md"))), 1)
        self.assertIn("NOT_READY", (doc / "delivery.md").read_text(encoding="utf-8"))
        before = (state_file.read_bytes(), store.operation_count(), store.event_count())
        code, out, err = self._sol_cli_call(argv)
        self.assertEqual(code, 0, (out, err))
        self.assertEqual((state_file.read_bytes(), store.operation_count(), store.event_count()), before)
        self.assertEqual(len(list(doc.glob("delivery-history-*.md"))), 1)

    def test_cli_revalidation_recovers_file_written_transition_before_success(self):
        import sys
        for transition in ("resume", "evidence-stale", "revalidated"):
            with self.subTest(transition=transition):
                run_id = "sol-published-" + transition
                state, state_file, _, _, _ = self._sol_cli_seed("task-reopen", run_id)
                state["tasks"][0]["status"] = "running"
                atomic(state_file, state)
                script = self.root / "test_published_cycle_fixture.py"
                script.write_text("import unittest\nclass Behavior(unittest.TestCase):\n def test_value(self): self.assertEqual(4, 4)\nif __name__ == '__main__': unittest.main()\n", encoding="utf-8")
                code, out, err = self._sol_cli_call(["--project", str(self.root), "run", run_id, "--task", "T01", "--",
                                                    sys.executable, "-X", "utf8", "-B", str(script)])
                self.assertEqual(code, 0, (out, err))
                code, out, err = self._sol_cli_call(["--project", str(self.root), "task-done", run_id, "T01"])
                self.assertEqual(code, 0, (out, err))
                state = read(state_file)
                state.update(controller_required=transition == "resume",
                             status="needs-verification" if transition == "resume" else "execute",
                             verification_epoch=1 if transition == "evidence-stale" else 0)
                state["revalidation"] = [{"task": "T01", "event": "fixture", "reason": transition, "at": "persisted-incident"}]
                self.project.write(state, "fixture-published-cycle", transition,
                                   action_uid="fixture-published-cycle-" + transition)
                interrupted = []
                original_finish = IdentityStore.finish_operation
                def lose_between_file_and_commit(store, operation_uid, result_ref):
                    if not interrupted and store.operation_state_path(operation_uid) == state_file.resolve():
                        interrupted.append(operation_uid)
                        raise OSError("state published before revalidation DB commit")
                    return original_finish(store, operation_uid, result_ref)
                argv = ["--project", str(self.root), "revalidate", run_id]
                with patch.object(IdentityStore, "finish_operation", lose_between_file_and_commit):
                    code, out, err = self._sol_cli_call(argv)
                self.assertEqual(code, 1, (out, err))
                self.assertIn("state published", err)
                self.assertEqual(len(interrupted), 1)
                store = IdentityStore(self.root)
                self.assertEqual(store.get_operation(interrupted[0]).phase, "file-written")
                code, out, err = self._sol_cli_call(argv)
                self.assertEqual(code, 0, (out, err))
                self.assertEqual(store.get_operation(interrupted[0]).phase, "committed")
                self.assertEqual(read(state_file)["revalidation"], [])
                self.assertEqual(read(state_file)["tasks"][0]["status"], "done")
                self.assertEqual(read(state_file)["verification_epoch"], 1 if transition == "evidence-stale" else 0)
                with closing(sqlite3.connect(store.path)) as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM operations WHERE state_path=? AND phase!='committed'",
                                                (state_file.relative_to(self.root).as_posix(),)).fetchone()[0], 0)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE operation_uid=?", (interrupted[0],)).fetchone()[0], 1)
                before = (state_file.read_bytes(), store.operation_count(), store.event_count())
                code, out, err = self._sol_cli_call(argv)
                self.assertEqual(code, 0, (out, err))
                self.assertEqual((state_file.read_bytes(), store.operation_count(), store.event_count()), before)

    def test_cli_unbacked_replay_marker_rejects_without_any_effect(self):
        from gsp.runtime import parser
        for command in ("task-reopen", "documents-adopt", "recover-run"):
            with self.subTest(command=command):
                state, state_file, event_file, doc, argv = self._sol_cli_seed(command, "sol-unbacked-" + command)
                args = parser().parse_args(argv)
                state["_command_commit"] = {
                    "schema": 1, "command": command, "event": command,
                    "action_uid": "never-recorded-" + command,
                    "request_digest": request_action_uid(command, args),
                    "detail": "",
                    "effects": {"documents_root": doc.relative_to(self.root).as_posix()},
                }
                atomic(state_file, state)
                store = IdentityStore(self.root)
                before_state = state_file.read_bytes()
                before_docs = {p.name: p.read_bytes() for p in doc.iterdir() if p.is_file()}
                before_counts = (store.operation_count(), store.event_count())
                before_events = event_file.read_bytes() if event_file.exists() else None
                run = state_file.parent
                before_files = sorted(p.relative_to(run).as_posix() for p in run.rglob("*") if p.is_file())
                code, out, err = self._sol_cli_call(argv)
                self.assertEqual(code, 1, (out, err))
                self.assertIn("COMMAND_REPLAY_UNPROVEN", err)
                self.assertEqual(state_file.read_bytes(), before_state)
                self.assertEqual({p.name: p.read_bytes() for p in doc.iterdir() if p.is_file()}, before_docs)
                self.assertEqual((store.operation_count(), store.event_count()), before_counts)
                self.assertEqual(event_file.read_bytes() if event_file.exists() else None, before_events)
                self.assertEqual(sorted(p.relative_to(run).as_posix() for p in run.rglob("*") if p.is_file()), before_files)

    def test_matching_action_unbacked_marker_rejects_before_intent(self):
        run_id = "sol-matching-unbacked"
        run, doc = self.project.paths(run_id)
        run.mkdir(parents=True, exist_ok=True)
        action_uid = "action-never-recorded"
        state_file = run / "state.json"
        state = {
            "id": run_id, "title": "Matching marker fixture", "status": "design",
            "project_id": self.project.identity()["id"], "root": str(self.root),
            "tasks": [], "evidence": [],
            "_command_commit": {
                "schema": 1, "command": "forged-event", "event": "forged-event",
                "action_uid": action_uid, "detail": "forged detail",
                "effects": {"documents_root": doc.relative_to(self.root).as_posix()},
            },
        }
        atomic(state_file, state)
        store = IdentityStore(self.root)
        before = state_file.read_bytes()
        before_counts = (store.operation_count(), store.event_count())
        before_files = {p.relative_to(run).as_posix(): p.read_bytes()
                        for p in run.rglob("*") if p.is_file()}
        with self.assertRaisesRegex(WorkflowError, "COMMAND_REPLAY_UNPROVEN"):
            self.project.write(state, "forged-event", "forged detail", action_uid=action_uid)
        self.assertEqual(state_file.read_bytes(), before)
        self.assertEqual((store.operation_count(), store.event_count()), before_counts)
        self.assertEqual({p.relative_to(run).as_posix(): p.read_bytes()
                          for p in run.rglob("*") if p.is_file()}, before_files)


if __name__ == "__main__":
    unittest.main()
