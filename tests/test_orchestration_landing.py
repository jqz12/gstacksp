"""Real-repository tests for the bounded Git landing business API."""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid

from gsp.identity_store import IdentityStore
from gsp.landing import (
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_LANDED,
    STATE_PENDING,
    STATE_PREPARED,
    land,
    load_landing,
    prepare_landing,
    recover_landing,
)
from gsp.orchestration_types import (
    LandingRequest,
    RequestRef,
    SessionBinding,
    digest_text,
    digest_value,
)
from gsp.resource_registry import ResourceRegistry
from gsp.session_context import record_binding_authoritative


@unittest.skipUnless(shutil.which("git"), "git is required")
class LandingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-landing-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        self.git("config", "user.name", "Landing Fixture")
        self.git("config", "user.email", "landing@example.invalid")
        exclude = self.root / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(".gsp/\n", encoding="utf-8")
        (self.root / "base.txt").write_text("base\n", encoding="utf-8")
        (self.root / "shared.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "--", "base.txt", "shared.txt")
        self.git("commit", "-q", "-m", "base")
        self.base = self.rev("HEAD")
        self.candidate = self.commit_on(
            "candidate", "candidate.txt", "candidate\n", "candidate",
            create=True,
        )
        self.git("checkout", "-q", "main")
        (self.root / ".gsp").mkdir()
        (self.root / ".gsp" / "project.json").write_text(
            json.dumps({"id": "landing-project", "root": str(self.root)}),
            encoding="utf-8",
        )
        self.store = IdentityStore(self.root)
        self.store.initialize()
        self.work = self.allocate("work", "landing-work")
        self.session = self.allocate(
            "session", "landing-session", work_uid=self.work.uid,
        )
        self.binding = SessionBinding(
            binding_uid=self.session.uid + "-binding",
            session=self.session,
            work_uid=self.work.uid,
            kind="main",
            host="codex",
            host_thread_id="landing-thread",
            host_state="unverified",
            controller_generation=1,
            cancel_generation=1,
            capabilities=frozenset({"write", "plan"}),
            cancelled=False,
            created_at="2026-11-27T00:00:00+00:00",
            project_uid=self.store.project_uid(),
            common_dir=str((self.root / ".git").resolve()),
            handle_path=".gsp/runs/main/sessions/S01.json",
        )
        record_binding_authoritative(self.store, self.binding)
        self.gate_calls = []

    def allocate(self, kind, logical_key, work_uid=None):
        return self.store.allocate(
            kind,
            None,
            RequestRef(
                request_uid=uuid.uuid4().hex,
                logical_key=logical_key,
                input_digest=digest_text(logical_key),
                project_uid=self.store.project_uid(),
                work_uid=work_uid,
                attempt_uid=None,
                operation_kind="allocate-" + kind,
            ),
        )

    def git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout

    def rev(self, value):
        return self.git("rev-parse", "--verify", value).strip()

    def commit_on(
        self, branch, name, text, message, *, create=False, start=None,
    ):
        if create:
            self.git("checkout", "-q", "-b", branch, start or "main")
        else:
            self.git("checkout", "-q", branch)
        (self.root / name).write_text(text, encoding="utf-8")
        self.git("add", "--", name)
        self.git("commit", "-q", "-m", message)
        return self.rev("HEAD")

    def allow_gate(self, context):
        self.gate_calls.append(dict(context))
        return {
            "accepted": True,
            "rule_digest": "rule-fixture",
            "phase": context["phase"],
        }

    def allow_verify(self, context):
        return {
            "accepted": True,
            "candidate_sha": context["candidate_sha"],
            "target_ref": context["target_ref"],
            "expected_target_sha": context["expected_target_sha"],
            "evidence_uid": context["evidence_uid"],
            "objects": ["quality-1"],
        }

    def request(
        self,
        *,
        target_ref="refs/heads/main",
        expected=None,
        candidate=None,
        evidence_uid="evidence-1",
        request_uid=None,
        binding=None,
    ):
        request_uid = request_uid or uuid.uuid4().hex
        source = RequestRef(
            request_uid=request_uid,
            logical_key="landing:" + request_uid,
            input_digest=digest_text(
                digest_value({
                    "target_ref": target_ref,
                    "expected": expected or self.base,
                    "candidate": candidate or self.candidate,
                })
            ),
            project_uid=self.store.project_uid(),
            work_uid=self.work.uid,
            attempt_uid="landing-attempt",
            operation_kind="landing",
            controller_generation=1,
            cancel_generation=1,
        )
        return LandingRequest(
            request=source,
            binding=binding or self.binding,
            target_ref=target_ref,
            expected_target_sha=expected or self.base,
            candidate_sha=candidate or self.candidate,
            evidence_uid=evidence_uid,
        )

    def prepare(self, request=None, **kwargs):
        kwargs.setdefault("check_gate", self.allow_gate)
        kwargs.setdefault("verify_candidate", self.allow_verify)
        return prepare_landing(self.root, request or self.request(), **kwargs)

    def test_checked_out_fast_forward_is_published_without_force(self):
        prepared = self.prepare()
        self.assertEqual(prepared["state"], STATE_PREPARED)
        self.assertTrue(prepared["fast_forward"])
        self.assertEqual(prepared["final_sha"], self.candidate)
        self.assertEqual(self.rev("refs/heads/main"), self.base)

        landed = land(self.root, prepared, check_gate=self.allow_gate)

        self.assertEqual(landed["state"], STATE_LANDED)
        self.assertEqual(landed["actual_sha"], self.candidate)
        self.assertEqual(self.rev("HEAD"), self.candidate)
        self.assertEqual(self.rev("refs/heads/main"), self.candidate)
        self.assertEqual(
            (self.root / "candidate.txt").read_text(encoding="utf-8"),
            "candidate\n",
        )
        replay = land(self.root, landed["uid"], check_gate=self.allow_gate)
        self.assertEqual(replay["proof_uid"], landed["proof_uid"])

    def test_current_successor_can_finish_original_intent_and_old_actor_cannot_replay(self):
        prepared = self.prepare()
        successor_session = self.allocate('session', 'successor', work_uid=self.work.uid)
        successor = replace(self.binding, binding_uid=successor_session.uid+'-binding',
                            session=successor_session, host_thread_id='successor',
                            capabilities=frozenset({'write', 'accept', 'plan'}))
        record_binding_authoritative(self.store, successor)
        self.store.transition_controller_bindings(self.binding.binding_uid, 1,
            successor.binding_uid, 1, 2, work_uid=self.work.uid, run_id='landing-work')
        successor = replace(successor, controller_generation=2)
        original_input = prepared['input']
        with self.assertRaisesRegex(Exception, 'SESSION_BINDING_STALE'):
            land(self.root, prepared['uid'], binding=self.binding, check_gate=self.allow_gate)
        self.assertEqual(STATE_PREPARED, load_landing(self.root, prepared['uid'])['state'])
        result = land(self.root, prepared['uid'], binding=successor, check_gate=self.allow_gate)
        self.assertEqual(STATE_LANDED, result['state'])
        self.assertEqual(original_input, result['input'])
        replay = recover_landing(self.root, prepared['uid'], binding=successor,
                                 check_gate=self.allow_gate)
        self.assertEqual(result['proof_uid'], replay['proof_uid'])
        with self.assertRaisesRegex(Exception, 'SESSION_BINDING_STALE'):
            recover_landing(self.root, prepared['uid'], binding=self.binding)

    def test_target_advanced_after_prepare_is_refused(self):
        prepared = self.prepare()
        advanced = self.commit_on(
            "main", "advanced.txt", "advanced\n", "advance target",
        )

        with self.assertRaisesRegex(Exception, "LANDING_TARGET_ADVANCED"):
            land(self.root, prepared, check_gate=self.allow_gate)

        record = load_landing(self.root, prepared["uid"])
        self.assertEqual(record["state"], STATE_FAILED)
        self.assertEqual(self.rev("refs/heads/main"), advanced)
        self.assertFalse((self.root / "candidate.txt").exists())

    def test_dirty_source_worktree_blocks_then_recovers_after_cleanup(self):
        prepared = self.prepare()
        (self.root / "base.txt").write_text("dirty\n", encoding="utf-8")

        with self.assertRaisesRegex(Exception, "LANDING_WORKTREE_DIRTY"):
            land(self.root, prepared, check_gate=self.allow_gate)

        self.assertEqual(
            load_landing(self.root, prepared["uid"])["state"], STATE_PENDING,
        )
        self.assertEqual(self.rev("refs/heads/main"), self.base)

        self.git("checkout", "--", "base.txt")
        recovered = recover_landing(
            self.root, prepared["uid"], check_gate=self.allow_gate,
        )
        self.assertEqual(recovered["state"], STATE_LANDED)
        self.assertEqual(self.rev("refs/heads/main"), self.candidate)

    def test_changed_index_blocks_landing(self):
        prepared = self.prepare()
        (self.root / "base.txt").write_text("staged\n", encoding="utf-8")
        self.git("add", "--", "base.txt")

        with self.assertRaisesRegex(Exception, "LANDING_WORKTREE_DIRTY"):
            land(self.root, prepared, check_gate=self.allow_gate)

        self.assertEqual(
            load_landing(self.root, prepared["uid"])["state"], STATE_PENDING,
        )
        self.assertEqual(self.rev("refs/heads/main"), self.base)

    def test_diverged_candidate_builds_verified_merge_without_checkout_change(self):
        target = self.commit_on(
            "main", "target.txt", "target\n", "advance main",
        )
        prepared = self.prepare(self.request(expected=target))

        self.assertFalse(prepared["fast_forward"])
        self.assertNotIn(prepared["final_sha"], {target, self.candidate})
        parents = self.git(
            "show", "-s", "--format=%P", prepared["final_sha"]
        ).strip().split()
        self.assertEqual(parents, [target, self.candidate])
        self.assertEqual(self.rev("HEAD"), target)
        self.assertFalse((self.root / "candidate.txt").exists())

        landed = land(self.root, prepared, check_gate=self.allow_gate)

        self.assertEqual(landed["state"], STATE_LANDED)
        self.assertEqual(self.rev("HEAD"), prepared["final_sha"])
        self.assertTrue((self.root / "target.txt").is_file())
        self.assertTrue((self.root / "candidate.txt").is_file())

    def test_merge_conflict_is_a_failed_intent(self):
        self.git("checkout", "-q", "-b", "conflict-candidate", self.base)
        (self.root / "shared.txt").write_text("candidate\n", encoding="utf-8")
        self.git("add", "--", "shared.txt")
        self.git("commit", "-q", "-m", "candidate conflict")
        candidate = self.rev("HEAD")
        self.git("checkout", "-q", "main")
        (self.root / "shared.txt").write_text("target\n", encoding="utf-8")
        self.git("add", "--", "shared.txt")
        self.git("commit", "-q", "-m", "target conflict")
        target = self.rev("HEAD")

        with self.assertRaisesRegex(Exception, "LANDING_CANDIDATE_CONFLICT"):
            self.prepare(
                self.request(expected=target, candidate=candidate),
            )

        records = list((self.root / ".gsp" / "landing").glob("*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(record["state"], STATE_FAILED)
        self.assertEqual(self.rev("refs/heads/main"), target)

    def test_gate_change_after_prepare_blocks_all_git_mutation(self):
        def changing_gate(context):
            self.gate_calls.append(dict(context))
            return {"accepted": context["phase"] == "prepare"}

        prepared = self.prepare(
            self.request(), check_gate=changing_gate,
        )
        with self.assertRaisesRegex(Exception, "LANDING_GATE_BLOCKED"):
            land(self.root, prepared, check_gate=changing_gate)

        record = load_landing(self.root, prepared["uid"])
        self.assertEqual(record["state"], STATE_PENDING)
        self.assertTrue(record["last_error"].startswith("LANDING_GATE_BLOCKED"))
        self.assertEqual(self.rev("refs/heads/main"), self.base)

        landed = recover_landing(
            self.root, prepared["uid"], check_gate=self.allow_gate,
        )
        self.assertEqual(landed["state"], STATE_LANDED)
        self.assertEqual(self.rev("refs/heads/main"), self.candidate)
        self.assertIn("land", [item["phase"] for item in self.gate_calls])

    def test_missing_gate_is_fail_closed_and_recovery_can_complete(self):
        with self.assertRaisesRegex(Exception, "LANDING_GATE_UNAVAILABLE"):
            self.prepare(self.request(), check_gate=None)

        records = list((self.root / ".gsp" / "landing").glob("*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(record["state"], STATE_PENDING)
        self.assertEqual(self.rev("refs/heads/main"), self.base)

        landed = recover_landing(
            self.root, record["uid"], check_gate=self.allow_gate,
        )
        self.assertEqual(landed["state"], STATE_LANDED)
        self.assertEqual(self.rev("refs/heads/main"), self.candidate)

    def test_response_loss_after_update_ref_is_recovered(self):
        self.git("branch", "release", self.base)
        prepared = self.prepare(
            self.request(target_ref="refs/heads/release"),
        )
        self.git(
            "update-ref",
            "refs/heads/release",
            prepared["final_sha"],
            prepared["expected_target_sha"],
        )

        recovered = recover_landing(self.root, prepared["uid"])

        self.assertEqual(recovered["state"], STATE_LANDED)
        self.assertEqual(recovered["actual_sha"], prepared["final_sha"])
        self.assertEqual(
            recovered["proof"]["method"], "recovered-existing-effect",
        )
        self.assertEqual(self.rev("refs/heads/release"), prepared["final_sha"])
        self.assertEqual(self.rev("refs/heads/main"), self.base)

    def test_non_checked_out_target_uses_cas_update_ref(self):
        self.git("branch", "release", self.base)
        prepared = self.prepare(
            self.request(target_ref="refs/heads/release"),
        )

        landed = land(self.root, prepared, check_gate=self.allow_gate)

        self.assertEqual(landed["state"], STATE_LANDED)
        self.assertEqual(landed["proof"]["method"], "update-ref")
        self.assertEqual(self.rev("refs/heads/release"), self.candidate)
        self.assertEqual(self.rev("refs/heads/main"), self.base)
        self.assertEqual(self.rev("HEAD"), self.base)
        self.assertFalse((self.root / "candidate.txt").exists())

    def test_cancel_before_prepare_has_no_resource_or_git_effect(self):
        request = self.request()
        record = self.prepare(request, cancel_check=lambda _context: True)

        self.assertEqual(record["state"], STATE_CANCELLED)
        self.assertEqual(self.rev("refs/heads/main"), self.base)
        self.assertIsNone(
            ResourceRegistry(self.root).get(record["resource_request_uid"]),
        )

    def test_cancel_before_land_prevents_landing_after_land_does_not_rollback(self):
        prepared = self.prepare(
            self.request(), cancel_check=lambda _context: False,
        )
        cancelled = land(
            self.root,
            prepared,
            check_gate=self.allow_gate,
            cancel_check=lambda _context: True,
        )
        self.assertEqual(cancelled["state"], STATE_CANCELLED)
        self.assertEqual(self.rev("refs/heads/main"), self.base)

        second = self.prepare(
            self.request(), cancel_check=lambda _context: False,
        )
        landed = land(self.root, second, check_gate=self.allow_gate)
        self.assertEqual(landed["state"], STATE_LANDED)
        after = land(
            self.root,
            landed,
            check_gate=self.allow_gate,
            cancel_check=lambda _context: True,
        )
        self.assertEqual(after["state"], STATE_LANDED)
        self.assertEqual(after["proof_uid"], landed["proof_uid"])
        self.assertEqual(self.rev("refs/heads/main"), self.candidate)

    def test_same_input_replays_but_changed_input_is_rejected(self):
        request = self.request()
        first = self.prepare(request)
        replay = self.prepare(request)
        self.assertEqual(first["uid"], replay["uid"])
        self.assertEqual(first["final_sha"], replay["final_sha"])
        self.assertEqual(first["input_digest"], replay["input_digest"])

        changed = self.request(
            request_uid=request.request.request_uid,
            candidate=self.base,
        )
        with self.assertRaisesRegex(
            Exception, "LANDING_REQUEST_INPUT_CHANGED"
        ):
            self.prepare(changed)

    def test_unbound_verification_cannot_mark_landed(self):
        with self.assertRaisesRegex(
            Exception, "LANDING_VERIFICATION_REJECTED"
        ):
            self.prepare(
                self.request(),
                verify_candidate=lambda _context: {"accepted": True},
            )

        records = list((self.root / ".gsp" / "landing").glob("*.json"))
        record = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(record["state"], STATE_FAILED)
        self.assertEqual(self.rev("refs/heads/main"), self.base)

    def test_tampered_binding_is_rejected_before_persisting_intent(self):
        tampered = replace(
            self.binding,
            capabilities=frozenset({"write", "report"}),
        )
        with self.assertRaisesRegex(Exception, "SESSION_CONTEXT_TAMPERED"):
            self.prepare(self.request(binding=tampered))
        landing_dir = self.root / ".gsp" / "landing"
        self.assertFalse(landing_dir.exists() and any(landing_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
