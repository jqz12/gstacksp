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

    def test_short_legacy_todo_numbers_widen_to_the_canonical_width(self):
        fixture = MigrationFixture(Path(self.temp.name) / 'short')
        fixture.add_pair(1)
        fixture.add_todo('GSP-0007')
        fixture.create_index()
        fixture.initialize_git()
        fixture.initialize_authority()
        manifest = preview_migration(fixture.root)
        self.assertEqual(manifest.blockers, ())
        result = plan_identity_import(fixture.root, manifest)
        todo = next(row for row in result['objects'] if row['kind'] == 'todo')
        self.assertEqual(todo['canonical_id'], 'TODO-000007')
        self.assertTrue(any(row['alias'] == 'GSP-0007' for row in result['aliases']))
        todo_floor = next(row for row in result['sequences'] if row['kind'] == 'todo')
        self.assertGreaterEqual(todo_floor['next_value'], 8)

    def test_short_task_aliases_are_adopted_canonically(self):
        fixture = MigrationFixture(Path(self.temp.name) / 'task-alias')
        fixture.add_pair(1, tasks=[{'id': 'T1', 'status': 'done'}, {'id': 'T2', 'status': 'pending'}])
        fixture.create_index()
        fixture.initialize_git()
        fixture.initialize_authority()
        manifest = preview_migration(fixture.root)
        self.assertEqual(manifest.blockers, ())
        result = plan_identity_import(fixture.root, manifest)
        tasks = sorted(row['canonical_id'] for row in result['objects'] if row['kind'] == 'task')
        self.assertEqual(tasks, ['T01', 'T02'])
        self.assertEqual(len(result['runs']), 1)
        self.assertFalse(any(row.get('reason') == 'LEGACY_TASK_ID_NOT_CANONICAL'
                             for row in result['read_only']))

    def test_non_canonical_task_id_keeps_the_run_read_only(self):
        fixture = MigrationFixture(Path(self.temp.name) / 'task-legacy')
        fixture.add_pair(1, tasks=[{'id': 'T00', 'status': 'done'}, {'id': 'T01', 'status': 'pending'}])
        fixture.create_index()
        fixture.initialize_git()
        fixture.initialize_authority()
        manifest = preview_migration(fixture.root)
        self.assertEqual(manifest.blockers, ())
        result = plan_identity_import(fixture.root, manifest)
        self.assertNotIn('run-1', result['runs'])
        degraded = [row for row in result['read_only']
                    if row.get('reason') == 'LEGACY_TASK_ID_NOT_CANONICAL']
        self.assertEqual(len(degraded), 1)
        self.assertEqual(degraded[0]['task_ids'], ['T00'])
