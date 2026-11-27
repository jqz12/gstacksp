"""Effective evidence selection: newest pass per entry, failures stay lane-local."""
import sys
import tempfile
import unittest
from pathlib import Path

from gsp.runtime import (Project, WorkflowError, atomic, dispatch, evidence_entry,
                         parser, read, select_evidence)
from tests.process_helpers import run as run_process


WORKSPACE = str((Path(tempfile.gettempdir()) / 'gsp-evidence-selection-workspace').resolve())
OTHER_WORKSPACE = str((Path(tempfile.gettempdir()) / 'gsp-evidence-selection-other').resolve())


def receipt(row_id, *, task='T01', kind='tests', status='pass', argv=('python', '-m', 'unittest'),
            fingerprint='fingerprint', contract='contract', epoch=0, workspace=WORKSPACE):
    return {
        'id': row_id,
        'task': task,
        'kind': kind,
        'status': status,
        'argv': list(argv),
        'fingerprint': fingerprint,
        'contract': contract,
        'verification_epoch': epoch,
        'workspace_root': workspace,
        'at': '2026-11-24T00:00:00+00:00',
        'logs': {},
    }


def query(**updates):
    value = {
        'task': 'T01',
        'workspace_root': WORKSPACE,
        'fingerprint': 'fingerprint',
        'contract': 'contract',
        'verification_epoch': 0,
    }
    value.update(updates)
    return value


class EvidenceSelectionUnitTests(unittest.TestCase):
    def test_failure_requires_same_entry_success(self):
        failed = receipt('t-failed', status='fail', argv=('python', 'suite-a.py'))
        other = receipt('t-other', argv=('python', 'suite-b.py'))
        selected = select_evidence([failed, other], query())
        self.assertEqual([row['id'] for row in selected['tests']], ['t-other'])
        self.assertEqual([row['id'] for row in selected['blocking_failures']], ['t-failed'])

        repaired = receipt('t-repaired', argv=('python', 'suite-a.py'))
        selected = select_evidence([failed, other, repaired], query())
        self.assertEqual(selected['blocking_failures'], [])
        self.assertEqual({row['id'] for row in selected['tests']}, {'t-other', 't-repaired'})

    def test_changed_epoch_workspace_entry_contract_or_input_invalidates(self):
        row = receipt('t-pass')
        self.assertEqual(len(select_evidence([row], query())['tests']), 1)

        cases = [
            (query(verification_epoch=1), 'before-recovery'),
            (query(workspace_root=OTHER_WORKSPACE), 'workspace-changed'),
            (query(entry=evidence_entry(receipt('other', argv=('python', 'other.py')))), 'entry-changed'),
            (query(contract='contract-2'), 'contract-changed'),
            (query(fingerprint='fingerprint-2'), 'input-changed'),
        ]
        for value, reason in cases:
            with self.subTest(reason=reason):
                selected = select_evidence([row], value)
                self.assertEqual(selected['tests'], [])
                self.assertEqual(selected['invalid_reasons'][0]['reason'], reason)

    def test_all_receipt_not_substituted_for_task(self):
        final = receipt('all-pass', task='all', kind='tests')
        selected = select_evidence([final], query())
        self.assertEqual(selected['tests'], [])
        self.assertEqual(selected['invalid_reasons'], [])


class EvidenceSelectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-evidence-selection-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = Project(self.root)
        self.project.initialize()

    def cmd(self, *argv, expect_ok=True):
        args = parser().parse_args(list(argv[:argv.index('--')])) if '--' in argv else parser().parse_args(list(argv))
        if '--' in argv:
            args.argv = list(argv[argv.index('--') + 1:])
        result, ok = dispatch(self.project, args)
        if expect_ok:
            self.assertTrue(ok, result)
        return result, ok

    def git(self, *argv):
        result = run_process(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                              '-c', 'core.hooksPath=', *argv], cwd=self.root,
                             capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def state(self):
        return read(self.root / '.gsp/runs/work/state.json')

    def prepare(self):
        self.git('init', '-q', '-b', 'main')
        (self.root / 'source.txt').write_text('baseline', encoding='utf-8')
        self.git('add', 'source.txt', '.gitignore')
        self.git('commit', '-qm', 'baseline')
        self.cmd('new', 'work', '--title', '证据选择', '--intent', 'single-task')
        state = self.state()
        # This published evidence-selection fixture predates the convergence schema.
        # Keep the product gate intact; only this synthetic work is legacy.
        state.pop('convergence_schema', None)
        self.project.write(
            state,
            'fixture-legacy-convergence',
            'legacy evidence-selection fixture created before convergence schema',
            action_uid='fixture:legacy-convergence:evidence-selection',
        )
        self.project.document(state, 'design').write_text('# Design\nFixture evidence selection.', encoding='utf-8')
        self.cmd('approve', 'work', '--quote', 'SYNTHETIC fixture approved')
        self.cmd('task-add', 'work', 'T01', '--title', 'Implementation T01', '--acceptance', 'Checks pass')
        self.project.document(state, 'plan').write_text('# Plan\nT01 implementation and checks.', encoding='utf-8')
        self.cmd('seal-plan', 'work')
        self.cmd('scope', 'work', '--off', '--quote', 'SYNTHETIC no freeze')
        self.cmd('topology-select', 'work', '--mode', 'in-place', '--quote', 'SYNTHETIC control writer')
        self.cmd('advance', 'work', '--to', 'execute')
        self.cmd('task-start', 'work', 'T01')

    def program(self, name, body):
        path = self.root / '.gsp' / 'fixtures' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8')
        return str(path)

    def check(self, kind, script, expect_ok=True):
        return self.cmd('run', 'work', '--task', 'T01', '--kind', kind, '--',
                        sys.executable, script, expect_ok=expect_ok)

    def test_tests_then_check_still_completes(self):
        self.prepare()
        passing = self.program('pass.py', 'import unittest\n'
                                          'class Check(unittest.TestCase):\n'
                                          ' def test_ok(self): self.assertTrue(True)\n'
                                          'unittest.main()\n')
        tests, _ = self.check('tests', passing)
        check, _ = self.check('check', passing)
        self.cmd('task-done', 'work', 'T01')
        task = next(item for item in self.state()['tasks'] if item['id'] == 'T01')
        self.assertEqual(task['evidence'], tests['id'])
        self.assertNotEqual(task['evidence'], check['id'])

    def test_failure_then_unrelated_check_cannot_complete(self):
        self.prepare()
        passing = self.program('pass.py', 'import unittest\n'
                                          'class Check(unittest.TestCase):\n'
                                          ' def test_ok(self): self.assertTrue(True)\n'
                                          'unittest.main()\n')
        failing = self.program('fail.py', 'raise SystemExit(7)\n')
        other = self.program('other.py', 'print("other entry")\n')
        self.check('tests', passing)
        self.check('check', failing, expect_ok=False)
        self.check('check', other)
        with self.assertRaisesRegex(WorkflowError, 'EVIDENCE_APPLICABLE_FAILURE'):
            self.cmd('task-done', 'work', 'T01')

    def test_log_tamper_rejected(self):
        self.prepare()
        passing = self.program('pass.py', 'import unittest\n'
                                          'class Check(unittest.TestCase):\n'
                                          ' def test_ok(self): self.assertTrue(True)\n'
                                          'unittest.main()\n')
        tests, _ = self.check('tests', passing)
        folder = self.root / next(path for path in self.state()['evidence']
                                  if path.endswith(tests['id'] + '/receipt.json'))
        (folder.parent / 'stdout.txt').write_text('tampered', encoding='utf-8')
        with self.assertRaisesRegex(WorkflowError, 'EVIDENCE_TAMPERED'):
            self.cmd('task-done', 'work', 'T01')


if __name__ == '__main__':
    unittest.main()
