import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from gsp.identity_store import IdentityStore, IdentityStoreError
from gsp.migration_apply import stage_migration
from gsp.orchestration_migration import preview_migration
from gsp.runtime import WorkflowError, locked
from tests.test_orchestration_migration_preview import (
    PROJECT_UID,
    MigrationFixture,
    source_snapshot,
    write_json,
)


class MigrationStageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gsp-migration-apply-')
        self.addCleanup(self.temp.cleanup)
        self.fixture = MigrationFixture(Path(self.temp.name))
        self.fixture.add_pair(1)
        self.fixture.add_todo('GSP-000002')
        self.fixture.create_index()
        self.fixture.initialize_git()
        self.root = self.fixture.root
        write_json(self.root / '.gsp/project.json', {
            'id': PROJECT_UID, 'root': str(self.root),
        })
        self.manifest = preview_migration(self.root)
        self.assertEqual(self.manifest.blockers, ())

    def test_stage_is_idempotent_backed_up_and_stays_inactive(self):
        source_before = source_snapshot(self.root)
        first = stage_migration(self.root, self.manifest)
        second = stage_migration(self.root, self.manifest)

        self.assertEqual(first['request_uid'], second['request_uid'])
        self.assertEqual(
            first['candidate_logical_digest'],
            second['candidate_logical_digest'],
        )
        self.assertEqual(source_snapshot(self.root), source_before)
        stage = self.root / first['stage_path']
        cursor = json.loads((stage / 'cursor.json').read_text(encoding='utf-8'))
        self.assertTrue(any(
            row['source_path'] == '.gsp/documents.sqlite'
            and row['mode'] == 'sqlite-backup'
            for row in cursor['files']
        ))
        backup = stage / 'source-backup/.gsp/documents.sqlite'
        with closing(sqlite3.connect(backup)) as db:
            self.assertIsNotNone(db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='entries'"
            ).fetchone())
        candidate = self.root / first['candidate_path']
        with closing(sqlite3.connect(candidate)) as db:
            self.assertIsNone(db.execute(
                "SELECT 1 FROM metadata WHERE key='schema_version'"
            ).fetchone())
            self.assertGreater(
                db.execute('SELECT COUNT(*) FROM objects').fetchone()[0], 0
            )
        self.assertTrue((self.root / '.gsp/migrations/pending.json').is_file())
        with self.assertRaisesRegex(
            IdentityStoreError, 'IDENTITY_MIGRATION_PENDING'
        ):
            IdentityStore(self.root).initialize()

    def test_source_drift_creates_no_stage_intent(self):
        (self.root / 'TODOS.md').write_text('# drift\n', encoding='utf-8')
        with self.assertRaisesRegex(WorkflowError, 'MIGRATION_SOURCE_CHANGED'):
            stage_migration(self.root, self.manifest)
        self.assertFalse((self.root / '.gsp/migrations').exists())

    def test_live_writer_is_fail_closed_and_preserved(self):
        with locked(self.root):
            with self.assertRaisesRegex(
                WorkflowError, 'MIGRATION_SOURCE_CHANGED'
            ):
                stage_migration(self.root, self.manifest)
            self.assertFalse((self.root / '.gsp/migrations').exists())

    def test_conflicting_request_keeps_pending_intent(self):
        first = stage_migration(self.root, self.manifest)
        pending_path = self.root / '.gsp/migrations/pending.json'
        before = pending_path.read_bytes()
        with self.assertRaisesRegex(
            WorkflowError, 'MIGRATION_REQUEST_CONFLICT'
        ):
            stage_migration(
                self.root, self.manifest, request_uid='different-request',
            )
        self.assertEqual(pending_path.read_bytes(), before)
        self.assertEqual(
            json.loads(before.decode('utf-8'))['request_uid'],
            first['request_uid'],
        )

    def test_linked_worktree_requires_control_root_without_local_stage(self):
        linked = self.fixture.add_worktree('linked', 'linked')
        with self.assertRaisesRegex(
            WorkflowError, 'MIGRATION_CONTROL_ROOT_REQUIRED'
        ):
            stage_migration(linked, self.manifest)
        self.assertFalse((linked / '.gsp/migrations').exists())


if __name__ == '__main__':
    unittest.main()
