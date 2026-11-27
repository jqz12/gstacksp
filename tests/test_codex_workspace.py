"""Codex-native workspace intent and attachment verification."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from gsp.codex_workspace import CodexCreateIntent, CodexWorkspaceAdapter
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
from gsp.worktree_adapters import WorktreeAdapterError


class CodexWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-codex-workspace-")
        self.addCleanup(self.tmp.cleanup)
        self.area = Path(self.tmp.name).resolve()
        self.control = self.area / "control"
        self.control.mkdir()
        self.git(self.control, "init", "-q", "-b", "main")
        (self.control / "README.md").write_text("# fixture\n", encoding="utf-8")
        self.git(self.control, "add", "README.md")
        self.git(self.control, "commit", "-qm", "baseline")
        self.base = self.git(self.control, "rev-parse", "HEAD")
        self.project_uid = "project-codex-workspace"
        marker = self.control / ".gsp/project.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(
            json.dumps({"schema": 1, "id": self.project_uid}),
            encoding="utf-8",
        )
        self.common = (
            self.control
            / self.git(self.control, "rev-parse", "--git-common-dir")
        ).resolve()
        store = IdentityStore(self.control)
        store.initialize()
        work_request = RequestRef(
            request_uid="request-workspace-work",
            logical_key="workspace-work",
            input_digest=digest_value({"fixture": "workspace-work"}),
            project_uid=self.project_uid,
            work_uid=None,
            attempt_uid=None,
            operation_kind="allocate",
        )
        self.work_uid = store.allocate(
            "work", None, work_request
        ).uid
        self.actor = SessionBinding(
            binding_uid=digest_value(
                {"kind": "binding", "fixture": "workspace-actor"}
            )[:32],
            session=ObjectRef(
                uid=digest_value({"kind": "session", "fixture": "workspace"})[:32],
                kind="session",
                project_uid=self.project_uid,
                canonical_id="S-workspace",
            ),
            work_uid=self.work_uid,
            kind="main_chat",
            host="codex",
            host_thread_id="thread-workspace",
            host_state="verified",
            controller_generation=1,
            cancel_generation=1,
            capabilities=frozenset(
                {"accept", "claim", "dispatch", "handoff", "plan", "report", "write"}
            ),
            cancelled=False,
            created_at="2026-11-28T00:00:00+00:00",
            project_uid=self.project_uid,
            common_dir=str(self.common),
            handle_path=".gsp/runs/session-context/workspace-actor.json",
        )
        record_binding_authoritative(store, self.actor)
        store.claim_initial_work_controller(
            self.work_uid, "work", self.actor.binding_uid
        )
        self.adapter = CodexWorkspaceAdapter(
            self.control, expected_project_uid=self.project_uid
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
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def request(self, suffix):
        path = self.area / ("external-" + suffix)
        branch = "codex/GSP-000034-T05-" + suffix
        ref = RequestRef(
            request_uid="request-" + suffix,
            logical_key="workspace-create-" + suffix,
            input_digest=digest_value(
                {
                    "project_uid": self.project_uid,
                    "work_uid": self.work_uid,
                    "base_sha": self.base,
                    "branch": branch,
                    "path": str(path),
                }
            ),
            project_uid=self.project_uid,
            work_uid=self.work_uid,
            attempt_uid=None,
            operation_kind="native-worktree-create",
        )
        return WorktreeRequest(
            request=ref,
            owner_adapter="codex-native-worktree",
            source_root=str(self.control),
            base_sha=self.base,
            branch=branch,
            path_hint=str(path),
        )

    def materialize(self, request):
        self.git(
            self.control,
            "worktree",
            "add",
            "-b",
            request.branch,
            request.path_hint,
            request.base_sha,
        )
        path = Path(request.path_hint).resolve()
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "worktree", "remove", "--force", str(path)],
                cwd=self.control,
                capture_output=True,
            )
        )
        return path

    def receipt(self, intent, path, *, source_cwd=None):
        operation_id = "operation-" + intent.request.request.request_uid
        attachment_id = "attachment-" + intent.request.request.request_uid

        def payload(value):
            return {
                "status": "fulfilled",
                "value": {
                    "isError": False,
                    "content": [{"type": "text", "text": json.dumps(value)}],
                },
            }

        return {
            "schema": 1,
            "kind": "observed-native-tool-receipts",
            "request_uid": intent.request.request.request_uid,
            "operation_id": operation_id,
            "creation_status": payload(
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
            "attachments_after_creation": payload(
                {
                    "attachments": [
                        {
                            "id": attachment_id,
                            "attachmentType": "worktree",
                            "identityKey": str(path),
                            "payload": {
                                "hostId": "host-local",
                                "root": str(path),
                                "workspaceRoot": str(path),
                                "sourceCwd": str(
                                    self.control if source_cwd is None else source_cwd
                                ),
                            },
                        }
                    ]
                }
            ),
        }

    def test_create_intent_is_pending_external_and_idempotent(self):
        request = self.request("intent")
        first = self.adapter.plan_create(
            request,
            task_uid="task-intent",
            owner="host-local",
            actor_binding=self.actor,
        )
        second = self.adapter.plan_create(
            request,
            task_uid="task-intent",
            owner="host-local",
            actor_binding=self.actor,
        )

        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.state, "pending")
        self.assertEqual(first.kind, "worktree-create")
        self.assertFalse(Path(request.path_hint).is_relative_to(self.control))
        self.assertFalse(Path(request.path_hint).exists())
        restored = CodexCreateIntent.from_dict(first.to_dict())
        self.assertEqual(restored.to_dict(), first.to_dict())
        tampered = first.to_dict()
        tampered["request"]["branch"] = "codex/GSP-000034-T05-tampered"
        with self.assertRaisesRegex(
            WorkflowError, "CODEX_WORKSPACE_REQUEST_CHANGED"
        ):
            CodexCreateIntent.from_dict(tampered)

    def test_native_attachment_requires_real_git_and_only_plans_archive(self):
        request = self.request("native")
        intent = self.adapter.plan_create(
            request,
            task_uid="task-native",
            owner="host-local",
            actor_binding=self.actor,
        )
        path = self.materialize(request)
        binding = self.adapter.verify_create(intent, self.receipt(intent, path), path)

        self.assertEqual(binding["ownership"], "native")
        self.assertTrue(binding["verification"]["registered"])
        self.assertEqual(
            Path(binding["common_dir"]),
            (
                self.control
                / self.git(self.control, "rev-parse", "--git-common-dir")
            ).resolve(),
        )
        self.assertEqual(binding["project_uid"], self.project_uid)

        pending = self.adapter.cleanup_plan(binding, reason="merged")
        self.assertEqual(pending["kind"], "worktree-archive")
        self.assertEqual(pending["state"], "pending")
        self.assertEqual(pending["action"], "archive")
        self.assertFalse(pending["deletable"])
        self.assertTrue(path.is_dir())
        tampered = dict(binding)
        tampered["path"] = str(self.area / "foreign")
        with self.assertRaisesRegex(
            WorkflowError, "CODEX_NATIVE_RECEIPT_REQUIRED"
        ):
            self.adapter.cleanup_plan(tampered)

    def test_control_marker_must_match_expected_project_uid(self):
        marker = self.control / ".gsp/project.json"
        marker.unlink()
        with self.assertRaisesRegex(
            WorkflowError, "CODEX_CONTROL_MARKER_MISSING"
        ):
            self.adapter.plan_create(
                self.request("marker"),
                task_uid="task-marker",
                owner="host-local",
            )

    def test_unregistered_copy_is_rejected_even_with_project_marker(self):
        request = self.request("copy")
        intent = self.adapter.plan_create(
            request,
            task_uid="task-copy",
            owner="host-local",
            actor_binding=self.actor,
        )
        path = Path(request.path_hint)
        path.mkdir()
        marker = path / ".gsp/project.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(
            json.dumps({"schema": 1, "id": self.project_uid}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            WorkflowError, "NATIVE_WORKSPACE_UNREGISTERED"
        ):
            self.adapter.verify_create(intent, self.receipt(intent, path), path)

    def test_attachment_must_match_control_and_project_uid(self):
        request = self.request("attachment")
        intent = self.adapter.plan_create(
            request,
            task_uid="task-attachment",
            owner="host-local",
            actor_binding=self.actor,
        )
        path = self.materialize(request)

        with self.assertRaisesRegex(WorkflowError, "NATIVE_ATTACHMENT_MISMATCH"):
            self.adapter.verify_create(
                intent,
                self.receipt(intent, path, source_cwd=self.area / "foreign"),
                path,
            )

        marker = path / ".gsp/project.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps({"schema": 1, "id": "foreign-project"}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            WorkflowError, "NATIVE_SOURCE_PROJECT_MISMATCH"
        ):
            self.adapter.verify_create(intent, self.receipt(intent, path), path)

    def test_registered_worktree_from_another_repository_is_rejected(self):
        request = self.request("foreign-common")
        intent = self.adapter.plan_create(
            request,
            task_uid="task-foreign-common",
            owner="host-local",
            actor_binding=self.actor,
        )
        foreign = self.area / "foreign-control"
        foreign.mkdir()
        self.git(foreign, "init", "-q", "-b", "main")
        (foreign / "README.md").write_text("# foreign\n", encoding="utf-8")
        self.git(foreign, "add", "README.md")
        self.git(foreign, "commit", "-qm", "foreign baseline")
        foreign_base = self.git(foreign, "rev-parse", "HEAD")
        path = Path(request.path_hint)
        self.git(
            foreign,
            "worktree",
            "add",
            "-b",
            request.branch,
            str(path),
            foreign_base,
        )
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "worktree", "remove", "--force", str(path)],
                cwd=foreign,
                capture_output=True,
            )
        )

        with self.assertRaisesRegex(
            WorkflowError, "NATIVE_COMMON_DIR_MISMATCH"
        ):
            self.adapter.verify_create(
                intent, self.receipt(intent, path), path, expected_head=foreign_base
            )

    def test_bound_releases_and_legacy_managed_keeps_remove_disposition(self):
        request = self.request("ownership")
        path = self.materialize(request)

        bound = self.adapter.bind_existing(path, ownership="bound")
        release = self.adapter.cleanup_plan(bound)
        self.assertEqual(release["action"], "release")
        self.assertFalse(release["deletable"])
        self.assertIsNone(release["host_action"])
        self.assertTrue(path.is_dir())

        legacy = self.adapter.bind_existing(path, ownership="legacy-managed")
        remove = self.adapter.cleanup_plan(legacy)
        self.assertEqual(remove["action"], "remove")
        self.assertTrue(remove["deletable"])
        self.assertIsNone(remove["host_action"])
        self.assertTrue(path.is_dir())

        with self.assertRaisesRegex(
            WorktreeAdapterError, "CODEX_NATIVE_RECEIPT_REQUIRED"
        ):
            self.adapter.bind_existing(path, ownership="native")


if __name__ == "__main__":
    unittest.main()
