"""Codex-native create, archive and restore lifecycle contracts."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from gsp.codex_workspace import CodexWorkspaceAdapter
from gsp.host_actions import (
    ARCHIVE_WORKTREE,
    CREATE_WORKTREE,
    RESTORE_WORKTREE,
    HostAction,
    validate_host_action_args,
)
from gsp.identity_store import IdentityStore
from gsp.orchestration_types import (
    ObjectRef,
    RequestRef,
    SessionBinding,
    WorktreeRequest,
    digest_value,
)
from gsp.runtime import WorkflowError
from gsp.session_context import record_binding_authoritative


class CodexWorkspaceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            prefix="gsp-codex-workspace-lifecycle-"
        )
        self.addCleanup(self.tmp.cleanup)
        self.area = Path(self.tmp.name).resolve()
        self.control = self.area / "control"
        self.control.mkdir()
        self.git(self.control, "init", "-q", "-b", "main")
        (self.control / "README.md").write_text("# fixture\n", encoding="utf-8")
        self.project_uid = "project-codex-workspace-lifecycle"
        marker = self.control / ".gsp" / "project.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(
            json.dumps({"schema": 1, "id": self.project_uid}),
            encoding="utf-8",
        )
        self.git(self.control, "add", "README.md", ".gsp/project.json")
        self.git(self.control, "commit", "-qm", "baseline")
        self.base = self.git(self.control, "rev-parse", "HEAD")
        self.common = (
            self.control
            / self.git(self.control, "rev-parse", "--git-common-dir")
        ).resolve()
        self.adapter = CodexWorkspaceAdapter(
            self.control, expected_project_uid=self.project_uid
        )
        self.store = IdentityStore(self.control)
        self.store.initialize()
        self.owner = "host-local"
        work_request = RequestRef(
            request_uid="request-work-lifecycle",
            logical_key="work-lifecycle",
            input_digest=digest_value({"fixture": "work-lifecycle"}),
            project_uid=self.project_uid,
            work_uid=None,
            attempt_uid=None,
            operation_kind="allocate",
        )
        self.work_uid = self.store.allocate(
            "work", None, work_request
        ).uid
        self.controller = self.binding("controller")
        self.store.claim_initial_work_controller(
            self.controller.work_uid, "work", self.controller.binding_uid,
        )
    def git(self, cwd, *argv):
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_CONFIG_")
        }
        result = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "core.hooksPath=",
                *argv,
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            ),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def binding(self, suffix):
        binding = SessionBinding(
            binding_uid=digest_value(
                {"kind": "binding", "suffix": suffix, "project": self.project_uid}
            )[:32],
            session=ObjectRef(
                uid=digest_value({"kind": "session", "suffix": suffix})[:32],
                kind="session",
                project_uid=self.project_uid,
                canonical_id="S-" + suffix,
            ),
            work_uid=self.work_uid,
            kind="main_chat",
            host="codex",
            host_thread_id="thread-" + suffix,
            host_state="verified",
            controller_generation=1,
            cancel_generation=1,
            capabilities=frozenset(
                {"accept", "claim", "dispatch", "handoff", "plan", "report", "write"}
            ),
            cancelled=False,
            created_at="2026-11-27T00:00:00+00:00",
            project_uid=self.project_uid,
            common_dir=str(self.common),
            handle_path=".gsp/runs/session-context/" + suffix + ".json",
        )
        record_binding_authoritative(self.store, binding)
        return binding

    def request(self, suffix, operation_kind="native-worktree-create"):
        path = self.area / ("external-" + suffix)
        branch = "codex/GSP-000034-T05-" + suffix
        request_uid = "request-" + operation_kind + "-" + suffix
        ref = RequestRef(
            request_uid=request_uid,
            logical_key=operation_kind + ":" + suffix,
            input_digest=digest_value(
                {
                    "request_uid": request_uid,
                    "project_uid": self.project_uid,
                    "work_uid": self.controller.work_uid,
                    "base_sha": self.base,
                    "branch": branch,
                    "path": str(path),
                }
            ),
            project_uid=self.project_uid,
            work_uid=self.controller.work_uid,
            attempt_uid=None,
            operation_kind=operation_kind,
            controller_generation=1,
            cancel_generation=1,
        )
        return WorktreeRequest(
            request=ref,
            owner_adapter="codex-native-worktree",
            source_root=str(self.control),
            base_sha=self.base,
            branch=branch,
            path_hint=str(path),
        )

    def materialize(self, request, *, detach=False):
        path = Path(request.path_hint)
        if detach:
            self.git(
                self.control,
                "worktree",
                "add",
                "--detach",
                str(path),
                request.base_sha,
            )
        else:
            self.git(
                self.control,
                "worktree",
                "add",
                "-b",
                request.branch,
                str(path),
                request.base_sha,
            )
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "worktree", "remove", "--force", str(path)],
                cwd=self.control,
                capture_output=True,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                ),
            )
        )
        return path

    @staticmethod
    def native_payload(value):
        return {
            "status": "fulfilled",
            "value": {
                "isError": False,
                "content": [{"type": "text", "text": json.dumps(value)}],
            },
        }

    def create_receipt(self, request, path):
        operation_id = "create-" + request.request.request_uid
        attachment_id = "attachment-" + request.request.request_uid
        return {
            "schema": 1,
            "kind": "observed-native-tool-receipts",
            "request_uid": request.request.request_uid,
            "operation_id": operation_id,
            "creation_status": self.native_payload(
                {
                    "operationId": operation_id,
                    "stage": "completed",
                    "result": {
                        "type": "created",
                        "registrationError": None,
                        "worktreeGitRoot": str(path),
                        "worktreeWorkspaceRoot": str(path),
                    },
                }
            ),
            "attachments_after_creation": self.native_payload(
                {
                    "attachments": [
                        {
                            "id": attachment_id,
                            "attachmentType": "worktree",
                            "identityKey": str(path),
                            "payload": {
                                "hostId": self.owner,
                                "root": str(path),
                                "workspaceRoot": str(path),
                                "sourceCwd": str(self.control),
                            },
                        }
                    ]
                }
            ),
        }

    def action_receipt(
        self,
        intent,
        *,
        result_type,
        attachment_id=None,
        identity_key=None,
        head=None,
        state,
        owner=None,
    ):
        operation_id = "operation-" + intent.request.request_uid
        attachment_id = attachment_id or intent.attachment_id
        identity_key = identity_key or intent.attachment_identity_key
        head = head or intent.expected_head
        return {
            "schema": 1,
            "kind": "observed-native-tool-receipts",
            "request_uid": intent.request.request_uid,
            "operation_id": operation_id,
            "action_status": self.native_payload(
                {
                    "operationId": operation_id,
                    "stage": "completed",
                    "result": {
                        "type": result_type,
                        "attachmentId": attachment_id,
                        "identityKey": identity_key,
                        "head": head,
                    },
                }
            ),
            "attachments_after_action": self.native_payload(
                {
                    "attachments": [
                        {
                            "id": attachment_id,
                            "attachmentType": "worktree",
                            "identityKey": identity_key,
                            "state": state,
                            "payload": {
                                "hostId": owner or self.owner,
                                "root": identity_key,
                                "workspaceRoot": identity_key,
                                "sourceCwd": str(self.control),
                            },
                        }
                    ]
                }
            ),
        }

    def create_native(self, suffix="native"):
        request = self.request(suffix)
        intent = self.adapter.plan_create(
            request,
            task_uid="task-" + suffix,
            owner=self.owner,
            actor_binding=self.controller,
        )
        path = self.materialize(request)
        binding = self.adapter.verify_create(
            intent,
            self.create_receipt(request, path),
            path,
        )
        return request, intent, path, binding

    def archive_plan(self, binding, request, *, owner=None, actor_binding=None):
        return self.adapter.plan_archive(
            binding,
            request=request,
            actor_binding=actor_binding or self.controller,
            owner=owner or self.owner,
            evidence_digest=digest_value({"evidence": request.request.request_uid}),
        )

    def assert_replay_is_fenced(self, intent, receipt, confirm):
        result_path=self.adapter.host_actions._result_path(intent.request.request_uid)
        original=result_path.read_bytes()
        changed={**receipt,'operation_id':'different-operation'}
        with self.assertRaisesRegex(WorkflowError,'HOST_ACTION_RESULT_CHANGED'):
            confirm(intent,changed)
        successor=self.binding('successor')
        self.store.transition_controller_bindings(self.controller.binding_uid,1,
            successor.binding_uid,1,2)
        with self.assertRaisesRegex(WorkflowError,'HOST_ACTION_BINDING_STALE'):
            confirm(intent,receipt)
        self.assertEqual(original,result_path.read_bytes())

    def test_host_action_keys_are_exact_and_tool_schemas_are_strict(self):
        self.assertEqual(
            set(validate_host_action_args(CREATE_WORKTREE, {
                "allowAsync": True,
                "ref": self.base,
            })),
            {"allowAsync", "ref"},
        )
        self.assertEqual(
            set(validate_host_action_args(ARCHIVE_WORKTREE, {
                "root": str(self.area / "attachment-control"),
            })),
            {"root"},
        )
        self.assertEqual(
            set(validate_host_action_args(RESTORE_WORKTREE, {
                "root": str(self.area / "attachment-control"),
            })),
            {"root"},
        )
        for action, args in (
            (CREATE_WORKTREE, {"ref": self.base, "extra": True}),
            (
                ARCHIVE_WORKTREE,
                {
                    "root": str(self.area / "attachment-control"),
                    "path": "extra",
                },
            ),
            (
                RESTORE_WORKTREE,
                {
                    "root": str(self.area / "attachment-control"),
                    "head": "extra",
                },
            ),
        ):
            with self.subTest(action=action):
                with self.assertRaisesRegex(
                    WorkflowError, "HOST_ACTION_ARGS_INVALID"
                ):
                    validate_host_action_args(action, args)

        request = self.request("intent-shape")
        value = {
            "schema": 1,
            "state": "pending",
            "action": CREATE_WORKTREE,
            "request": request.request.to_dict(),
            "request_digest": digest_value(request.to_dict()),
            "project_uid": self.project_uid,
            "work_uid": self.controller.work_uid,
            "task_uid": "task-intent-shape",
            "owner": self.owner,
            "binding_uid": self.controller.binding_uid,
            "binding_digest": digest_value({"binding": self.controller.binding_uid}),
            "controller_generation": 1,
            "cancel_generation": 1,
            "attachment_id": None,
            "attachment_identity_key": str(self.control),
            "expected_head": self.base,
            "base_sha": self.base,
            "common_dir": str(self.common),
            "action_args": {"allowAsync": True, "ref": self.base},
            "input_digest": digest_value(
                {
                    "request": request.request.to_dict(),
                    "action_args": {"allowAsync": True, "ref": self.base},
                }
            ),
            "evidence_digest": None,
        }
        value["unknown"] = True
        with self.assertRaisesRegex(
            WorkflowError, "HOST_ACTION_INTENT_INVALID"
        ):
            HostAction.from_dict(value)

    def test_create_intent_is_durable_idempotent_and_fenced(self):
        request = self.request("durable-create")
        first = self.adapter.plan_create(
            request,
            task_uid="task-durable-create",
            owner=self.owner,
            actor_binding=self.controller,
        )
        second = self.adapter.plan_create(
            request,
            task_uid="task-durable-create",
            owner=self.owner,
            actor_binding=self.controller,
        )
        self.assertEqual(first.to_dict(), second.to_dict())
        path = (
            self.control
            / ".gsp"
            / "orchestration"
            / "host-actions"
            / (request.request.request_uid + ".json")
        )
        self.assertTrue(path.is_file())
        pending = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(pending["state"], "pending")
        self.assertEqual(pending["action"], CREATE_WORKTREE)
        self.assertEqual(pending["owner"], self.owner)
        self.assertEqual(pending["attachment_identity_key"], str(self.control))

        changed_owner = self.request("durable-create")
        with self.assertRaisesRegex(
            WorkflowError, "HOST_ACTION_REQUEST_CHANGED"
        ):
            self.adapter.plan_create(
                changed_owner,
                task_uid="task-durable-create",
                owner="foreign-host",
                actor_binding=self.controller,
            )

        stale = self.binding("stale")
        next_controller = self.binding("next")
        self.store.transition_controller_bindings(
            stale.binding_uid, 1, next_controller.binding_uid, 1, 2
        )
        with self.assertRaisesRegex(
            WorkflowError, "HOST_ACTION_BINDING_STALE"
        ):
            self.adapter.plan_create(
                self.request("stale-create"),
                task_uid="task-stale-create",
                owner=self.owner,
                actor_binding=stale,
            )
        self.assertFalse(
            (
                self.control
                / ".gsp"
                / "orchestration"
                / "host-actions"
                / "request-native-worktree-create-stale-create.json"
            ).exists()
        )

    def test_create_confirmation_is_durable_and_rejects_changed_receipt(self):
        request,intent,path,binding=self.create_native('durable-result')
        result=self.adapter.host_action_result(request.request.request_uid)
        self.assertIsNotNone(result)
        self.assertEqual(binding,result.payload['binding'])
        replay=self.adapter.verify_create(intent,self.create_receipt(request,path),path)
        self.assertEqual(binding,replay)
        changed=self.create_receipt(request,path)
        changed['operation_id']='different-create'
        changed['creation_status']=self.native_payload({'operationId':'different-create',
            'stage':'completed','result':{'type':'created','registrationError':None,
            'worktreeGitRoot':str(path),'worktreeWorkspaceRoot':str(path)}})
        with self.assertRaisesRegex(WorkflowError,'HOST_ACTION_RESULT_CHANGED'):
            self.adapter.verify_create(intent,changed,path)

    def test_actual_codex_create_starts_detached_and_is_receipt_bound(self):
        request=self.request('detached-native-create')
        intent=self.adapter.plan_create(request,task_uid='task-detached-native-create',
                                        owner=self.owner,actor_binding=self.controller)
        path=self.materialize(request,detach=True)
        binding=self.adapter.verify_create(intent,self.create_receipt(request,path),path)
        self.assertIsNone(binding['branch'])
        self.assertTrue(binding['verification']['detached'])
        self.assertEqual(self.base,binding['head'])
        self.assertIsNotNone(self.adapter.host_action_result(request.request.request_uid))

    def test_create_requires_actor_and_stale_actor_cannot_confirm(self):
        request = self.request("stale-confirm")
        with self.assertRaisesRegex(
            WorkflowError, "HOST_ACTION_BINDING_REQUIRED"
        ):
            self.adapter.plan_create(
                request,
                task_uid="task-stale-confirm",
                owner=self.owner,
            )
        preview = self.adapter.preview_create(
            request,
            task_uid="task-stale-confirm",
            owner=self.owner,
        )
        self.assertEqual(preview.state, "pending")
        with self.assertRaisesRegex(
            WorkflowError, "HOST_ACTION_BINDING_REQUIRED"
        ):
            self.adapter.verify_create(
                preview,
                self.create_receipt(request, Path(request.path_hint)),
                request.path_hint,
            )
        intent = self.adapter.plan_create(
            request,
            task_uid="task-stale-confirm",
            owner=self.owner,
            actor_binding=self.controller,
        )
        path = self.materialize(request)
        receipt = self.create_receipt(request, path)
        next_controller = self.binding("stale-confirm-next")
        self.store.transition_controller_bindings(
            self.controller.binding_uid,
            1,
            next_controller.binding_uid,
            1,
            2,
            work_uid=self.controller.work_uid,
            run_id="work",
        )

        with self.assertRaisesRegex(
            WorkflowError, "HOST_ACTION_BINDING_STALE"
        ):
            self.adapter.verify_create(intent, receipt, path)
        self.assertIsNone(
            self.adapter.host_action_result(request.request.request_uid)
        )

    def test_archive_requires_native_owner_evidence_and_current_git_identity(self):
        create_request, _, path, binding = self.create_native("archive-valid")
        archive_request = self.request(
            "archive-valid", operation_kind="native-worktree-archive"
        )

        with self.assertRaisesRegex(
            WorkflowError, "CODEX_WORKSPACE_OWNER_MISMATCH"
        ):
            self.archive_plan(binding, archive_request, owner="foreign-host")

        intent = self.archive_plan(binding, archive_request)
        self.assertEqual(intent.action, ARCHIVE_WORKTREE)
        self.assertEqual(intent.attachment_id, "attachment-" + create_request.request.request_uid)
        self.assertEqual(intent.attachment_identity_key, str(path))
        self.assertEqual(intent.expected_head, binding["head"])
        self.assertEqual(intent.state, "pending")

        (path / "drift.txt").write_text("head drift\n", encoding="utf-8")
        self.git(path, "add", "drift.txt")
        self.git(path, "commit", "-qm", "drift")
        drifted = self.git(path, "rev-parse", "HEAD")
        self.git(self.control, "worktree", "remove", "--force", str(path))
        with self.assertRaisesRegex(
            WorkflowError, "CODEX_WORKSPACE_HEAD_DRIFT"
        ):
            self.adapter.confirm_archive(
                intent,
                self.action_receipt(
                    intent,
                    result_type="archived",
                    head=drifted,
                    state="archived",
                ),
            )
        self.assertEqual(
            self.adapter.host_action(intent.request.request_uid).state,
            "pending",
        )
        self.assertFalse(
            self.adapter.host_action_result(intent.request.request_uid)
        )

    def test_missing_workspace_and_receipt_are_not_archive_proof(self):
        _, _, path, binding = self.create_native("missing-proof")
        archive_intent = self.archive_plan(
            binding,
            self.request(
                "missing-proof", operation_kind="native-worktree-archive"
            ),
        )
        self.git(self.control, "worktree", "remove", "--force", str(path))

        with self.assertRaisesRegex(
            WorkflowError, "HOST_ACTION_RECEIPT_REQUIRED"
        ):
            self.adapter.confirm_archive(archive_intent, None)
        self.assertEqual(
            self.adapter.host_action(
                archive_intent.request.request_uid
            ).state,
            "pending",
        )
        with self.assertRaisesRegex(
            WorkflowError, "CODEX_WORKSPACE_SOURCE_PATH_MISMATCH"
        ):
            self.adapter.plan_archive(
                binding,
                request=self.request(
                    "already-missing",
                    operation_kind="native-worktree-archive",
                ),
                actor_binding=self.controller,
                owner=self.owner,
                evidence_digest=digest_value({"evidence": "already-missing"}),
            )

    def test_archive_confirmation_and_response_loss_replay_are_idempotent(self):
        _, _, path, binding = self.create_native("archive-replay")
        intent = self.archive_plan(
            binding,
            self.request(
                "archive-replay", operation_kind="native-worktree-archive"
            ),
        )
        self.git(self.control, "worktree", "remove", "--force", str(path))
        receipt = self.action_receipt(
            intent,
            result_type="archived",
            state="archived",
        )
        first = self.adapter.confirm_archive(intent, receipt)
        second = self.adapter.confirm_archive(intent, receipt)
        self.assertEqual(first, second)
        self.assertEqual(first["state"], "confirmed")
        self.assertEqual(first["action"], ARCHIVE_WORKTREE)
        self.assertNotIn("landed", first)
        self.assertNotIn("closed", first)
        results = list(
            (
                self.control
                / ".gsp"
                / "orchestration"
                / "host-action-results"
            ).glob("*.json")
        )
        self.assertEqual(len(results), 2)  # create and archive each have one durable result
        self.assertEqual({json.loads(file.read_text(encoding='utf-8'))['action']
                          for file in results},{CREATE_WORKTREE,ARCHIVE_WORKTREE})
        self.assertFalse(path.exists())

        self.assert_replay_is_fenced(intent,receipt,self.adapter.confirm_archive)

    def test_archive_confirmation_race_keeps_one_immutable_result(self):
        _, _, path, binding = self.create_native("archive-race")
        intent = self.archive_plan(
            binding,
            self.request(
                "archive-race", operation_kind="native-worktree-archive"
            ),
        )
        self.git(self.control, "worktree", "remove", "--force", str(path))
        first_receipt = self.action_receipt(
            intent,
            result_type="archived",
            state="archived",
        )
        second_receipt = json.loads(json.dumps(first_receipt))
        second_receipt["operation_id"] = "archive-operation-B"
        action_row = second_receipt["action_status"]["value"]["content"][0]
        action_payload = json.loads(action_row["text"])
        action_payload["operationId"] = "archive-operation-B"
        action_row["text"] = json.dumps(action_payload)
        barrier = threading.Barrier(2)
        original_publish = self.adapter.host_actions._publish
        calls = []

        def synchronized_publish(result):
            calls.append(result.operation_id)
            if len(calls) <= 2:
                barrier.wait(timeout=10)
            return original_publish(result)

        self.adapter.host_actions._publish = synchronized_publish
        outputs = []

        def confirm(name, receipt):
            try:
                value = self.adapter.confirm_archive(intent, receipt)
                outputs.append((name, value["operation_id"]))
            except WorkflowError as exc:
                outputs.append((name, "REJECTED " + str(exc)))

        first = threading.Thread(target=confirm, args=("first", first_receipt))
        second = threading.Thread(target=confirm, args=("second", second_receipt))
        first.start()
        second.start()
        first.join()
        second.join()
        accepted = [item for item in outputs if not item[1].startswith("REJECTED")]
        rejected = [item for item in outputs if item[1].startswith("REJECTED")]
        self.assertEqual(len(accepted), 1, outputs)
        self.assertEqual(len(rejected), 1, outputs)
        self.assertIn("HOST_ACTION_RESULT_CHANGED", rejected[0][1])
        winner = self.adapter.host_action_result(intent.request.request_uid)
        self.assertEqual(winner.operation_id, accepted[0][1])
        winning_receipt = (
            first_receipt if accepted[0][0] == "first" else second_receipt
        )
        self.assertEqual(
            self.adapter.confirm_archive(intent, winning_receipt)["operation_id"],
            winner.operation_id,
        )

    def test_archive_rejects_wrong_receipt_attachment_and_stale_binding(self):
        _, _, path, binding = self.create_native("archive-wrong")
        intent = self.archive_plan(
            binding,
            self.request(
                "archive-wrong", operation_kind="native-worktree-archive"
            ),
        )
        self.git(self.control, "worktree", "remove", "--force", str(path))
        with self.assertRaisesRegex(
            WorkflowError, "HOST_ACTION_RECEIPT_ATTACHMENT_MISMATCH"
        ):
            self.adapter.confirm_archive(
                intent,
                self.action_receipt(
                    intent,
                    result_type="archived",
                    attachment_id="wrong-attachment",
                    state="archived",
                ),
            )

        next_controller = self.binding("archive-next")
        self.store.transition_controller_bindings(
            self.controller.binding_uid,
            1,
            next_controller.binding_uid,
            1,
            2,
        )
        with self.assertRaisesRegex(
            WorkflowError, "HOST_ACTION_BINDING_STALE"
        ):
            self.adapter.confirm_archive(
                intent,
                self.action_receipt(
                    intent,
                    result_type="archived",
                    state="archived",
                ),
            )
        self.assertFalse(
            self.adapter.host_action_result(intent.request.request_uid)
        )

    def test_restore_requires_same_archived_attachment_and_detached_snapshot(self):
        create_request, _, path, binding = self.create_native("restore")
        archive_intent = self.archive_plan(
            binding,
            self.request("restore", operation_kind="native-worktree-archive"),
        )
        self.git(self.control, "worktree", "remove", "--force", str(path))
        archived = self.adapter.confirm_archive(
            archive_intent,
            self.action_receipt(
                archive_intent,
                result_type="archived",
                state="archived",
            ),
        )
        restore_request = self.request(
            "restore", operation_kind="native-worktree-restore"
        )
        restore_intent = self.adapter.plan_restore(
            archived,
            request=restore_request,
            actor_binding=self.controller,
            owner=self.owner,
        )
        self.assertEqual(restore_intent.action, RESTORE_WORKTREE)
        self.assertEqual(
            restore_intent.attachment_id,
            "attachment-" + create_request.request.request_uid,
        )

        self.materialize(restore_request, detach=True)
        receipt = self.action_receipt(
            restore_intent,
            result_type="restored",
            head=binding["head"],
            state="active",
        )
        restored = self.adapter.confirm_restore(restore_intent, receipt)
        self.assertEqual(restored["state"], "confirmed")
        self.assertEqual(restored["action"], RESTORE_WORKTREE)
        self.assertTrue(restored["binding"]["registered"])
        self.assertTrue(restored["binding"]["verification"]["detached"])
        self.assertIsNone(restored["binding"]["branch"])
        self.assertEqual(restored["binding"]["head"], binding["head"])
        self.assertEqual(
            Path(restored["binding"]["common_dir"]).resolve(),
            self.common,
        )
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=path,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(branch.returncode, 0)
        self.assertEqual(branch.stdout.strip(), "")

        self.assertEqual(restored,self.adapter.confirm_restore(restore_intent,receipt))
        self.assert_replay_is_fenced(restore_intent,receipt,self.adapter.confirm_restore)

    def test_restore_rejects_wrong_attachment_and_wrong_head(self):
        _, _, path, binding = self.create_native("restore-wrong")
        archive_intent = self.archive_plan(
            binding,
            self.request(
                "restore-wrong", operation_kind="native-worktree-archive"
            ),
        )
        self.git(self.control, "worktree", "remove", "--force", str(path))
        archived = self.adapter.confirm_archive(
            archive_intent,
            self.action_receipt(
                archive_intent,
                result_type="archived",
                state="archived",
            ),
        )
        restore_request = self.request(
            "restore-wrong", operation_kind="native-worktree-restore"
        )
        restore_intent = self.adapter.plan_restore(
            archived,
            request=restore_request,
            actor_binding=self.controller,
            owner=self.owner,
        )
        self.materialize(restore_request, detach=True)

        with self.assertRaisesRegex(
            WorkflowError, "HOST_ACTION_RECEIPT_ATTACHMENT_MISMATCH"
        ):
            self.adapter.confirm_restore(
                restore_intent,
                self.action_receipt(
                    restore_intent,
                    result_type="restored",
                    attachment_id="other-attachment",
                    identity_key=str(path),
                    head=binding["head"],
                    state="active",
                ),
            )
        with self.assertRaisesRegex(
            WorkflowError, "CODEX_WORKSPACE_HEAD_DRIFT"
        ):
            self.adapter.confirm_restore(
                restore_intent,
                self.action_receipt(
                    restore_intent,
                    result_type="restored",
                    head="f" * 40,
                    state="active",
                ),
            )
        self.assertFalse(
            self.adapter.host_action_result(restore_intent.request.request_uid)
        )


if __name__ == "__main__":
    unittest.main()
