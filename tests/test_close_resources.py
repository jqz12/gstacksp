"""Close never publishes completion before disposing required work resources."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gsp.documents import Governance
from gsp.resource_lifecycle import register_build_resource
from gsp.runtime import Project, dispatch, parser, read


class CloseResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gsp-close-resource-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = Project(self.root)
        self.project.initialize()
        self.command('new', 'work', '--title', 'resource fixture')
        self.command('block', 'work', '--reason', 'synthetic abandoned fixture')
        state = self.state()
        self.build = self.root / '.gsp/build' / state['number'] / 'direct-run'
        register_build_resource(self.project, state, 'T01', {
            'mode': 'isolated', 'root': str(self.build), 'provenance': 'isolated',
            'owner': {'run': 'work', 'task': 'T01', 'workspace': str(self.root)},
        })
        self.build.mkdir(parents=True)
        (self.build / 'artifact').write_text('fixture', encoding='utf-8')

    def command(self, *argv):
        return dispatch(self.project, parser().parse_args(argv))

    def state(self):
        return read(self.root / '.gsp/runs/work/state.json')

    def test_cleanup_pending_keeps_state_and_archive_open(self):
        state_before = (self.root / '.gsp/runs/work/state.json').read_bytes()
        book = Governance(self.root)
        record = book.get(self.state()['work_uuid'])
        stage_before = record['stage']
        result, success = self.command('close', 'work', '--abandoned', '--reason', 'fixture')
        self.assertFalse(success)
        self.assertEqual(result['code'], 'RESOURCE_CLEANUP_PENDING')
        self.assertEqual((self.root / '.gsp/runs/work/state.json').read_bytes(), state_before)
        self.assertEqual(book.get(record['uuid'])['stage'], stage_before)
        self.assertTrue(self.build.exists())

    def test_unknown_execution_blocks_close_before_any_resource_action(self):
        state = self.state()
        state['inflight'] = {'id': 'unknown-quality', 'kind': 'quality'}
        self.project.write(state, 'fixture', 'unknown child')
        before = (self.root / '.gsp/runs/work/state.json').read_bytes()
        with patch('gsp.resource_lifecycle.remove_tree') as remove:
            result, success = self.command('close', 'work', '--abandoned', '--cleanup', '--reason', 'fixture')
        self.assertFalse(success)
        self.assertIn('EXECUTION_UNKNOWN', [gate['code'] for gate in result['blockers']])
        remove.assert_not_called()
        self.assertEqual((self.root / '.gsp/runs/work/state.json').read_bytes(), before)

    def test_active_reuse_exclusive_lease_keeps_close_open_and_claim_held(self):
        shared = Path(self.temp.name) / 'shared-build'
        shared.mkdir()
        (shared / '.gsp-build-lease.json').write_text(
            '{"phase":"running"}',
            encoding='utf-8',
        )
        state = self.state()
        register_build_resource(self.project, state, 'T01', {
            'mode': 'reuse-exclusive',
            'root': str(shared),
            'provenance': 'policy',
            'owner': {'run': 'work', 'task': 'T01', 'workspace': str(self.root)},
        })
        result, success = self.command(
            'close', 'work', '--abandoned', '--cleanup', '--reason', 'fixture',
        )
        self.assertFalse(success)
        self.assertEqual(result['code'], 'RESOURCE_CLEANUP_PENDING')
        self.assertNotEqual(self.state()['status'], 'closed')
        self.assertTrue(shared.is_dir())
        self.assertTrue((shared / '.gsp-build-lease.json').is_file())
        release = next(
            item for item in result['resources']['items']
            if Path(item['path']).resolve() == shared.resolve()
        )
        self.assertEqual(release['state'], 'pending')
        self.assertEqual(release['reason'], 'BUILD_LEASE_ACTIVE_OR_UNKNOWN')

    def test_cleanup_completes_before_archive_transition_and_retry_is_noop(self):
        original = Governance.stage
        observed = []

        def stage(book, *args, **kwargs):
            observed.append(self.build.exists())
            return original(book, *args, **kwargs)

        with patch.object(Governance, 'stage', stage):
            result, success = self.command('close', 'work', '--abandoned', '--cleanup', '--reason', 'fixture')
        self.assertTrue(success, result)
        self.assertEqual(observed, [False])
        self.assertEqual(self.state()['status'], 'closed')
        self.assertFalse(self.build.exists())
        result, success = self.command('close', 'work', '--cleanup', '--reason', 'fixture')
        self.assertTrue(success, result)
        self.assertEqual(result['resources']['items'], [])

    def test_pending_branch_or_prune_cannot_publish_closed_with_empty_resources(self):
        clean_resources = {'complete': True, 'items': []}
        with patch('gsp.resource_lifecycle.cleanup_work_resources', return_value=clean_resources), \
                patch('gsp.runtime.topology_clean', return_value={
                    'pending': True, 'prune_error': 'injected failure',
                    'refused': [], 'resources': clean_resources}):
            result, success = self.command('close', 'work', '--abandoned', '--cleanup', '--reason', 'fixture')
        self.assertFalse(success)
        self.assertEqual(result['code'], 'RESOURCE_CLEANUP_PENDING')
        self.assertNotEqual(self.state()['status'], 'closed')
