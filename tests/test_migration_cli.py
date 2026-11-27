"""Public migration entry stays read-only unless maintenance is explicit."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest

from gsp.identity_store import IdentityStore
from gsp.runtime import main
from tests.test_orchestration_migration_preview import source_snapshot


class MigrationCliTests(unittest.TestCase):
    def setUp(self):
        from tests.test_migration_apply import MigrationStageTests
        self.fixture = MigrationStageTests('test_stage_is_idempotent_backed_up_and_stays_inactive')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['--project', str(self.root), 'orchestration-migrate', *argv])
        self.assertEqual(code, 0, err.getvalue())
        return json.loads(out.getvalue())

    def test_default_preview_and_unconfirmed_stage_do_not_write_source(self):
        before = source_snapshot(self.root)
        preview = self.cli()
        self.assertEqual(preview['uid'], self.fixture.manifest.uid)
        self.assertTrue(self.cli('--action', 'stage')['dry_run'])
        self.assertEqual(source_snapshot(self.root), before)
        self.assertFalse((self.root / '.gsp/migrations').exists())

    def test_explicit_apply_and_response_loss_resume_use_the_original_request(self):
        manifest = self.root.parent / 'cli-migration-manifest.json'
        manifest.write_text(json.dumps(self.cli()), encoding='utf-8')
        first = self.cli('--action', 'apply', '--manifest', str(manifest),
                         '--request-uid', 'cli-request', '--apply')
        self.assertEqual(first['request_uid'], 'cli-request')
        second = self.cli('--action', 'resume', '--request-uid', 'cli-request', '--apply')
        self.assertEqual(second['request_uid'], first['request_uid'])
        self.assertEqual(IdentityStore(self.root).project_uid(), self.fixture.manifest.project_uid)
