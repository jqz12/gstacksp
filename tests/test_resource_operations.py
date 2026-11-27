"""Business-entry tests for native resources and durable evidence."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from gsp.codex_workspace import CodexWorkspaceAdapter
from gsp.core import WorkflowError, digest
from gsp.identity_store import IdentityStore
from gsp.orchestration_types import (
    ObjectRef,
    RequestRef,
    SessionBinding,
    WorktreeRequest,
    digest_text,
    digest_value,
)
from gsp.resource_operations import handle_resource, preserve_work_evidence
from gsp.resource_registry import ResourceOwner, ResourceRegistry
from gsp.session_context import record_binding_authoritative
from gsp.workflow_support import contract_hash


class FixtureProject:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def paths(self, task_id):
        return (
            self.root / ".gsp" / "runs" / task_id,
            self.root / "docs" / "gsp" / "tasks" / task_id,
        )

    def document(self, state, kind):
        return self.paths(state["id"])[1] / (kind + ".md")

    @staticmethod
    def task_definition_hash(state):
        return digest_value(state.get("tasks") or [])


class ResourceOperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-resource-operations-")
        self.addCleanup(self.tmp.cleanup)
        self.area = Path(self.tmp.name).resolve()
        self.root = self.area / "control"
        self.root.mkdir()
        self.git(self.root, "init", "-q", "-b", "main")
        self.git(self.root, "config", "user.name", "Resource Operations")
        self.git(
            self.root, "config", "user.email", "resource@example.invalid"
        )
        (self.root / "README.md").write_text("baseline\n", encoding="utf-8")
        (self.root / "runtime.log").write_text("7 tests passed\n", encoding="utf-8")
        marker = self.root / ".gsp" / "project.json"
        marker.parent.mkdir(parents=True)
        self.project_uid = "resource-operations-project"
        marker.write_text(
            json.dumps({"schema": 1, "id": self.project_uid, "root": str(self.root)}),
            encoding="utf-8",
        )
        self.git(self.root, "add", "README.md", "runtime.log")
        self.git(self.root, "add", "-f", ".gsp/project.json")
        self.git(self.root, "commit", "-qm", "baseline")
        self.base = self.git(self.root, "rev-parse", "HEAD")
        self.project = FixtureProject(self.root)
        self.store = IdentityStore(self.root)
        self.store.initialize()
        self.project_uid = self.store.project_uid()
        work = self.store.allocate(
            "work", None, self.raw_request("work", None)
        )
        task = self.store.allocate(
            "task", work.uid, self.raw_request("task", work.uid)
        )
        session = self.store.allocate(
            "session", None, self.raw_request("session", work.uid)
        )
        self.work_uid = work.uid
        self.task_uid = task.uid
        common = self.git(self.root, "rev-parse", "--git-common-dir")
        self.common = (self.root / common).resolve()
        self.binding = SessionBinding(
            binding_uid="b" * 32,
            session=session,
            work_uid=work.uid,
            kind="main_chat",
            host="codex",
            host_thread_id="thread-resource",
            host_state="verified",
            controller_generation=1,
            cancel_generation=1,
            capabilities=frozenset({"plan", "report", "write"}),
            cancelled=False,
            created_at="2026-11-28T00:00:00+00:00",
            project_uid=self.project_uid,
            common_dir=str(self.common),
            handle_path=".gsp/runs/session-context/binding.json",
        )
        record_binding_authoritative(self.store, self.binding)
        self.store.claim_initial_work_controller(
            self.work_uid, "work", self.binding.binding_uid
        )
        run, docs = self.project.paths("work")
        docs.mkdir(parents=True)
        (docs / "design.md").write_text("# design\n", encoding="utf-8")
        (docs / "plan.md").write_text("# plan\n\n## T01\n", encoding="utf-8")
        (docs / "review.md").write_text("# review\n\nPASS\n", encoding="utf-8")
        self.state = {
            "id": "work",
            "project_id": self.project_uid,
            "work_uuid": self.work_uid,
            "controller_generation": 1,
            "cancel_generation": 1,
            "tasks": [{"id": "T01"}],
            "evidence": [],
            "review": {"decision": "PASS"},
            "inflight": None,
        }
        self.write_state()

    def git(self, cwd, *args):
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def raw_request(self, key, work_uid):
        return RequestRef(
            request_uid=digest_text("request:" + key),
            logical_key=key,
            input_digest=digest_text("input:" + key),
            project_uid=self.project_uid,
            work_uid=work_uid,
            attempt_uid=None,
            operation_kind="fixture",
        )

    def write_state(self):
        path = self.project.paths("work")[0] / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.state), encoding="utf-8")

    def payload(self, name, value):
        folder = self.area / "payloads"
        folder.mkdir(exist_ok=True)
        path = folder / (name + ".json")
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def request_ref(self, uid, operation):
        return RequestRef(
            request_uid=uid,
            logical_key=uid,
            input_digest=digest_value(
                {"request_uid": uid, "operation": operation, "work": self.work_uid}
            ),
            project_uid=self.project_uid,
            work_uid=self.work_uid,
            attempt_uid=None,
            operation_kind=operation,
            controller_generation=1,
            cancel_generation=1,
            contract_digest=contract_hash(
                self.project, self.state, self.project.paths("work")[1]
            ),
        )

    def evidence_args(self, uid, **payload):
        return {
            "command": "evidence-collect",
            "request_uid": uid,
            "task": "T01",
            "source": ["runtime.log"],
            "commit": [self.base],
            "payload": self.payload(uid, payload),
        }

    def test_native_path_conflict_leaves_no_partial_branch_claim(self):
        path=self.area/'occupied-native'
        registry=ResourceRegistry(self.root)
        registry.reserve('foreign-path',ResourceOwner('foreign-work','foreign-session',1),
                         [{'kind':'path','identity':str(path),'disposition':'archive'}])
        ref=self.request_ref('conflicting-native','native-worktree-create')
        request=WorktreeRequest(ref,'codex-native-worktree',str(self.root),self.base,
                                'codex/conflicting-native',str(path))
        result,ok=handle_resource(self.project,self.state,{'command':'native-workspace','action':'create',
            'request_uid':ref.request_uid,'payload':{'request':ref.to_dict(),'worktree':request.to_dict(),
            'owner':'local','task_uid':'T01'}},binding=self.binding)
        self.assertFalse(ok,result)
        self.assertIn('RESOURCE_CONFLICT',result['error'])
        self.assertIsNone(registry.get(ref.request_uid))

    def test_create_plan_failure_before_host_intent_releases_reservation(self):
        request = self.request_ref(
            "plan-failure-before-intent", "native-worktree-create"
        )
        path = self.area / "plan-failure-before-intent"
        worktree = WorktreeRequest(
            request,
            "codex-native-worktree",
            str(self.root),
            self.base,
            "codex/plan-failure-before-intent",
            str(path),
        )
        args = {
            "command": "native-workspace",
            "action": "create",
            "request_uid": request.request_uid,
            "payload": {
                "request": request.to_dict(),
                "worktree": worktree.to_dict(),
                "owner": "local",
                "task_uid": "T01",
            },
        }
        with patch.object(
            CodexWorkspaceAdapter,
            "plan_create",
            side_effect=WorkflowError("PLAN_FAILED_BEFORE_HOST_INTENT"),
        ):
            result, ok = handle_resource(
                self.project, self.state, args, binding=self.binding
            )
        self.assertFalse(ok)
        self.assertIn("PLAN_FAILED_BEFORE_HOST_INTENT", result["error"])
        registry = ResourceRegistry(self.root)
        self.assertEqual(registry.get(request.request_uid)["state"], "released")
        self.assertEqual(registry.list_for_work(self.work_uid), [])
        probe = subprocess.run(
            [
                "git",
                "show-ref",
                "--verify",
                "--quiet",
                "refs/heads/codex/plan-failure-before-intent",
            ],
            cwd=self.root,
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.assertNotEqual(probe.returncode, 0)
        self.assertFalse(path.exists())

    def test_create_plan_failure_after_host_intent_preserves_and_replays(self):
        request = self.request_ref(
            "plan-failure-after-intent", "native-worktree-create"
        )
        path = self.area / "plan-failure-after-intent"
        worktree = WorktreeRequest(
            request,
            "codex-native-worktree",
            str(self.root),
            self.base,
            "codex/plan-failure-after-intent",
            str(path),
        )
        args = {
            "command": "native-workspace",
            "action": "create",
            "request_uid": request.request_uid,
            "payload": {
                "request": request.to_dict(),
                "worktree": worktree.to_dict(),
                "owner": "local",
                "task_uid": "T01",
            },
        }
        original = CodexWorkspaceAdapter.plan_create
        persisted = {}

        def persist_then_fail(adapter, *args_value, **kwargs):
            original(adapter, *args_value, **kwargs)
            persisted["intent"] = adapter.host_action(
                request.request_uid
            ).to_dict()
            raise WorkflowError("PLAN_FAILED_AFTER_HOST_INTENT")

        with patch.object(
            CodexWorkspaceAdapter, "plan_create", new=persist_then_fail
        ):
            result, ok = handle_resource(
                self.project, self.state, args, binding=self.binding
            )
        self.assertFalse(ok)
        self.assertIn("PLAN_FAILED_AFTER_HOST_INTENT", result["error"])
        registry = ResourceRegistry(self.root)
        self.assertEqual(registry.get(request.request_uid)["state"], "reserved")
        adapter = CodexWorkspaceAdapter(self.root, self.project_uid)
        self.assertEqual(
            adapter.host_action(request.request_uid).to_dict(), persisted["intent"]
        )

        replay, ok = handle_resource(
            self.project, self.state, args, binding=self.binding
        )
        self.assertTrue(ok, replay)
        self.assertEqual(
            adapter.host_action(request.request_uid).to_dict(), persisted["intent"]
        )
        self.assertEqual(registry.get(request.request_uid)["state"], "reserved")

    def test_evidence_collect_verify_replay_and_per_receipt_preserve(self):
        args = self.evidence_args("evidence-one")
        first, ok = handle_resource(
            self.project, self.state, args, binding=self.binding
        )
        self.assertTrue(ok, first)
        self.assertEqual(first["quality_status"], "NOT_EVALUATED")
        self.assertTrue(first["manifest"]["git_refs"])
        second, ok = handle_resource(
            self.project, self.state, args, binding=self.binding
        )
        self.assertTrue(ok)
        self.assertEqual(first, second)
        verify_args = {
            "command": "evidence-verify",
            "request_uid": "evidence-verify",
            "payload": self.payload("verify-one", {"manifest": first}),
        }
        verified, ok = handle_resource(
            self.project, self.state, verify_args, binding=self.binding
        )
        self.assertTrue(ok, verified)
        self.assertTrue(verified["accepted"])
        self.assertEqual(verified["landing"], "NOT_PROVEN")

        folder = self.root / ".gsp/runs/work/evidence/e-one"
        folder.mkdir(parents=True)
        stdout = folder / "stdout.txt"
        stdout.write_text("7 tests passed\n", encoding="utf-8")
        result = folder / "command-result.json"
        result.write_text('{"returncode": 0}\n', encoding="utf-8")
        receipt = {
            "logs": {"stdout.txt": digest(stdout)},
            "execution_result": str(result),
        }
        receipt_path = folder / "receipt.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        self.state["evidence"] = [
            receipt_path.relative_to(self.root).as_posix()
        ]
        self.write_state()
        with patch('gsp.resource_operations.fingerprint',side_effect=AssertionError('disposal scanned all source')):
            preserved = preserve_work_evidence(
                self.project,
                self.state,
                binding=self.binding,
                request_uid="preserve-one",
                commits=[self.base],
            )
        self.assertTrue(
            any(
                row["logical_path"].endswith("receipt.json")
                for row in preserved["manifest"]["objects"]
            )
        )

        stdout.unlink()
        with self.assertRaisesRegex(WorkflowError, "EVIDENCE_LOG_REQUIRED"):
            preserve_work_evidence(
                self.project,
                self.state,
                binding=self.binding,
                request_uid="preserve-missing-log",
            )

    def test_evidence_verify_preserves_historical_authority_without_write(self):
        args = self.evidence_args("historical-evidence")
        manifest, ok = handle_resource(
            self.project, self.state, args, binding=self.binding
        )
        self.assertTrue(ok, manifest)

        successor_session = self.store.allocate(
            "session", None, self.raw_request("successor", self.work_uid)
        )
        successor = replace(
            self.binding,
            binding_uid="d" * 32,
            session=successor_session,
            controller_generation=1,
            cancel_generation=1,
        )
        record_binding_authoritative(self.store, successor)
        transition = self.store.transition_controller_bindings(
            self.binding.binding_uid,
            self.binding.controller_generation,
            successor.binding_uid,
            successor.controller_generation,
            2,
            work_uid=self.work_uid,
            run_id="work",
        )
        current = replace(
            successor,
            controller_generation=transition["new"]["controller_generation"],
            cancel_generation=transition["new"]["cancel_generation"],
        )
        state_path = self.project.paths("work")[0] / "state.json"
        before = state_path.read_bytes()
        verified, ok = handle_resource(
            self.project,
            self.state,
            {
                "command": "evidence-verify",
                "request_uid": "historical-verify",
                "payload": self.payload("historical-verify", {"manifest": manifest}),
            },
            binding=current,
        )
        self.assertTrue(ok, verified)
        self.assertTrue(verified["accepted"], verified)
        self.assertEqual(verified["authority_status"], "historical")
        self.assertEqual(verified["quality_status"], "NOT_EVALUATED")
        self.assertEqual(verified["landing"], "NOT_PROVEN")
        self.assertEqual(state_path.read_bytes(), before)

        stale_write, ok = handle_resource(
            self.project,
            self.state,
            self.evidence_args("historical-write"),
            binding=self.binding,
        )
        self.assertFalse(ok)
        self.assertFalse(stale_write["accepted"])
        self.assertEqual(state_path.read_bytes(), before)

    def test_native_create_retry_confirm_and_archive_release_claim(self):
        worktree = self.area / "native-worktree"
        create_request = self.request_ref("create-one", "native-worktree-create")
        create = WorktreeRequest(
            request=create_request,
            owner_adapter="codex-native-worktree",
            source_root=str(self.root),
            base_sha=self.base,
            branch="codex/resource-operations",
            path_hint=str(worktree),
        )
        create_args = {
            "command": "native-workspace",
            "action": "create",
            "request_uid": create_request.request_uid,
            "payload": self.payload(
                "create-one",
                {
                    "request": create_request.to_dict(),
                    "worktree": create.to_dict(),
                    "owner": "host-local",
                },
            ),
        }
        planned, ok = handle_resource(
            self.project, self.state, create_args, binding=self.binding
        )
        self.assertTrue(ok, planned)
        retry, ok = handle_resource(
            self.project, self.state, create_args, binding=self.binding
        )
        self.assertTrue(ok)
        self.assertEqual(planned, retry)
        registry = ResourceRegistry(self.root)
        intent_record = registry.get(create_request.request_uid)
        self.assertEqual(intent_record["state"], "reserved")
        self.assertTrue(
            any(
                row["kind"] == "path" and row["disposition"] == "archive"
                for row in registry.list_for_work(self.work_uid)
            )
        )

        self.git(
            self.root,
            "worktree",
            "add",
            "-b",
            create.branch,
            str(worktree),
            self.base,
        )
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=self.root,
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        )
        (worktree / "head.txt").write_text("head commit\n", encoding="utf-8")
        self.git(worktree, "add", "head.txt")
        self.git(worktree, "commit", "-qm", "head")
        advanced_head = self.git(worktree, "rev-parse", "HEAD")
        confirm_create, ok = handle_resource(
            self.project,
            self.state,
            {
                "command": "native-workspace",
                "action": "confirm",
                "request_uid": create_request.request_uid,
                "payload": self.payload(
                    "confirm-create",
                    {
                        "intent_key": create_request.request_uid,
                        "intent": planned,
                        "receipt": self.create_receipt(create_request, worktree),
                        "source_root": str(worktree),
                        "expected_head": advanced_head,
                    },
                ),
            },
            binding=self.binding,
        )
        self.assertTrue(ok, confirm_create)
        self.assertTrue(confirm_create["registered"])
        archive_uid = confirm_create["archive_claim_request_uid"]
        archive_paths=[row for row in registry.get(archive_uid)['resources'] if row['kind']=='path']
        self.assertEqual([row['disposition'] for row in archive_paths],['archive'])
        self.assertEqual(Path(archive_paths[0]['identity']),worktree)

        base_evidence, ok = handle_resource(
            self.project,
            self.state,
            self.evidence_args("archive-evidence-base"),
            binding=self.binding,
        )
        self.assertTrue(ok, base_evidence)
        archive_request = self.request_ref(
            "archive-one", "native-worktree-archive"
        )
        archive = WorktreeRequest(
            request=archive_request,
            owner_adapter="codex-native-worktree",
            source_root=str(self.root),
            base_sha=self.base,
            branch=create.branch,
            path_hint=str(worktree),
        )
        archive_args = {
            "command": "native-workspace",
            "action": "archive",
            "request_uid": archive_request.request_uid,
            "payload": self.payload(
                "archive-one",
                {
                    "request": archive_request.to_dict(),
                    "worktree": archive.to_dict(),
                    "workspace": confirm_create,
                    "manifest": base_evidence,
                    "owner": "host-local",
                },
            ),
        }
        rejected, ok = handle_resource(
            self.project, self.state, archive_args, binding=self.binding
        )
        self.assertFalse(ok)
        self.assertIn("EVIDENCE_GIT_HEAD_ANCHOR_REQUIRED", rejected["error"])
        head_evidence, ok = handle_resource(
            self.project,
            self.state,
            {
                "command": "evidence-collect",
                "request_uid": "archive-evidence-head",
                "task": "T01",
                "source": ["runtime.log"],
                "commit": [advanced_head],
                "payload": self.payload("archive-evidence-head", {}),
            },
            binding=self.binding,
        )
        self.assertTrue(ok, head_evidence)
        archive_args["payload"] = self.payload(
            "archive-one",
            {
                "request": archive_request.to_dict(),
                "worktree": archive.to_dict(),
                "workspace": confirm_create,
                "manifest": head_evidence,
                "owner": "host-local",
            },
        )
        dirty = worktree / "uncommitted.txt"
        dirty.write_text("must not be lost\n", encoding="utf-8")
        rejected, ok = handle_resource(
            self.project, self.state, archive_args, binding=self.binding
        )
        self.assertFalse(ok)
        self.assertIn("CODEX_WORKSPACE_DIRTY", rejected["error"])
        dirty.unlink()

        own_owner = ResourceOwner(
            work_uid=self.work_uid,
            session_uid=self.binding.session.uid,
            generation=1,
        )
        registry.reserve_execution_slot("current-work-slot", own_owner)
        rejected, ok = handle_resource(
            self.project, self.state, archive_args, binding=self.binding
        )
        self.assertFalse(ok)
        self.assertIn("RESOURCE_EXECUTION_SLOT_ACTIVE", rejected["error"])
        registry.release_execution_slot("current-work-slot", own_owner)
        unrelated_owner = ResourceOwner(
            work_uid="unrelated-work",
            session_uid="u" * 32,
            generation=1,
        )
        registry.reserve_execution_slot("unrelated-slot", unrelated_owner)
        archive_intent, ok = handle_resource(
            self.project, self.state, archive_args, binding=self.binding
        )
        self.assertTrue(ok, archive_intent)
        self.git(self.root, "worktree", "remove", "--force", str(worktree))
        archived, ok = handle_resource(
            self.project,
            self.state,
            {
                "command": "native-workspace",
                "action": "confirm",
                "request_uid": archive_request.request_uid,
                "payload": self.payload(
                    "confirm-archive",
                    {
                        "intent_key": archive_request.request_uid,
                        "receipt": self.action_receipt(
                            archive_intent, result_type="archived", state="archived"
                        ),
                    },
                ),
            },
            binding=self.binding,
        )
        self.assertTrue(ok, archived)
        self.assertEqual(registry.get(archive_uid)["state"], "released")
        active = registry.list_for_work(self.work_uid)
        self.assertFalse(
            any(
                row["kind"] == "path" and row["disposition"] == "archive"
                for row in active
            )
        )
        status, ok = handle_resource(
            self.project,
            self.state,
            {
                "command": "native-workspace",
                "action": "status",
                "request_uid": create_request.request_uid,
                "payload": self.payload("status-missing", {}),
            },
            binding=self.binding,
        )
        self.assertTrue(ok, status)
        self.assertFalse(worktree.exists())

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
        operation_id = "create-" + request.request_uid
        attachment_id = "attachment-" + request.request_uid
        return {
            "schema": 1,
            "kind": "observed-native-tool-receipts",
            "request_uid": request.request_uid,
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
                                "hostId": "host-local",
                                "root": str(path),
                                "workspaceRoot": str(path),
                                "sourceCwd": str(self.root),
                            },
                        }
                    ]
                }
            ),
        }

    def action_receipt(self, intent, *, result_type, state):
        operation_id = "operation-" + intent["request"]["request_uid"]
        return {
            "schema": 1,
            "kind": "observed-native-tool-receipts",
            "request_uid": intent["request"]["request_uid"],
            "operation_id": operation_id,
            "action_status": self.native_payload(
                {
                    "operationId": operation_id,
                    "stage": "completed",
                    "result": {
                        "type": result_type,
                        "attachmentId": intent["attachment_id"],
                        "identityKey": intent["attachment_identity_key"],
                        "head": intent["expected_head"],
                    },
                }
            ),
            "attachments_after_action": self.native_payload(
                {
                    "attachments": [
                        {
                            "id": intent["attachment_id"],
                            "attachmentType": "worktree",
                            "identityKey": intent["attachment_identity_key"],
                            "state": state,
                            "payload": {
                                "hostId": intent["owner"],
                                "root": intent["attachment_identity_key"],
                                "workspaceRoot": intent["attachment_identity_key"],
                                "sourceCwd": str(self.root),
                            },
                        }
                    ]
                }
            ),
        }


if __name__ == "__main__":
    unittest.main()
