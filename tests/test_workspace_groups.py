"""Sequential workspace-group reuse over existing resource authorities."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from gsp.orchestration_types import digest_value
from gsp.resource_registry import ResourceRegistry
from gsp.runtime import Project, WorkflowError, dispatch, parser, read
from gsp.dispatch import dispatch_tasks, load_dispatch
from gsp.git_topology import workspace_group_binding_projection
from gsp.sessions import (
    _save as save_sessions,
    authorize_edit,
    load_registry,
    register_session,
    release_session_lease,
)
from gsp.workspace_groups import (
    SessionWorkspaceLeaseAuthority,
    WorkspaceGroup,
    build_workspace_group,
    claim_workspace_group,
    evaluate_workspace_group,
    release_workspace_group,
    workspace_group_projection,
)
try:
    from process_helpers import session_binding
except ModuleNotFoundError:
    from tests.process_helpers import session_binding


class WorkspaceGroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-workspace-groups-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Workspace Group Test")
        self.git("config", "user.email", "workspace-group@example.invalid")
        (self.root / "README.md").write_text("# group\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-qm", "chore: baseline")
        self.project = Project(self.root)
        self.project.initialize()
        self.project_uid = self.project.identity()["id"]
        self.work_uid = "work-uid"
        self.head = self.git("rev-parse", "HEAD")
        self.state = {
            "schema": 1,
            "id": "work",
            "number": "GSP-000034",
            "root": str(self.root),
            "project_id": self.project_uid,
            "work_uuid": self.work_uid,
            "status": "execute",
            "tasks": [
                {"id": "T00", "title": "baseline", "status": "done"},
                {"id": "T01", "title": "first slice", "status": "execute"},
                {"id": "T02", "title": "second slice", "status": "pending"},
            ],
        }
        self.state_path = self.root / ".gsp/runs/work/state.json"
        self.registry_path = self.root / ".gsp/runs/work/sessions/index.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.registry = {
            "schema": 1,
            "task": "work",
            "number": "GSP-000034",
            "controller_session": "S01",
            "next_session": 4,
            "workspace_leases": {self.norm_path(self.root): "S01"},
            "sessions": [
                self.session("S01", "controller", None, "controller-binding"),
                self.session("S02", "worker", "T01", "worker-one-binding"),
                self.session("S03", "worker", "T02", "worker-two-binding"),
            ],
            "tasks": {
                "T00": {"acceptance_status": "accepted"},
                "T01": {},
                "T02": {},
            },
            "events": [],
            "outbox": [],
        }
        self.save_registry()
        (self.registry_path.parent / "index.md").write_text(
            "# projection sentinel\n", encoding="utf-8"
        )
        self.group = build_workspace_group(
            self.root,
            self.root,
            self.root,
            project_uid=self.project_uid,
            work_uid=self.work_uid,
            parent_binding_uid="controller-binding",
            controller_binding_uid="controller-binding",
            controller_generation=1,
            cancel_generation=1,
            source_writer_binding_uid="worker-one-binding",
        )

    def git(self, *argv):
        result = subprocess.run(
            ["git", *argv],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    @staticmethod
    def norm_path(path):
        return str(Path(path).resolve()).casefold()

    def session(self, session_id, role, task, binding_uid):
        return {
            "id": session_id,
            "role": role,
            "host": "codex",
            "thread_id": "thread-" + session_id,
            "task": task,
            "branch": "main",
            "worktree": str(self.root),
            "write_mode": "write" if role != "controller" else "read-only",
            "claimed_paths": ["."],
            "status": "active",
            "started_at": "2026-11-27T00:00:00+00:00",
            "updated_at": "2026-11-27T00:00:00+00:00",
            "context": "context-" + session_id,
            "context_mode": "bound",
            "binding_uid": binding_uid,
            "work_uid": self.work_uid,
            "controller_generation": 1,
            "cancel_generation": 1,
        }

    def save_registry(self):
        self.registry_path.write_text(
            json.dumps(self.registry, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def claim(self, group=None, **overrides):
        values = {
            "project_uid": self.project_uid,
            "work_uid": self.work_uid,
            "control_root": self.root,
            "workspace_root": self.root,
            "branch": "main",
            "task_uid": "T01",
            "contract_digest": digest_value({"task": "T01"}),
            "source_writer_session_id": "S02",
            "source_writer_binding_uid": "worker-one-binding",
            "source_writer_generation": 1,
            "source_writer_cancel_generation": 1,
            "resource_registry": ResourceRegistry(self.root),
            "lease_authority": SessionWorkspaceLeaseAuthority(
                self.root, "work"
            ),
        }
        values.update(overrides)
        return claim_workspace_group(group or self.group, **values)

    def test_claim_persists_one_root_branch_request_and_source_writer_lease(self):
        projection_path = self.registry_path.parent / "index.md"
        projection_before = projection_path.read_bytes()
        receipt = self.claim()

        registry = ResourceRegistry(self.root)
        stored = registry.get(self.group.resource_request_uid)
        self.assertEqual(stored["state"], "committed")
        self.assertEqual(stored["owner"]["session_uid"], self.group.group_uid)
        self.assertEqual(
            {(row["kind"], row["identity"]) for row in stored["resources"]},
            {
                ("path", registry._canonical_path(str(self.root))),
                ("branch", "main"),
            },
        )
        current = read(self.registry_path)
        self.assertEqual(
            current["workspace_leases"][self.norm_path(self.root)], "S02"
        )
        worker = next(row for row in current["sessions"] if row["id"] == "S02")
        self.assertEqual(
            worker["workspace_group"]["group_uid"], self.group.group_uid
        )
        self.assertEqual(receipt.state, "committed")
        self.assertEqual(receipt.host_observation, "unverified")
        self.assertEqual(
            [row for row in registry.list_for_work(self.work_uid)
             if row["kind"] == "slot"],
            [],
        )
        self.assertEqual(projection_path.read_bytes(), projection_before)

        before = self.registry_path.read_bytes()
        replay = self.claim()
        self.assertEqual(replay.lease_revision, receipt.lease_revision)
        self.assertEqual(self.registry_path.read_bytes(), before)

    def test_parallel_mode_only_produces_host_pending_and_creates_nothing(self):
        worktrees_before = self.git("worktree", "list", "--porcelain")
        eligibility = evaluate_workspace_group(
            self.group,
            project_uid=self.project_uid,
            work_uid=self.work_uid,
            control_root=self.root,
            workspace_root=self.root,
            branch="main",
            task_uid="T01",
            contract_digest=digest_value({"task": "T01"}),
            requested_mode="parallel",
            controller_binding_uid="controller-binding",
            controller_generation=1,
            cancel_generation=1,
        )
        self.assertFalse(eligibility.eligible)
        self.assertEqual(eligibility.mode, "pending_host")
        self.assertTrue(eligibility.requires_new_context)
        self.assertTrue(eligibility.requires_independent_review)

        with self.assertRaisesRegex(
            WorkflowError, "WORKSPACE_GROUP_CREATE_REQUIRED"
        ):
            self.claim(requested_mode="parallel")
        self.assertIsNone(
            ResourceRegistry(self.root).get(self.group.resource_request_uid)
        )
        self.assertEqual(
            self.git("worktree", "list", "--porcelain"),
            worktrees_before,
        )

    def test_controller_cannot_be_source_writer(self):
        group = replace(
            self.group, source_writer_binding_uid="controller-binding"
        )
        before = self.registry_path.read_bytes()
        with self.assertRaisesRegex(
            WorkflowError, "WORKSPACE_GROUP_CONTROLLER_WRITE_FORBIDDEN"
        ):
            self.claim(
                group,
                source_writer_session_id="S01",
                source_writer_binding_uid="controller-binding",
            )
        self.assertEqual(self.registry_path.read_bytes(), before)
        self.assertIsNone(
            ResourceRegistry(self.root).get(self.group.resource_request_uid)
        )

    def test_foreign_work_root_branch_and_stale_generation_are_blocked(self):
        checks = [
            ({"work_uid": "foreign-work"}, "WORKSPACE_GROUP_WORK_MISMATCH"),
            ({"workspace_root": self.root.parent}, "WORKSPACE_GROUP_ROOT_MISMATCH"),
            ({"branch": "other"}, "WORKSPACE_GROUP_BRANCH_MISMATCH"),
            ({"source_writer_generation": 2}, "WORKSPACE_GROUP_STALE_GENERATION"),
        ]
        for override, error in checks:
            with self.subTest(error=error):
                with self.assertRaisesRegex(WorkflowError, error):
                    self.claim(**override)
        self.assertIsNone(
            ResourceRegistry(self.root).get(self.group.resource_request_uid)
        )

    def test_unknown_inflight_blocks_before_any_claim_is_persisted(self):
        with self.assertRaisesRegex(
            WorkflowError, "WORKSPACE_GROUP_EXECUTION_UNKNOWN"
        ):
            self.claim(unknown_inflight=True)
        self.assertIsNone(
            ResourceRegistry(self.root).get(self.group.resource_request_uid)
        )

    def test_stale_lease_revision_keeps_a_resumable_group_intent(self):
        before = self.registry_path.read_bytes()
        with self.assertRaisesRegex(
            WorkflowError, "WORKSPACE_GROUP_LEASE_REVISION_MISMATCH"
        ):
            self.claim(expected_lease_revision="0" * 64)
        self.assertEqual(self.registry_path.read_bytes(), before)
        self.assertEqual(
            ResourceRegistry(self.root).get(
                self.group.resource_request_uid
            )["state"],
            "reserved",
        )
        receipt = self.claim(expected_lease_revision=self.lease_revision())
        self.assertEqual(receipt.state, "committed")
        self.assertEqual(
            ResourceRegistry(self.root).get(
                self.group.resource_request_uid
            )["state"],
            "committed",
        )

    def test_same_writer_cannot_change_contract_in_place(self):
        self.claim()
        with self.assertRaisesRegex(
            WorkflowError, "WORKSPACE_GROUP_CLAIM_CHANGED"
        ):
            self.claim(contract_digest=digest_value({"task": "T01-v2"}))
        self.assertEqual(
            ResourceRegistry(self.root).get(
                self.group.resource_request_uid
            )["state"],
            "committed",
        )

    def test_next_task_requires_previous_acceptance_and_ended_worker(self):
        self.claim()
        self.registry = read(self.registry_path)
        self.registry["workspace_leases"][self.norm_path(self.root)] = "S02"
        self.save_registry()
        second = replace(
            self.group, source_writer_binding_uid="worker-two-binding"
        )

        with self.assertRaisesRegex(
            WorkflowError, "WORKSPACE_GROUP_PREVIOUS_TASK_UNACCEPTED"
        ):
            self.claim(
                second,
                task_uid="T02",
                contract_digest=digest_value({"task": "T02"}),
                source_writer_session_id="S03",
                source_writer_binding_uid="worker-two-binding",
            )

        self.registry = read(self.registry_path)
        self.registry["workspace_leases"].pop(self.norm_path(self.root))
        self.registry["tasks"]["T01"]["acceptance_status"] = "accepted"
        next(
            row for row in self.registry["sessions"] if row["id"] == "S02"
        )["status"] = "released"
        self.save_registry()

        receipt = self.claim(
            second,
            task_uid="T02",
            contract_digest=digest_value({"task": "T02"}),
            source_writer_session_id="S03",
            source_writer_binding_uid="worker-two-binding",
            expected_lease_revision=self.lease_revision(),
        )
        self.assertEqual(receipt.source_writer_session_id, "S03")
        self.assertEqual(
            read(self.registry_path)["workspace_leases"][
                self.norm_path(self.root)
            ],
            "S03",
        )
        self.assertEqual(
            ResourceRegistry(self.root).get(
                self.group.resource_request_uid
            )["state"],
            "committed",
        )

    def test_explicit_legacy_mode_never_claims_native_ownership(self):
        eligibility = evaluate_workspace_group(
            None,
            project_uid=self.project_uid,
            work_uid=self.work_uid,
            control_root=self.root,
            workspace_root=self.root,
            branch="main",
            task_uid="T01",
            contract_digest=digest_value({"task": "T01"}),
            requested_mode="sequential",
            controller_binding_uid="controller-binding",
            controller_generation=1,
            cancel_generation=1,
            legacy_compatibility=True,
        )
        self.assertTrue(eligibility.eligible)
        self.assertEqual(eligibility.mode, "legacy_managed")
        self.assertIn("host_observation=unverified", eligibility.reasons)

    def test_release_requires_acceptance_and_keeps_root_branch_claim(self):
        receipt = self.claim()
        authority = SessionWorkspaceLeaseAuthority(self.root, "work")
        with self.assertRaisesRegex(
            WorkflowError, "WORKSPACE_GROUP_TASK_NOT_ACCEPTED"
        ):
            release_workspace_group(
                self.group,
                task_uid="T01",
                source_writer_session_id="S02",
                expected_lease_revision=receipt.lease_revision,
                lease_authority=authority,
            )

        self.registry = read(self.registry_path)
        self.registry["tasks"]["T01"]["acceptance_status"] = "accepted"
        self.save_registry()
        released = release_workspace_group(
            self.group,
            task_uid="T01",
            source_writer_session_id="S02",
            expected_lease_revision=self.lease_revision(),
            lease_authority=authority,
        )
        self.assertTrue(released["released"])
        self.assertNotIn(
            self.norm_path(self.root),
            read(self.registry_path)["workspace_leases"],
        )
        self.assertEqual(
            ResourceRegistry(self.root).get(
                self.group.resource_request_uid
            )["state"],
            "committed",
        )

    def test_projection_is_read_only(self):
        self.claim()
        database_before = ResourceRegistry(self.root).path.read_bytes()
        registry_before = self.registry_path.read_bytes()
        projection = workspace_group_projection(
            self.group,
            resource_registry=ResourceRegistry(self.root),
            lease_authority=SessionWorkspaceLeaseAuthority(
                self.root, "work"
            ),
        )
        self.assertEqual(projection["source_writer_session_id"], "S02")
        self.assertEqual(projection["host_observation"], "unverified")
        self.assertEqual(
            ResourceRegistry(self.root).path.read_bytes(), database_before
        )
        self.assertEqual(self.registry_path.read_bytes(), registry_before)

        topology = workspace_group_binding_projection(
            self.state, "T01", self.group, projection,
        )
        self.assertEqual(topology["topology"]["workspace_root"], str(self.root.resolve()))
        self.assertEqual(topology["ownership"]["group_uid"], self.group.group_uid)
        self.assertEqual(topology["cleanup_action"]["action"], "release-source-writer")
        self.assertEqual(topology["verification"]["host_observation"], "unverified")
        self.assertEqual(topology["verification"]["native_host_receipt"], "NOT_CHECKED")

    def lease_revision(self):
        registry = read(self.registry_path)
        return digest_value(
            {
                "schema": 1,
                "workspace_leases": registry["workspace_leases"],
            }
        )


class SequentialWorkspaceWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-workspace-wiring-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Workspace Wiring Test")
        self.git("config", "user.email", "workspace-wiring@example.invalid")
        (self.root / "README.md").write_text("# wiring\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-qm", "chore: baseline")
        self.project = Project(self.root)
        self.project.initialize()
        self.cmd("new", "work", "--title", "顺序工作区", "--intent", "cross-session")
        self.cmd(
            "task-add", "work", "T01", "--title", "first",
            "--acceptance", "first acceptance",
        )
        self.cmd(
            "task-add", "work", "T02", "--title", "second",
            "--acceptance", "second acceptance",
        )
        self.cmd(
            "task-add", "work", "T03", "--title", "dependent",
            "--acceptance", "dependent acceptance", "--depends", "T01",
        )
        self.state = self.read_state()
        self.parent = self.root.parent / "parent"
        # Most tests below exercise the legacy direct workspace-group provider.
        # The normal-stage test explicitly opts back into sealed contracts.
        self.state.pop('operation_schema',None)
        self.git("worktree", "add", "-q", "-b", "feature/parent", str(self.parent))
        self.state["git_topology"] = {
            "schema": 1,
            "status": "selected",
            "intent": "cross-session",
            "effective_mode": "worktree",
            "control_root": str(self.root),
            "workspace_root": str(self.parent),
            "branch": "feature/parent",
            "head": self.git("rev-parse", "HEAD"),
        }
        self.project.write(
            self.state, "fixture", "workspace group parent root",
            action_uid="fixture:workspace-group-parent",
        )
        plan = self.project.document(self.state, "plan")
        plan.write_text(
            "# Plan\n\n## Global Constraints\n\nKeep all gates.\n\n"
            "## Task T01: first\n\nFirst slice.\n\n"
            '**Writes:** {"paths":["src/first.py"]}\n\n'
            "## Task T02: second\n\nSecond slice.\n\n"
            '**Writes:** {"paths":["src/second.py"]}\n\n'
            "## Task T03: dependent\n\nDependent slice.\n\n"
            '**Writes:** {"paths":["src/dependent.py"]}\n',
            encoding="utf-8",
        )
        self.project.document(self.state, "design").write_text(
            "# Design\n\nUse one sequential parent root.\n",
            encoding="utf-8",
        )
        self.controller_binding = session_binding(
            self.root, self.state["work_uuid"], "main", "codex", "main-thread",
        )
        register_session(
            self.project,
            self.state,
            parser().parse_args([
                "session-register", "work", "--role", "controller",
                "--host", "codex", "--thread", "main-thread",
                "--worktree", str(self.parent), "--write-mode", "write",
                "--path", ".", "--context",
                str(self.root / self.controller_binding.handle_path),
            ]),
        )

    def git(self, *argv):
        result = subprocess.run(
            ["git", *argv], cwd=self.root, capture_output=True, text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def cmd(self, *argv):
        result, ok = dispatch(
            self.project, parser().parse_args(list(argv)),
        )
        self.assertTrue(ok, result)
        return result

    def read_state(self):
        return read(self.root / ".gsp" / "runs" / "work" / "state.json")

    def dispatch_group(self, task):
        args = parser().parse_args([
            "session-dispatch", "work", "--session", "S01",
            "--workspace-group", "parent",
            "--task", task, "--target", f"{task}=codex",
            "--path", f"{task}=" + (
                "src/first.py" if task == "T01" else "src/second.py"
            ),
        ])
        return dispatch_tasks(self.project, self.state, args)

    def test_group_dispatch_recovers_grant_before_dispatch_projection(self):
        real_reserve=ResourceRegistry.reserve_execution_batch
        def lose_response(registry,*args,**kwargs):
            real_reserve(registry,*args,**kwargs)
            raise OSError('group grant response lost')
        with patch.object(ResourceRegistry,'reserve_execution_batch',lose_response):
            with self.assertRaisesRegex(OSError,'group grant response lost'):
                self.dispatch_group('T01')
        pending=load_dispatch(self.project,self.state)['dispatches']
        self.assertEqual(1,len(pending))
        self.assertEqual('preparing',pending[0]['status'])
        registry=ResourceRegistry(self.root)
        before=registry.scheduler_snapshot()
        result=self.dispatch_group('T01')
        self.assertEqual(pending[0]['id'],result['dispatches'][0]['id'])
        self.assertEqual('workspace-pending',result['dispatches'][0]['status'])
        self.assertEqual(before,registry.scheduler_snapshot())
        self.assertEqual(1,len(load_dispatch(self.project,self.state)['dispatches']))

    def register_worker(self, session_id, task, thread):
        binding = session_binding(
            self.root, self.state["work_uuid"], "child", "codex", thread,
        )
        args = parser().parse_args([
            "session-register", "work", "--session", session_id,
            "--workspace-group", "parent",
            "--role", "worker", "--host", "codex", "--thread", thread,
            "--task", task, "--worktree", str(self.parent),
            "--write-mode", "write", "--path",
            "src/first.py" if task == "T01" else "src/second.py",
            "--context", str(self.root / binding.handle_path),
        ])
        registered = register_session(self.project, self.state, args)
        return binding, registered

    def test_normal_stage_entry_uses_sequential_layout_and_requires_the_live_writer(self):
        from tests.process_helpers import temporary_env
        from gsp.runtime import validate_parallel_bindings, parallel_required, git_identity
        self.state = self.read_state()
        self.state['git'] = git_identity(self.parent)
        self.state['operation_schema']=2
        self.project.write(self.state, 'fixture', 'normal selected source identity',
                           action_uid='fixture:normal-source-identity')
        plan = self.project.document(self.state, 'plan')
        text = plan.read_text(encoding='utf-8')
        for sentence in ('First slice.', 'Second slice.','Dependent slice.'):
            text = text.replace(sentence, sentence + '\nConvergence: '
                                '{"requires_slices":false,"single_outcome":true,"small":true}')
        plan.write_text(text, encoding='utf-8')
        context = self.root / self.controller_binding.handle_path

        def command(*argv):
            with temporary_env({'GSP_SESSION_CONTEXT': str(context)}):
                return dispatch(self.project, parser().parse_args(argv), enforce_context=True)

        self.assertTrue(parallel_required(self.read_state()))
        self.assertIn('GIT_INTEGRATION_REQUIRED', {
            row['code'] for row in validate_parallel_bindings(self.read_state())['blocking']})
        for argv in (('approve', 'work', '--quote', 'SYNTHETIC approve'),
                     ('seal-plan', 'work'),
                     ('session-dispatch', 'work', '--session', 'S01', '--task', 'T01',
                      '--workspace-group', 'parent', '--target', 'T01=codex', '--path', 'T01=src/first.py')):
            result, ok = command(*argv)
            self.assertTrue(ok, result)
        self.state = self.read_state()
        self.assertFalse(parallel_required(self.state))
        self.assertTrue(validate_parallel_bindings(self.state)['valid'])
        result, ok = command('advance', 'work', '--to', 'execute')
        self.assertTrue(ok, result)
        self.state = self.read_state()
        self.register_worker('S02', 'T01', 'normal-worker-one')
        result, ok = command('task-start', 'work', 'T01')
        self.assertTrue(ok, result)
        self.assertEqual(self.read_state()['tasks'][0]['status'], 'running')
        with self.assertRaisesRegex(WorkflowError, 'SOURCE_WRITER_REQUIRED'):
            command('task-start', 'work', 'T02')

    def test_two_tasks_reuse_one_parent_root_with_new_bindings_and_slots(self):
        dispatch_path = (
            self.root / ".gsp" / "runs" / "work" / "sessions" / "dispatch.json"
        )
        dispatch_before_dependency = (
            dispatch_path.read_bytes() if dispatch_path.exists() else None
        )
        resources_before_dependency = ResourceRegistry(self.root).list_for_work(
            self.state["work_uuid"]
        )
        with self.assertRaisesRegex(
            WorkflowError, "DISPATCH_DEPENDENCY_BLOCKED T03"
        ):
            self.dispatch_group("T03")
        self.assertEqual(
            dispatch_path.read_bytes() if dispatch_path.exists() else None,
            dispatch_before_dependency,
        )
        self.assertEqual(
            ResourceRegistry(self.root).list_for_work(self.state["work_uuid"]),
            resources_before_dependency,
        )

        first_dispatch = self.dispatch_group("T01")
        first_row = first_dispatch["dispatches"][0]
        self.assertEqual(first_dispatch["workspace_group"]["state"], "pending_host")
        self.assertEqual(first_row["platform"], "codex")
        self.assertFalse(first_row["managed_worktree"])
        self.assertEqual(Path(first_row["worktree"]).resolve(), self.parent.resolve())

        first_binding, first_registration = self.register_worker(
            "S02", "T01", "worker-one",
        )
        first_group = first_registration["session"]["workspace_group"]
        registry = load_registry(self.project, self.state)
        self.assertEqual(
            registry["workspace_leases"][str(self.parent.resolve()).casefold()],
            "S02",
        )
        control_document = self.project.document(self.state, "plan")
        authorized = authorize_edit(
            self.project,
            self.state,
            "S01",
            [str(control_document)],
            context_path=str(
                self.root / self.controller_binding.handle_path
            ),
        )
        self.assertEqual(
            authorized["authority"], "controller-control-document",
        )
        with self.assertRaisesRegex(
            WorkflowError, "SESSION_WRITE_LEASE_REQUIRED S01"
        ):
            authorize_edit(
                self.project, self.state, "S01",
                [str(self.parent / "src" / "first.py")],
                context_path=str(
                    self.root / self.controller_binding.handle_path
                ),
            )
        with self.assertRaisesRegex(
            WorkflowError, "SESSION_WRITE_LEASE_REQUIRED S01"
        ):
            authorize_edit(
                self.project, self.state, "S01",
                [
                    str(control_document),
                    str(self.parent / "src" / "first.py"),
                ],
                context_path=str(
                    self.root / self.controller_binding.handle_path
                ),
            )

        dispatch_before = (
            self.root / ".gsp" / "runs" / "work" / "sessions" / "dispatch.json"
        ).read_bytes()
        resources_before = ResourceRegistry(self.root).list_for_work(
            self.state["work_uuid"]
        )
        with self.assertRaisesRegex(
            WorkflowError, "WORKSPACE_GROUP_PREVIOUS_TASK_UNACCEPTED"
        ):
            self.dispatch_group("T02")
        self.assertEqual(
            (
                self.root / ".gsp" / "runs" / "work" / "sessions"
                / "dispatch.json"
            ).read_bytes(),
            dispatch_before,
        )
        self.assertEqual(
            ResourceRegistry(self.root).list_for_work(self.state["work_uuid"]),
            resources_before,
        )

        registry = load_registry(self.project, self.state)
        registry["tasks"]["T01"]["acceptance_status"] = "accepted"
        save_sessions(self.project, self.state, registry)
        release_session_lease(self.project, self.state, "S02", "accepted")
        self.state["inflight"] = {"task": "T01", "evidence": "unknown"}
        try:
            with self.assertRaisesRegex(
                WorkflowError, "WORKSPACE_GROUP_EXECUTION_UNKNOWN"
            ):
                self.dispatch_group("T02")
        finally:
            self.state.pop("inflight", None)
        self.assertEqual(
            (
                self.root / ".gsp" / "runs" / "work" / "sessions"
                / "dispatch.json"
            ).read_bytes(),
            dispatch_before,
        )

        second_dispatch = self.dispatch_group("T02")
        second_row = second_dispatch["dispatches"][0]
        self.assertEqual(
            second_dispatch["workspace_group"]["group_uid"],
            first_dispatch["workspace_group"]["group_uid"],
        )
        self.assertEqual(
            second_dispatch["workspace_group"]["resource_request_uid"],
            first_dispatch["workspace_group"]["resource_request_uid"],
        )
        self.assertNotEqual(
            first_row["execution_slot_request_uid"],
            second_row["execution_slot_request_uid"],
        )
        second_binding, second_registration = self.register_worker(
            "S03", "T02", "worker-two",
        )
        self.assertNotEqual(
            first_binding.binding_uid, second_binding.binding_uid,
        )
        self.assertNotEqual(
            first_registration["session"]["context"],
            second_registration["session"]["context"],
        )
        self.assertEqual(
            second_registration["session"]["workspace_group"]["group_uid"],
            first_group["group_uid"],
        )
        registry = load_registry(self.project, self.state)
        self.assertEqual(
            registry["workspace_leases"][str(self.parent.resolve()).casefold()],
            "S03",
        )


if __name__ == "__main__":
    unittest.main()
