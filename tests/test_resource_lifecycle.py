"""Work-scoped build resources are registered, previewed and resumed."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from gsp.identity_store import IdentityStore
from gsp.resource_lifecycle import (
    cleanup_work_resources,
    register_build_resource,
)
from gsp.resource_registry import ResourceOwner, ResourceRegistry


class ResourceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-resource-lifecycle-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        (self.root / ".gsp").mkdir()
        (self.root / ".gsp" / "project.json").write_text(
            json.dumps({"id": "lifecycle-project", "root": str(self.root)}),
            encoding="utf-8",
        )
        IdentityStore(self.root).initialize()
        self.state = {
            "id": "run-one",
            "number": "GSP-000034",
            "work_uuid": "work-one",
            "controller_binding_uid": "binding-one",
            "controller_generation": 1,
            "git_topology": {
                "workspace_root": str(self.root / "missing-workspace")
            },
            "tasks": [{"id": "T01", "title": "one"}],
        }
        self.owner = ResourceOwner("work-one", "binding-one", 1)

    def context(self, mode="isolated", root=None, provenance="isolated"):
        root = root or self.root / ".gsp/build/GSP-000034/run-one"
        return {
            "schema": 1,
            "mode": mode,
            "root": str(Path(root).resolve()),
            "provenance": provenance,
            "owner": {
                "run": "run-one",
                "task": "T01",
                "workspace": str((self.root / "missing-workspace").resolve()),
            },
        }

    def test_register_lists_work_scoped_delete_build_root(self):
        context = self.context()
        snapshot = register_build_resource(
            type("Project", (), {"root": self.root})(),
            self.state,
            "T01",
            context,
        )
        self.assertEqual(snapshot["state"], "reserved")
        rows = ResourceRegistry(self.root).list_for_work("work-one")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "build_root")
        self.assertEqual(rows[0]["disposition"], "delete")
        self.assertEqual(Path(rows[0]["identity"]), Path(context["root"]))

    def test_closed_missing_worktree_recovers_only_its_receipted_main_build_root(self):
        self.state['status'] = 'closed'
        build = Path(self.context()['root'])
        build.mkdir(parents=True)
        receipt = self.root / '.gsp/runs/run-one/evidence/old/receipt.json'
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({'id': 'old', 'task': 'all',
            'build_context': self.context(), 'workspace_root': self.state['git_topology']['workspace_root']}), encoding='utf-8')
        self.state['evidence'] = [receipt.relative_to(self.root).as_posix()]
        project = type('Project', (), {'root': self.root})()
        database = IdentityStore(self.root).path
        before = database.read_bytes()
        preview = cleanup_work_resources(project, self.state)
        self.assertFalse(preview['complete'])
        self.assertEqual(preview['items'][0]['state'], 'would-register-and-delete')
        self.assertEqual(database.read_bytes(), before)
        self.assertTrue(build.exists())
        applied = cleanup_work_resources(project, self.state, dry_run=False)
        self.assertTrue(applied['complete'])
        self.assertFalse(build.exists())
        self.assertEqual(cleanup_work_resources(project, self.state, dry_run=False)['items'], [])

    def test_managed_claim_releases_only_after_persisted_removal_and_actual_absence(self):
        path = self.root / '.gsp/worktrees/child'
        path.mkdir(parents=True)
        registry = ResourceRegistry(self.root)
        registry.reserve('managed', self.owner, [{'kind': 'path', 'identity': str(path), 'disposition': 'delete'}])
        project = type('Project', (), {'root': self.root})()
        proof = self.root / '.gsp/runs/run-one/sessions/dispatch.json'
        proof.parent.mkdir(parents=True)
        proof.write_text(json.dumps({'dispatches': [{'worktree': str(path),
            'resource_request_uid': 'managed', 'worktree_state': 'removed',
            'worktree_disposition': {'action': 'remove', 'state': 'completed',
                'path': str(path), 'verified_absent': True}}]}), encoding='utf-8')
        self.assertFalse(cleanup_work_resources(project, self.state, dry_run=False)['complete'])
        path.rmdir()
        self.assertTrue(cleanup_work_resources(project, self.state, dry_run=False)['complete'])
        self.assertEqual(registry.list_for_work('work-one'), [])

    def test_present_build_lease_blocks_physical_cleanup(self):
        context = self.context()
        project = type('Project', (), {'root': self.root})()
        register_build_resource(project, self.state, 'T01', context)
        root = Path(context['root'])
        root.mkdir(parents=True)
        (root / '.gsp-build-lease.json').write_text('{"phase":"running"}', encoding='utf-8')
        result = cleanup_work_resources(project, self.state, dry_run=False)
        self.assertTrue(root.exists())
        self.assertEqual(result['items'][0]['state'], 'pending')

    def test_present_build_lease_blocks_delete_and_release_claims(self):
        project = type('Project', (), {'root': self.root})()
        contexts = [
            self.context(),
            self.context(
                'reuse-exclusive',
                Path(self.tmp.name) / 'shared',
                'policy',
            ),
        ]
        for context in contexts:
            register_build_resource(project, self.state, 'T01', context)
            root = Path(context['root'])
            root.mkdir(parents=True, exist_ok=True)
            (root / '.gsp-build-lease.json').write_text(
                '{"phase":"running"}',
                encoding='utf-8',
            )

        result = cleanup_work_resources(project, self.state, dry_run=False)

        self.assertFalse(result['complete'])
        self.assertEqual(
            [item['state'] for item in result['items']],
            ['pending', 'pending'],
        )
        self.assertEqual(
            [item['reason'] for item in result['items']],
            ['BUILD_LEASE_ACTIVE_OR_UNKNOWN'] * 2,
        )
        self.assertEqual(
            len(ResourceRegistry(self.root).list_for_work('work-one')),
            2,
        )
        self.assertTrue(all(Path(context['root']).exists() for context in contexts))

    def test_unknown_execution_retains_resource_even_without_a_live_lease_file(self):
        context = self.context()
        project = type('Project', (), {'root': self.root})()
        register_build_resource(project, self.state, 'T01', context)
        root = Path(context['root'])
        root.mkdir(parents=True)
        self.state['inflight'] = {'id': 'unknown', 'phase': 'command'}
        result = cleanup_work_resources(project, self.state, dry_run=False)
        self.assertTrue(root.is_dir())
        self.assertFalse(result['complete'])
        self.assertEqual(result['items'][0]['reason'], 'EXECUTION_UNKNOWN')

    def test_reuse_exclusive_registration_is_release_only(self):
        shared = Path(self.tmp.name) / "shared"
        result = register_build_resource(
            type("Project", (), {"root": self.root})(),
            self.state,
            "T01",
            self.context("reuse-exclusive", shared, "policy"),
        )
        self.assertEqual(result["resources"][0]["disposition"], "release")
        row = ResourceRegistry(self.root).list_for_work("work-one")[0]
        self.assertEqual(row["disposition"], "release")

    def test_cleanup_preview_then_delete_is_resumable(self):
        path = Path(self.context()["root"])
        path.mkdir(parents=True)
        (path / "artifact.bin").write_bytes(b"x" * 32)
        project = type("Project", (), {"root": self.root})()
        register_build_resource(project, self.state, "T01", self.context())

        preview = cleanup_work_resources(project, self.state)
        self.assertFalse(preview["complete"])
        self.assertEqual(preview["items"][0]["state"], "would-delete")
        self.assertTrue(path.exists())

        applied = cleanup_work_resources(project, self.state, dry_run=False)
        self.assertTrue(applied["complete"])
        self.assertFalse(path.exists())
        repeated = cleanup_work_resources(project, self.state, dry_run=False)
        self.assertTrue(repeated["complete"])
        self.assertEqual(repeated["items"], [])

    def test_release_policy_never_removes_the_shared_root(self):
        path = Path(self.tmp.name) / "shared"
        path.mkdir()
        (path / "keep.bin").write_bytes(b"x")
        project = type("Project", (), {"root": self.root})()
        register_build_resource(
            project,
            self.state,
            "T01",
            self.context("reuse-exclusive", path, "policy"),
        )
        result = cleanup_work_resources(project, self.state, dry_run=False)
        self.assertTrue(result["complete"])
        self.assertTrue(path.exists())
        self.assertEqual(result["items"][0]["state"], "released")

    def test_uncontrolled_delete_root_stays_pending(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        ResourceRegistry(self.root).reserve(
            "outside-request",
            self.owner,
            [{
                "kind": "build_root",
                "identity": str(outside),
                "disposition": "delete",
            }],
        )
        result = cleanup_work_resources(
            type("Project", (), {"root": self.root})(),
            self.state,
            dry_run=False,
        )
        self.assertFalse(result["complete"])
        self.assertEqual(result["items"][0]["state"], "pending")
        self.assertEqual(
            result["items"][0]["reason"], "UNCONTROLLED_BUILD_PATH"
        )
        self.assertTrue(outside.exists())

    def test_legacy_request_without_disposition_reads_as_release(self):
        registry = ResourceRegistry(self.root)
        registry.reserve(
            "schema-seed", self.owner, [("path", "seed")]
        )
        legacy_path = str((self.root / "legacy").resolve())
        with closing(sqlite3.connect(registry.path)) as db:
            project_uid = db.execute(
                "SELECT value FROM metadata WHERE key='project_uid'"
            ).fetchone()[0]
            db.execute(
                "INSERT INTO resource_requests("
                "request_uid,request_digest,project_uid,owner_work_uid,"
                "owner_session_uid,owner_generation,state,resources_json,"
                "created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy-request",
                    "legacy-digest",
                    project_uid,
                    self.owner.work_uid,
                    self.owner.session_uid,
                    self.owner.generation,
                    "committed",
                    json.dumps([{"kind": "path", "identity": legacy_path}]),
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            db.execute(
                "INSERT INTO resource_claims("
                "kind,identity,request_uid,project_uid,owner_work_uid,"
                "owner_session_uid,owner_generation,state,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "path",
                    legacy_path,
                    "legacy-request",
                    project_uid,
                    self.owner.work_uid,
                    self.owner.session_uid,
                    self.owner.generation,
                    "committed",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            db.commit()
        rows = registry.list_for_work("work-one")
        legacy = next(row for row in rows if row["request_uid"] == "legacy-request")
        self.assertEqual(legacy["disposition"], "release")


if __name__ == "__main__":
    unittest.main()
