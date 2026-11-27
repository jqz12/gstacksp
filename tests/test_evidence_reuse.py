import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from gsp.command_execution import execute_command
from gsp.evidence import (
    EvidenceCache,
    QualityReportLocator,
    execution_identity,
)
from gsp.runtime import Project, atomic


class EvidenceReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gsp-evidence-reuse-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        Project(self.root).initialize()
        self.cache = EvidenceCache(self.root)
        self.argv = [sys.executable, '-c', "print('Ran 1 test')"]

    def identity(self, **changes):
        values = dict(argv=self.argv, cwd=self.root, env=dict(os.environ),
                      inputs={'source': 'input-sha', 'scripts': {}},
                      contract='frozen-contract', phase='task:T01:green',
                      coverage='tests:T01', closed_inputs=True,
                      configuration='config-sha',
                      source_fingerprint='source-fingerprint')
        values.update(changes)
        return execution_identity(**values)

    def publish(self, identity, *, success=True):
        folder = self.root / '.gsp/runs/work/evidence' / str(self.cache.count() + 1)
        result = execute_command(self.argv, self.root, dict(os.environ), 10, folder, {})
        path = folder / 'receipt.json'
        atomic(path, {'status': 'pass' if success else 'fail',
                      'execution_identity': identity,
                      'execution_result': str(folder / 'command-result.json')})
        self.cache.publish(identity, path)
        return result, path

    def test_equivalent_second_requirement_returns_original_artifact(self):
        identity = self.identity()
        result, path = self.publish(identity)
        found = self.cache.select(identity, timeout=10)
        self.assertEqual(found['receipt'], str(path))
        self.assertEqual(found['execution']['stdout_path'], result['stdout_path'])
        self.assertEqual(self.cache.count(), 1)

    def test_environment_contract_coverage_and_source_changes_cannot_reuse(self):
        self.publish(self.identity())
        for change in ({'contract': 'new'}, {'phase': 'final-all'}, {'coverage': 'tests:all'},
                       {'inputs': {'source': 'changed'}},
                       {'configuration': 'new-config'},
                       {'source_fingerprint': 'changed-source'},
                       {'env': {'PATH': os.environ.get('PATH', ''), 'DIFFERENT': '1'}}):
            self.assertIsNone(self.cache.select(self.identity(**change), timeout=10))

    def test_later_failure_and_log_tamper_do_not_fall_back_to_old_pass(self):
        identity = self.identity()
        result, path = self.publish(identity)
        Path(result['stdout_path']).write_text('tampered', encoding='utf-8')
        self.assertIsNone(self.cache.select(identity, timeout=10))
        self.publish(identity)
        self.publish(identity, success=False)
        self.assertIsNone(self.cache.select(identity, timeout=10))

    def test_unknown_input_closure_never_reuses(self):
        self.assertIsNone(self.identity(closed_inputs=False))

    def test_reading_uninitialized_project_is_byte_read_only(self):
        root = self.root / 'uninitialized'
        root.mkdir()
        cache = EvidenceCache(root)
        self.assertIsNone(cache.select(self.identity(), timeout=10))
        self.assertEqual(list(root.iterdir()), [])

    def write_quality_report(
        self, work_uid, run_id, *, status='bound', run_slug=None,
    ):
        run_slug = run_slug or work_uid
        folder = self.root / '.gsp/runs' / run_slug / 'quality' / run_id
        folder.mkdir(parents=True)
        report = {
            'task_id': run_slug,
            'work_uid': work_uid,
            'mode': 'release',
            'phase': f'quality:release:{work_uid}:epoch:0',
            'verification_epoch': 0,
            'binding_status': status,
            'source_fingerprint': 'a' * 64,
            'contract': 'b' * 64,
            'config_digest': 'c' * 64,
            'binding': {'status': status},
        }
        path = folder / 'report.json'
        path.write_text(
            json.dumps(report, sort_keys=True) + '\n',
            encoding='utf-8',
            newline='\n',
        )
        return path, report

    def test_quality_locator_uses_work_uid_with_distinct_run_slug(self):
        path, report = self.write_quality_report(
            'WORK-UUID', 'slugged', run_slug='work',
        )
        state_path = (
            self.root / '.gsp/runs/work/state.json'
        )
        state_path.write_text(
            json.dumps({'id': 'work', 'work_uuid': 'WORK-UUID'}) + '\n',
            encoding='utf-8',
            newline='\n',
        )
        locator = QualityReportLocator(self.root)
        self.assertEqual(locator.rebuild_work('WORK-UUID'), 1)
        selected = locator.select_bound(
            'WORK-UUID', report['mode'], report['phase'],
            report['verification_epoch'], report['source_fingerprint'],
            report['contract'], report['config_digest'],
        )
        self.assertEqual(selected['report'], str(path.relative_to(self.root)))
        self.assertEqual(locator.count('WORK-UUID'), 1)
        self.assertEqual(locator.count('work'), 0)
        self.assertIsNone(locator.latest('work', 'release'))

    def test_quality_locator_selects_only_the_exact_bound_report(self):
        path, report = self.write_quality_report('WORK-A', 'exact')
        locator = QualityReportLocator(self.root)
        self.assertTrue(locator.publish(
            path, 'WORK-A', report['mode'], report['phase'],
            report['verification_epoch'], report['binding_status'],
            report['source_fingerprint'], report['contract'],
            report['config_digest'],
        ))
        selected = locator.select_bound(
            'WORK-A', report['mode'], report['phase'],
            report['verification_epoch'], report['source_fingerprint'],
            report['contract'], report['config_digest'],
        )
        self.assertEqual(selected['report'], str(path.relative_to(self.root)))
        self.assertIsNone(locator.select_bound(
            'WORK-B', report['mode'], report['phase'],
            report['verification_epoch'], report['source_fingerprint'],
            report['contract'], report['config_digest'],
        ))

    def test_quality_locator_rebuild_is_scoped_to_one_work(self):
        first, _ = self.write_quality_report('WORK-A', 'first')
        second, _ = self.write_quality_report('WORK-B', 'second')
        locator = QualityReportLocator(self.root)
        self.assertEqual(locator.rebuild_work('WORK-A'), 1)
        self.assertEqual(locator.count('WORK-A'), 1)
        self.assertEqual(locator.count('WORK-B'), 0)
        self.assertTrue(first.is_file())
        self.assertTrue(second.is_file())

    def test_quality_locator_read_is_zero_write_before_initialization(self):
        root = self.root / 'uninitialized-quality'
        root.mkdir()
        locator = QualityReportLocator(root)
        self.assertIsNone(locator.select_bound(
            'WORK', 'release', 'quality:release:WORK:epoch:0',
            0, 'a' * 64, 'b' * 64, 'c' * 64,
        ))
        self.assertIsNone(locator.latest('WORK', 'release'))
        self.assertEqual(list(root.iterdir()), [])
