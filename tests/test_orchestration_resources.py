"""Project-wide resource leasing uses the orchestration identity authority."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from gsp.identity_store import IdentityStore
from gsp.resource_registry import (
    ResourceConflictError,
    ResourceOwner,
    ResourceRegistry,
    ResourceRegistryError,
)


class ResourceRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-resources-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".gsp").mkdir()
        (self.root / ".gsp" / "project.json").write_text(
            json.dumps({"id": "resource-project", "root": str(self.root)}),
            encoding="utf-8",
        )
        self.identity = IdentityStore(self.root)
        self.identity.initialize()
        self.registry = ResourceRegistry(self.root)

    @staticmethod
    def owner(work="work-1", session="session-1", generation=1):
        return ResourceOwner(work, session, generation)

    def test_reserve_is_idempotent_and_batch_conflict_has_no_partial_claim(self):
        first = self.registry.reserve(
            "request-a",
            self.owner(),
            [
                ("path", "src"),
                ("branch", "feature/shared"),
                ("build_root", "target/a"),
            ],
        )
        replay = self.registry.reserve(
            "request-a",
            self.owner(),
            [
                ("build_root", "target/a"),
                ("branch", "feature/shared"),
                ("path", "src"),
            ],
        )
        self.assertEqual(first, replay)
        self.assertEqual(first["state"], "reserved")

        with self.assertRaisesRegex(
            ResourceConflictError, r"RESOURCE_CONFLICT branch feature/shared"
        ):
            self.registry.reserve(
                "request-b",
                self.owner(session="session-2", generation=2),
                [
                    ("path", "tests"),
                    ("branch", "feature/shared"),
                    ("build_root", "target/b"),
                ],
            )
        self.assertIsNone(self.registry.get("request-b"))
        self.assertEqual(self.registry.conflicts([("path", "tests")]), [])
        self.assertEqual(self.registry.conflicts([("build_root", "target/b")]), [])

    def test_commit_release_and_owner_generation_fencing(self):
        self.registry.reserve(
            "request-old", self.owner(), [("path", "src/shared")]
        )
        committed = self.registry.commit("request-old", self.owner())
        self.assertEqual(committed["state"], "committed")
        self.assertEqual(
            self.registry.commit("request-old", self.owner()), committed
        )

        with self.assertRaisesRegex(
            ResourceRegistryError, "RESOURCE_OWNER_MISMATCH"
        ):
            self.registry.release(
                "request-old",
                self.owner(session="stale-session"),
            )
        with self.assertRaisesRegex(
            ResourceRegistryError, "RESOURCE_OWNER_MISMATCH"
        ):
            self.registry.release(
                "request-old",
                self.owner(generation=2),
            )

        released = self.registry.release("request-old", self.owner())
        self.assertEqual(released["state"], "released")
        self.assertEqual(
            self.registry.release("request-old", self.owner()), released
        )

        replacement = self.registry.reserve(
            "request-new",
            self.owner(session="session-2", generation=3),
            [("path", "src/shared")],
        )
        self.assertEqual(replacement["state"], "reserved")
        self.registry.release("request-old", self.owner())
        self.assertEqual(
            self.registry.get("request-new")["state"], "reserved"
        )

    def test_changed_request_input_is_rejected(self):
        self.registry.reserve("request-a", self.owner(), [("path", "src")])
        with self.assertRaisesRegex(
            ResourceRegistryError, "RESOURCE_REQUEST_INPUT_CHANGED"
        ):
            self.registry.reserve(
                "request-a", self.owner(), [("path", "tests")]
            )
        with self.assertRaisesRegex(
            ResourceRegistryError, "RESOURCE_REQUEST_INPUT_CHANGED"
        ):
            self.registry.reserve(
                "request-a",
                self.owner(generation=2),
                [("path", "src")],
            )

    def test_parent_child_and_build_root_paths_share_exclusion(self):
        self.registry.reserve('parent', self.owner(), [('path', 'src')])
        for kind, path in [('path', 'src/module.py'), ('build_root', 'src/cache')]:
            with self.subTest(kind=kind):
                self.assertEqual(self.registry.conflicts([(kind, path)])[0]['request_uid'], 'parent')
                with self.assertRaisesRegex(ResourceConflictError, 'RESOURCE_CONFLICT'):
                    self.registry.reserve('child-' + kind, self.owner(work='other'), [(kind, path)])
                self.assertIsNone(self.registry.get('child-' + kind))
        self.assertEqual(self.registry.conflicts([('path', 'src-other')]), [])

    def test_path_and_build_root_are_project_normalized(self):
        self.registry.reserve(
            "request-a", self.owner(), [("path", "src/../tests")]
        )
        conflict = self.registry.conflicts(
            [("path", str(self.root / "tests"))]
        )
        self.assertEqual(conflict[0]["request_uid"], "request-a")
        self.assertEqual(
            Path(conflict[0]["identity"]), (self.root / "tests").resolve()
        )

    def test_duplicate_resources_in_one_request_are_rejected(self):
        with self.assertRaisesRegex(
            ResourceRegistryError, "RESOURCE_DUPLICATE"
        ):
            self.registry.reserve(
                "request-a",
                self.owner(),
                [("path", "src"), ("path", str(self.root / "src"))],
            )
        self.assertIsNone(self.registry.get("request-a"))

    def test_concurrent_reservations_use_one_atomic_winner(self):
        barrier = threading.Barrier(2)

        def attempt(session):
            barrier.wait(timeout=10)
            try:
                return self.registry.reserve(
                    "request-" + session,
                    self.owner(session=session, generation=1),
                    [("path", "src/shared")],
                )
            except ResourceConflictError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(attempt, ("one", "two")))
        winners = [row for row in rows if isinstance(row, dict)]
        conflicts = [row for row in rows if isinstance(row, ResourceConflictError)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(conflicts), 1)

    def test_execution_slots_release_independently_from_workspace_claims(self):
        slots = [
            (
                "slot-request-" + str(index),
                self.owner(session="worker-" + str(index)),
            )
            for index in range(1, 4)
        ]
        reserved = self.registry.reserve_execution_batch(
            "batch-a",
            self.owner(session="controller"),
            [("path", "src/shared"), ("build_root", "target/shared")],
            slots,
        )
        self.registry.commit(
            "batch-a", self.owner(session="controller")
        )
        self.assertEqual(
            {
                row["resources"][0]["identity"]
                for row in reserved["slots"].values()
            },
            {
                "project-execution-slot:1",
                "project-execution-slot:2",
                "project-execution-slot:3",
            },
        )

        active = self.registry.list_for_work("work-1")
        self.assertEqual(
            {row["kind"] for row in active},
            {"path", "build_root", "slot"},
        )
        self.assertEqual(
            len([row for row in active if row["kind"] == "slot"]),
            3,
        )

        released = self.registry.release_execution_slot(
            "slot-request-2",
            self.owner(session="worker-2"),
            "project-execution-slot:2",
        )
        self.assertEqual(released["state"], "released")
        self.assertEqual(released["resources"][0]["identity"],
                         "project-execution-slot:2")
        active = self.registry.list_for_work("work-1")
        self.assertEqual(
            {
                row["identity"] for row in active if row["kind"] == "slot"
            },
            {
                "project-execution-slot:1",
                "project-execution-slot:3",
            },
        )
        self.assertEqual(
            {
                row["kind"] for row in active
                if row["kind"] != "slot"
            },
            {"path", "build_root"},
        )
        self.assertEqual(
            self.registry.get("batch-a")["state"], "committed"
        )

        reacquired = self.registry.reserve_execution_batch(
            "batch-review",
            self.owner(work="review-work", session="reviewer"),
            [("path", "review/output")],
            [("slot-request-review", self.owner(
                work="review-work", session="reviewer",
            ))],
        )
        self.assertEqual(
            reacquired["slots"]["slot-request-review"]["resources"][0][
                "identity"
            ],
            "project-execution-slot:2",
        )

    def test_slot_exhaustion_rolls_back_the_workspace_batch(self):
        self.registry.reserve_execution_batch(
            "batch-full",
            self.owner(),
            [("path", "src/full")],
            [
                ("slot-a", self.owner(session="worker-a")),
                ("slot-b", self.owner(session="worker-b")),
                ("slot-c", self.owner(session="worker-c")),
            ],
        )
        with self.assertRaisesRegex(
            ResourceConflictError, "RESOURCE_SLOTS_EXHAUSTED"
        ):
            self.registry.reserve_execution_batch(
                "batch-failed",
                self.owner(work="other", session="worker-d"),
                [("path", "tests/rolled-back")],
                [("slot-d", self.owner(
                    work="other", session="worker-d",
                ))],
            )

        self.assertIsNone(self.registry.get("batch-failed"))
        self.assertEqual(
            self.registry.conflicts([("path", "tests/rolled-back")]),
            [],
        )
        self.assertEqual(
            len([
                row for row in self.registry.list_for_work("work-1")
                if row["kind"] == "slot"
            ]),
            3,
        )

    def test_slot_identity_only_conflicts_with_the_same_slot(self):
        self.registry.reserve_execution_batch(
            "batch-a",
            self.owner(),
            [("path", "project-execution-slot:1")],
            [("slot-a", self.owner(session="worker-a"))],
        )
        self.assertTrue(any(
            row["kind"] == "path"
            for row in self.registry.conflicts([
                ("path", "project-execution-slot:1")
            ])
        ))
        self.assertFalse(any(
            row["kind"] == "slot"
            for row in self.registry.conflicts([
                ("path", "project-execution-slot:1")
            ])
        ))
        slot_conflicts = self.registry.conflicts([
            ("slot", "project-execution-slot:1")
        ])
        self.assertEqual(slot_conflicts[0]["kind"], "slot")
        self.assertEqual(
            self.registry.conflicts([
                ("slot", "project-execution-slot:2")
            ]),
            [],
        )

    def test_slot_release_is_fenced_by_owner_generation_and_identity(self):
        self.registry.reserve_execution_batch(
            "batch-a",
            self.owner(),
            [("path", "src")],
            [("slot-a", self.owner(session="worker-a"))],
        )
        with self.assertRaisesRegex(
            ResourceRegistryError, "RESOURCE_OWNER_MISMATCH"
        ):
            self.registry.release_execution_slot(
                "slot-a",
                self.owner(session="worker-a", generation=2),
            )
        with self.assertRaisesRegex(
            ResourceRegistryError, "RESOURCE_SLOT_IDENTITY_MISMATCH"
        ):
            self.registry.release_execution_slot(
                "slot-a",
                self.owner(session="worker-a"),
                "project-execution-slot:2",
            )
        self.assertEqual(
            self.registry.get_slot("slot-a")["state"], "reserved"
        )

    def test_released_resource_request_cannot_acquire_a_new_slot(self):
        owner = self.owner()
        self.registry.reserve_execution_batch(
            "batch-a",
            owner,
            [("path", "src")],
            [("slot-a", self.owner(session="worker-a"))],
        )
        released = self.registry.release("batch-a", owner)
        self.assertEqual(released["state"], "released")
        self.assertEqual(
            self.registry.get_slot("slot-a")["state"], "reserved"
        )

        with self.assertRaisesRegex(
            ResourceRegistryError, "RESOURCE_REQUEST_RELEASED"
        ):
            self.registry.reserve_execution_batch(
                "batch-a",
                owner,
                [("path", "src")],
                [("slot-b", self.owner(session="worker-b"))],
            )

        self.assertIsNone(self.registry.get_slot("slot-b"))
        self.assertEqual(
            self.registry.get_slot("slot-a")["state"], "reserved"
        )
        self.assertEqual(self.registry.get("batch-a")["state"], "released")
        self.assertEqual(
            self.registry.reserve("batch-a", owner, [("path", "src")]),
            released,
        )

    def test_execution_batch_replay_requires_the_complete_active_slot_set(self):
        owner = self.owner()
        slot_a = ("slot-a", self.owner(session="worker-a"))
        first = self.registry.reserve_execution_batch(
            "batch-a",
            owner,
            [("path", "src")],
            [slot_a],
        )
        replay = self.registry.reserve_execution_batch(
            "batch-a",
            owner,
            [("path", "src")],
            [slot_a],
        )
        self.assertEqual(replay, first)

        with self.assertRaisesRegex(
            ResourceRegistryError, "RESOURCE_SLOT_SET_INCOMPLETE"
        ):
            self.registry.reserve_execution_batch(
                "batch-a",
                owner,
                [("path", "src")],
                [
                    slot_a,
                    ("slot-b", self.owner(session="worker-b")),
                ],
            )
        self.assertIsNone(self.registry.get_slot("slot-b"))
        self.assertEqual(
            self.registry.get_slot("slot-a")["state"], "reserved"
        )

    def test_execution_batch_replay_rejects_a_missing_active_slot_claim(self):
        owner = self.owner()
        slot_a = ("slot-a", self.owner(session="worker-a"))
        self.registry.reserve_execution_batch(
            "batch-a",
            owner,
            [("path", "src")],
            [slot_a],
        )
        with closing(sqlite3.connect(self.registry.path)) as db:
            db.execute(
                "DELETE FROM execution_slot_claims WHERE request_uid=?",
                ("slot-a",),
            )
            db.commit()

        with self.assertRaisesRegex(
            ResourceRegistryError, "RESOURCE_SLOT_CLAIMS_INCOMPLETE"
        ):
            self.registry.reserve_execution_batch(
                "batch-a",
                owner,
                [("path", "src")],
                [slot_a],
            )
        self.assertEqual(
            self.registry.get_slot("slot-a")["state"], "reserved"
        )
        self.assertFalse(any(
            row["kind"] == "slot"
            for row in self.registry.list_for_work("work-1")
        ))

    def test_read_queries_do_not_create_schema_or_directories(self):
        database = self.identity.path
        with closing(sqlite3.connect(database)) as db:
            before_schema = db.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall()
        before_dirs = {
            path.relative_to(self.root)
            for path in self.root.rglob("*")
            if path.is_dir()
        }
        self.assertIsNone(self.registry.get("missing"))
        self.assertEqual(self.registry.conflicts([("path", "src")]), [])
        with closing(sqlite3.connect(database)) as db:
            after_schema = db.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall()
        after_dirs = {
            path.relative_to(self.root)
            for path in self.root.rglob("*")
            if path.is_dir()
        }
        self.assertEqual(before_schema, after_schema)
        self.assertEqual(before_dirs, after_dirs)


if __name__ == "__main__":
    unittest.main()
