from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from gsp.identity_store import IdentityStore, IdentityStoreError
from gsp.migration_apply import (
    _sqlite_snapshot,
    _sqlite_logical_digest,
    apply_migration,
    restore_migration,
    resume_migration,
    stage_migration,
)
from gsp.orchestration_migration import preview_migration
from gsp.orchestration_types import RequestRef
from gsp.runtime import WorkflowError
from tests.test_orchestration_migration_preview import (
    PROJECT_UID,
    MigrationFixture,
    write_json,
)


def allocation_request(uid):
    return RequestRef(
        request_uid=uid,
        logical_key=uid,
        input_digest='0' * 64,
        project_uid=PROJECT_UID,
        work_uid=None,
        attempt_uid=None,
        operation_kind='migration-test',
    )


class MigrationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gsp-migration-recovery-')
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

    def test_apply_activates_last_and_preserves_highwater(self):
        staged = stage_migration(self.root, self.manifest)
        receipt = apply_migration(self.root, self.manifest)
        self.assertEqual(receipt['request_uid'], staged['request_uid'])
        self.assertFalse((self.root / '.gsp/migrations/pending.json').exists())
        self.assertEqual(
            IdentityStore(self.root).read_schema()['schema'],
            1,
        )
        allocated = IdentityStore(self.root).allocate(
            'work', None, allocation_request('post-migration-work'),
        )
        self.assertGreaterEqual(int(allocated.canonical_id.split('-')[1]), 3)
        self.assertEqual(
            apply_migration(self.root, self.manifest)['request_uid'],
            receipt['request_uid'],
        )
        with self.assertRaisesRegex(
            WorkflowError, 'MIGRATION_RESTORE_AFTER_WRITES'
        ):
            restore_migration(self.root)

    def test_resume_finishes_publish_before_receipt_crash_window(self):
        staged = stage_migration(self.root, self.manifest)
        pending_path = self.root / '.gsp/migrations/pending.json'
        pending_before = pending_path.read_bytes()
        receipt = apply_migration(self.root, self.manifest)
        activation = self.root / staged['stage_path'] / 'activation.json'
        activation.unlink()
        pending_path.write_bytes(pending_before)

        resumed = resume_migration(
            self.root, request_uid=receipt['request_uid'],
        )

        self.assertEqual(resumed['activated_logical_digest'],
                         receipt['activated_logical_digest'])
        self.assertTrue(activation.is_file())
        self.assertFalse(pending_path.exists())

    def test_restore_rejects_operation_only_journal_write(self):
        apply_migration(self.root, self.manifest)
        authority = self.root / '.gsp/orchestration/registry.sqlite'
        def logical_digest():
            with _sqlite_snapshot(authority) as snapshot:
                return _sqlite_logical_digest(snapshot)
        before_logical = logical_digest()
        self.assertFalse(Path(str(authority) + '-wal').exists())
        self.assertFalse(Path(str(authority) + '-shm').exists())
        store = IdentityStore(self.root)
        operation = store.begin_operation(
            RequestRef(
                request_uid='operation-only-write',
                logical_key='operation-only-write',
                input_digest='1' * 64,
                project_uid=PROJECT_UID,
                work_uid=None,
                attempt_uid=None,
                operation_kind='restore-fence-probe',
            ),
            '2' * 64,
        )
        self.assertEqual(logical_digest(), before_logical)
        with self.assertRaisesRegex(
            WorkflowError, 'MIGRATION_RESTORE_AFTER_WRITES'
        ):
            restore_migration(self.root)
        self.assertEqual(
            store.get_operation(operation.operation_uid).operation_uid,
            operation.operation_uid,
        )

    def test_restore_before_writes_is_readonly_and_allocator_blocked(self):
        apply_migration(self.root, self.manifest)
        restored = restore_migration(self.root)

        self.assertEqual(restored['state'], 'restored-readonly')
        self.assertFalse(restored['allocator_write'])
        self.assertTrue(
            (self.root / restored['recovery_point']).is_file()
        )
        self.assertTrue((self.root / '.gsp/migrations/pending.json').is_file())
        self.assertEqual(
            restore_migration(self.root)['recovery_point'],
            restored['recovery_point'],
        )
        with self.assertRaisesRegex(
            IdentityStoreError, 'IDENTITY_MIGRATION_PENDING'
        ):
            IdentityStore(self.root).initialize()

    def test_restore_reuses_sqlite_backup_of_prior_empty_authority(self):
        fixture = MigrationFixture(Path(self.temp.name) / 'with-authority')
        fixture.add_pair(9)
        fixture.add_todo('GSP-000010')
        fixture.create_index()
        fixture.initialize_git()
        fixture.initialize_authority()
        manifest = preview_migration(fixture.root)
        self.assertEqual(manifest.blockers, ())
        apply_migration(fixture.root, manifest)
        self.assertFalse(
            (fixture.root / '.gsp/orchestration/registry.sqlite-wal').exists()
        )
        self.assertFalse(
            (fixture.root / '.gsp/orchestration/registry.sqlite-shm').exists()
        )
        restored = restore_migration(fixture.root)

        self.assertEqual(restored['state'], 'restored-readonly')
        authority = fixture.root / '.gsp/orchestration/registry.sqlite'
        self.assertTrue(authority.is_file())
        with closing(sqlite3.connect(authority)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0],
                '1',
            )
            self.assertEqual(
                db.execute('SELECT COUNT(*) FROM objects').fetchone()[0],
                0,
            )


if __name__ == '__main__':
    unittest.main()
