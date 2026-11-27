"""Durable raw evidence and Git reachability tests."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from gsp.core import WorkflowError
from gsp.evidence_store import (
    collect_evidence,
    persist_git_objects,
    verify_git_reachability,
    verify_manifest,
)
from gsp.identity_store import IdentityStore
from gsp.orchestration_types import (
    RequestRef,
    SessionBinding,
    digest_text,
    digest_value,
)


def record_binding_authoritative(store, binding):
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
    store.record_session_binding(binding.binding_uid, canonical)
    store.record_session_state(
        binding.binding_uid,
        binding.controller_generation,
        binding.cancel_generation,
        binding.cancelled,
    )


class EvidenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-evidence-store-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Evidence Store Test")
        self.git("config", "user.email", "evidence-store@example.invalid")
        (self.root / "README.md").write_text("# evidence\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-qm", "chore: baseline")
        (self.root / ".gsp").mkdir()
        (self.root / ".gsp/project.json").write_text(
            json.dumps({"id": "evidence-project", "root": str(self.root)}),
            encoding="utf-8",
        )
        self.store = IdentityStore(self.root)
        self.store.initialize()
        self.work = self.store.allocate(
            "work", None, self.raw_request("allocate-work", work_uid=None)
        )
        self.task = self.store.allocate(
            "task",
            self.work.uid,
            self.raw_request("allocate-task", work_uid=self.work.uid),
        )
        self.session = self.store.allocate(
            "session",
            None,
            self.raw_request("allocate-session", work_uid=self.work.uid),
        )
        self.binding = SessionBinding(
            binding_uid="b" * 32,
            session=self.session,
            work_uid=self.work.uid,
            kind="main",
            host="codex",
            host_thread_id="thread-evidence",
            host_state="unverified",
            controller_generation=2,
            cancel_generation=3,
            capabilities=frozenset({"report", "write"}),
            cancelled=False,
            created_at="2026-11-27T00:00:00+00:00",
            project_uid=self.store.project_uid(),
            common_dir=str((self.root / ".git").resolve()),
            handle_path=".gsp/runs/work/sessions/S01.json",
        )
        record_binding_authoritative(self.store, self.binding)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.log = self.workspace / "run.log"
        self.log.write_text("tests: 10 passed\nresult: PASS\n", encoding="utf-8")
        self.artifact = self.workspace / "result.json"
        self.artifact.write_text(
            json.dumps({"status": "PASS", "tests": 10}), encoding="utf-8"
        )
        self.contract_digest = "c" * 64
        self.input_digest = digest_value(
            {
                "workspace": "workspace",
                "log": "run.log",
                "artifact": "result.json",
            }
        )

    def git(self, *args, check=True):
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            ),
        )
        if check and result.returncode:
            self.fail(result.stdout + result.stderr)
        return result.stdout.strip()

    def raw_request(self, logical_key, *, work_uid, operation="fixture"):
        return RequestRef(
            request_uid=digest_text("request:" + logical_key),
            logical_key=logical_key,
            input_digest=digest_text(logical_key),
            project_uid=self.store.project_uid(),
            work_uid=work_uid,
            attempt_uid=None,
            operation_kind=operation,
        )

    def request(self, logical_key="evidence:T01", input_digest=None,
                request_uid=None, controller_generation=2,
                cancel_generation=3):
        value = input_digest or self.input_digest
        return RequestRef(
            request_uid=request_uid or digest_value(
                {
                    "logical_key": logical_key,
                    "input_digest": value,
                    "work_uid": self.work.uid,
                }
            ),
            logical_key=logical_key,
            input_digest=value,
            project_uid=self.store.project_uid(),
            work_uid=self.work.uid,
            attempt_uid=None,
            operation_kind="evidence-collect",
            controller_generation=controller_generation,
            cancel_generation=cancel_generation,
            contract_digest=self.contract_digest,
        )

    def collect(self, *, sources=None, binding=None, request=None,
                input_digest=None):
        input_digest = input_digest or self.input_digest
        request = request or self.request(input_digest=input_digest)
        sources = sources or [
            {
                "path": self.workspace,
                "label": "workspace",
                "kind": "directory",
                "required": True,
            }
        ]
        return collect_evidence(
            self.root,
            self.work.uid,
            self.task.uid,
            sources,
            self.contract_digest,
            input_digest,
            binding=binding or self.binding,
            request=request,
        )

    def manifest_files(self):
        root = self.root / ".gsp/evidence/manifests" / self.work.uid
        return sorted(root.glob("*.json")) if root.is_dir() else []

    def object_row(self, manifest, logical_path):
        return next(
            row
            for row in manifest["manifest"]["objects"]
            if row["logical_path"] == logical_path
        )

    def test_collect_survives_workspace_deletion_and_does_not_claim_quality(self):
        manifest = self.collect()
        first = verify_manifest(self.root, manifest)
        self.assertTrue(first["accepted"], first["errors"])
        self.assertEqual(first["quality_status"], "NOT_EVALUATED")
        self.assertEqual(first["git_proof_digest"], None)
        log_row = self.object_row(manifest, "workspace/run.log")
        stored = self.root / log_row["object_path"]
        expected_log = self.log.read_bytes()
        self.assertEqual(stored.read_bytes(), expected_log)
        for path in sorted(self.workspace.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        self.workspace.rmdir()
        second = verify_manifest(self.root, manifest)
        self.assertTrue(second["accepted"], second["errors"])
        self.assertEqual(
            (self.root / self.object_row(manifest, "workspace/run.log")[
                "object_path"
            ]).read_bytes(),
            expected_log,
        )

    def test_missing_or_unreadable_required_source_publishes_no_manifest(self):
        missing = self.workspace / "missing.log"
        with self.assertRaisesRegex(
            WorkflowError, "EVIDENCE_SOURCE_MISSING"
        ):
            self.collect(sources=[{"path": missing, "required": True}])
        self.assertEqual(self.manifest_files(), [])
        with mock.patch("gsp.evidence_store.os.access", return_value=False):
            with self.assertRaisesRegex(
                WorkflowError, "EVIDENCE_SOURCE_UNREADABLE"
            ):
                self.collect(
                    request=self.request(logical_key="evidence:unreadable")
                )
        self.assertEqual(self.manifest_files(), [])

    def test_source_escape_and_symlink_are_rejected(self):
        outside = Path(self.tmp.name) / "outside.log"
        outside.write_text("outside\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "EVIDENCE_SOURCE_ESCAPE"):
            self.collect(sources=[outside])
        self.assertEqual(self.manifest_files(), [])
        link = self.workspace / "escape.log"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlink unavailable")
        with self.assertRaisesRegex(
            WorkflowError, "EVIDENCE_SOURCE_LINK_REFUSED"
        ):
            self.collect(sources=[self.workspace])
        self.assertEqual(self.manifest_files(), [])

    def test_object_tampering_is_rejected(self):
        manifest = self.collect()
        log_row = self.object_row(manifest, "workspace/run.log")
        object_path = self.root / log_row["object_path"]
        original = object_path.read_bytes()
        object_path.write_bytes(
            bytes([original[0] ^ 1]) + original[1:]
        )
        result = verify_manifest(self.root, manifest)
        self.assertFalse(result["accepted"])
        self.assertIn("EVIDENCE_OBJECT_TAMPERED", result["errors"])

    def test_manifest_tampering_is_rejected(self):
        manifest = self.collect(
            request=self.request(logical_key="evidence:manifest-tamper")
        )
        manifest_path = self.root / verify_manifest(self.root, manifest)[
            "manifest_path"
        ]
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        value["manifest"]["task_uid"] = "changed"
        manifest_path.write_text(json.dumps(value), encoding="utf-8")
        result = verify_manifest(self.root, manifest)
        self.assertFalse(result["accepted"])
        self.assertTrue(
            any("TAMPERED" in error or "CONFLICT" in error
                for error in result["errors"])
        )

    def test_request_replay_is_idempotent_and_input_change_is_rejected(self):
        request = self.request()
        first = self.collect(request=request)
        files = self.manifest_files()
        second = self.collect(request=request)
        self.assertEqual(first, second)
        self.assertEqual(self.manifest_files(), files)
        changed = self.request(
            input_digest=digest_value({"changed": True}),
            request_uid=digest_text("changed-request-uid"),
        )
        with self.assertRaisesRegex(WorkflowError, "REQUEST_INPUT_CHANGED"):
            self.collect(
                request=changed,
                input_digest=changed.input_digest,
            )
        self.assertEqual(self.manifest_files(), files)

    def test_wrong_work_and_stale_actor_are_rejected(self):
        other = self.store.allocate(
            "work", None, self.raw_request("other-work", work_uid=None)
        )
        wrong_binding = replace(self.binding, work_uid=other.uid)
        wrong_request = replace(
            self.request(),
            project_uid=self.store.project_uid(),
            work_uid=other.uid,
        )
        with self.assertRaisesRegex(WorkflowError, "WORK_UID_MISMATCH"):
            self.collect(binding=wrong_binding, request=wrong_request)
        manifest = self.collect()
        stale_session = self.store.allocate(
            "session",
            None,
            self.raw_request("successor-session", work_uid=self.work.uid),
        )
        successor = replace(
            self.binding,
            binding_uid="d" * 32,
            session=stale_session,
            controller_generation=1,
            cancel_generation=1,
        )
        record_binding_authoritative(self.store, successor)
        self.store.transition_controller_bindings(
            self.binding.binding_uid,
            self.binding.controller_generation,
            successor.binding_uid,
            successor.controller_generation,
            4,
            work_uid=self.work.uid,
            run_id="work",
        )
        result = verify_manifest(self.root, manifest)
        # Readonly integrity of published evidence survives a controller
        # handoff. This grants no permission to collect or publish new refs.
        self.assertTrue(result["accepted"],result)
        self.assertEqual('historical',result['authority_status'])
        self.assertEqual('NOT_EVALUATED',result['quality_status'])
        with self.assertRaisesRegex(WorkflowError, "SESSION_BINDING_STALE"):
            self.collect(
                request=self.request(logical_key="evidence:stale-writer")
            )

    def test_git_refs_survive_branch_delete_and_proof_tamper_is_rejected(self):
        self.git("switch", "-q", "-c", "feature/evidence")
        (self.root / "commit.txt").write_text("one\n", encoding="utf-8")
        self.git("add", "commit.txt")
        self.git("commit", "-qm", "test: evidence commit")
        commit = self.git("rev-parse", "HEAD")
        self.git("switch", "-q", "main")
        self.git("branch", "-D", "feature/evidence")
        manifest = self.collect()
        request = self.request()
        proofed = persist_git_objects(
            self.root, self.binding, manifest, [commit], request
        )
        raw = verify_manifest(self.root, proofed)
        self.assertTrue(raw["accepted"], raw["errors"])
        self.assertEqual(raw["git_refs"], [
            "refs/gsp/evidence/{}/{}/{}".format(
                self.work.uid, proofed["manifest"]["uid"], commit
            )
        ])
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD"), "main")
        reachable = verify_git_reachability(self.root, proofed)
        self.assertTrue(reachable["accepted"], reachable["errors"])
        self.assertEqual(reachable["landing"], "NOT_PROVEN")
        replay = persist_git_objects(
            self.root, self.binding, manifest, [commit], request
        )
        self.assertEqual(proofed, replay)
        proof_path = (
            self.root
            / ".gsp/evidence/git-proofs"
            / self.work.uid
            / (proofed["manifest"]["uid"] + ".json")
        )
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        proof["commits"][0]["commit"] = "0" * len(commit)
        proof_path.write_text(json.dumps(proof), encoding="utf-8")
        tampered = verify_git_reachability(self.root, proofed)
        self.assertFalse(tampered["accepted"])
        self.assertIn("EVIDENCE_GIT_PROOF_TAMPERED", tampered["errors"])

    def test_git_commit_change_is_rejected_before_new_ref(self):
        commits = []
        for index in range(2):
            (self.root / "commit.txt").write_text(
                "{}\n".format(index), encoding="utf-8"
            )
            self.git("add", "commit.txt")
            self.git("commit", "-qm", "test: commit {}".format(index))
            commits.append(self.git("rev-parse", "HEAD"))
        manifest = self.collect()
        request = self.request()
        first = persist_git_objects(
            self.root, self.binding, manifest, commits[:1], request
        )
        with self.assertRaisesRegex(WorkflowError, "REQUEST_INPUT_CHANGED"):
            persist_git_objects(
                self.root, self.binding, manifest, commits[1:], request
            )
        self.assertEqual(
            verify_git_reachability(self.root, first)["accepted"], True
        )

    def test_non_git_and_unbound_manifests_are_not_accepted(self):
        manifest = collect_evidence(
            self.root,
            self.work.uid,
            self.task.uid,
            [self.log],
            self.contract_digest,
            self.input_digest,
            binding=None,
            request=None,
        )
        result = verify_manifest(self.root, manifest)
        self.assertFalse(result["accepted"])
        self.assertIn("EVIDENCE_AUTHORITY_REQUIRED", result["errors"])


if __name__ == "__main__":
    unittest.main()
