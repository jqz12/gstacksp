from pathlib import Path
import tempfile
import unittest

from gsp.migration_apply import plan_identity_import
from gsp.orchestration_migration import preview_migration
from gsp.runtime import WorkflowError
from tests.test_orchestration_migration_preview import MigrationFixture, source_snapshot


class MigrationIdentityPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gsp-migrate-plan-')
        self.addCleanup(self.temp.cleanup)
        self.fixture = MigrationFixture(Path(self.temp.name))
        self.fixture.add_pair(1)
        self.fixture.add_todo('GSP-000002')
        self.fixture.create_index()
        self.fixture.initialize_git()
        self.fixture.initialize_authority()
        self.root = self.fixture.root
        self.manifest = preview_migration(self.root)
        self.assertEqual(self.manifest.blockers, ())

    def test_legacy_uids_aliases_and_shared_counter_floor_are_preserved(self):
        result = plan_identity_import(self.root, self.manifest)
        work = next(row for row in result['objects'] if row['kind'] == 'work')
        todo = next(row for row in result['objects'] if row['kind'] == 'todo')
        self.assertEqual(work['uid'], '00000000000000000000000000000001')
        self.assertEqual(work['canonical_id'], 'GSP-000001')
        self.assertEqual(todo['canonical_id'], 'TODO-000002')
        work_floor = next(row for row in result['sequences'] if row['kind'] == 'work')
        self.assertGreaterEqual(work_floor['next_value'], 3)
        self.assertTrue(any(row['alias'] == 'GSP-000002' for row in result['aliases']))
        self.assertIn('run-1', result['requires_reverification'])

    def test_same_manifest_produces_same_mapping_without_source_writes(self):
        before = source_snapshot(self.root)
        first = plan_identity_import(self.root, self.manifest)
        second = plan_identity_import(self.root, self.manifest)
        self.assertEqual(first, second)
        self.assertEqual(source_snapshot(self.root), before)
        self.assertFalse((self.root / '.gsp/migrations').exists())

    def test_source_drift_blocks_before_any_staging(self):
        (self.root / 'TODOS.md').write_text('# Changed source\n', encoding='utf-8')
        with self.assertRaisesRegex(WorkflowError, 'MIGRATION_SOURCE_CHANGED'):
            plan_identity_import(self.root, self.manifest)
        self.assertFalse((self.root / '.gsp/migrations').exists())
