import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from gsp.identity_store import IdentityStore
from gsp.migration_apply import adopt_legacy_run
from gsp.runtime import Project, WorkflowError
from tests.test_orchestration_migration_preview import MigrationFixture, write_json


def snapshot(root):
    paths = sorted(
        path for path in root.rglob('*')
        if path.is_file() and path.name not in {
            'writer.lock', 'writer.mutex', 'registry.sqlite-wal',
            'registry.sqlite-shm',
        }
    )
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


class LegacyAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gsp-migration-adoption-')
        self.addCleanup(self.temp.cleanup)
        fixture = MigrationFixture(Path(self.temp.name))
        fixture.add_pair(9)
        fixture.create_index()
        fixture.initialize_git()
        fixture.initialize_authority()
        self.root = fixture.root
        self.legacy = self.root / '.gsp/runs/run-9/state.json'
        state = json.loads(self.legacy.read_text(encoding='utf-8'))
        docs = self.root / 'docs/legacy/run-9'
        docs.mkdir(parents=True)
        design = docs / 'spec.md'
        plan = docs / 'plan.md'
        design.write_text('# Legacy spec\n\nOriginal requirement.\n', encoding='utf-8')
        plan.write_text('# Legacy plan\n\n1. Do work.\n', encoding='utf-8')
        state.update({
            'status': 'ready',
            'design_approval': {'sha256': 'old-design'},
            'plan_seal': {'sha256': 'old-plan', 'tasks_sha256': 'old-tasks'},
            'review': {'status': 'accepted'},
            'documents': {
                'design': design.relative_to(self.root).as_posix(),
                'plan': plan.relative_to(self.root).as_posix(),
            },
            'tasks': [{
                'id': 'T01',
                'title': 'Legacy task',
                'acceptance': ['new run must revalidate'],
                'depends': [],
                'role': 'implementation',
                'status': 'done',
                'evidence': ['old-receipt'],
            }],
            'evidence': ['old-receipt'],
        })
        write_json(self.legacy, state)
        self.design = design
        self.plan = plan

    def test_adoption_preserves_source_and_resets_acceptance(self):
        source = {
            path: path.read_bytes()
            for path in (self.legacy, self.design, self.plan)
        }
        result = adopt_legacy_run(
            self.root, 'run-9', 'adopted-9', 'request-adopt-9',
        )

        self.assertEqual(result['state'], 'adopted-design')
        self.assertFalse(result['accepted'])
        state = Project(self.root).ledger_state('adopted-9')
        self.assertEqual(state['status'], 'design')
        self.assertIsNone(state['design_approval'])
        self.assertIsNone(state['plan_seal'])
        self.assertIsNone(state['review'])
        self.assertEqual(state['tasks'][0]['status'], 'pending')
        self.assertEqual(state['tasks'][0]['acceptance'], ['new run must revalidate'])
        self.assertEqual(
            (self.root / state['documents']['design']).read_bytes(),
            source[self.design],
        )
        self.assertEqual(
            (self.root / state['documents']['plan']).read_bytes(),
            source[self.plan],
        )
        for path, expected in source.items():
            self.assertEqual(path.read_bytes(), expected)
        project = Project(self.root)
        with self.assertRaises(WorkflowError):
            project.valid_design(
                state, project.document(state, 'design'),
            )

    def test_new_run_conflict_changes_nothing(self):
        conflict = self.root / '.gsp/runs/taken'
        conflict.mkdir()
        write_json(conflict / 'state.json', {'id': 'taken'})
        before = snapshot(self.root)
        next_value = IdentityStore(self.root).next_value('work', None)
        with self.assertRaisesRegex(WorkflowError, 'LEGACY_ADOPTION_RUN_CONFLICT'):
            adopt_legacy_run(self.root, 'run-9', 'taken', 'request-conflict')
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(IdentityStore(self.root).next_value('work', None), next_value)

    def test_request_replay_is_idempotent_and_changed_inputs_reject(self):
        first = adopt_legacy_run(
            self.root, 'run-9', 'adopted-replay', 'request-replay',
        )
        project = Project(self.root)
        state = project.ledger_state('adopted-replay')
        state['status'] = 'implementing'
        state['tasks'][0]['status'] = 'running'
        project.write(
            state,
            'post-adoption-progress',
            'Continue the newly adopted run',
            action_uid='post-adoption-progress',
        )
        state = project.ledger_state('adopted-replay')
        (self.root / state['documents']['design']).write_text(
            '# Updated adopted design\n\nNew work is in progress.\n',
            encoding='utf-8',
        )
        before = snapshot(self.root)
        second = adopt_legacy_run(
            self.root, 'run-9', 'adopted-replay', 'request-replay',
        )
        self.assertEqual(second, first)
        self.assertEqual(snapshot(self.root), before)
        with self.assertRaisesRegex(
            WorkflowError,
            'LEGACY_ADOPTION_REQUEST_CONFLICT|REQUEST_INPUT_CHANGED',
        ):
            adopt_legacy_run(
                self.root, 'run-9', 'different-run', 'request-replay',
            )
        self.assertFalse((self.root / '.gsp/runs/different-run').exists())


if __name__ == '__main__':
    unittest.main()
