"""Ready blocks only unexplained hygiene leftovers and records Git delivery state."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from gsp.runtime import (Project, WorkflowError, _delivery_git_state,
                         _hygiene_release, atomic)


class ReadyHygieneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-ready-hygiene-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = Project(self.root)
        self.project.initialize()
        self.state = {'id': 'work', 'status': 'checking', 'evidence': [],
                      'git_topology': {'workspace_root': str(self.root), 'base_branch': 'main'}}
        self.decisions = self.project.paths('work')[1] / 'decisions.md'
        self.decisions.parent.mkdir(parents=True, exist_ok=True)
        self.decisions.write_text('# 决策记录\n\n', encoding='utf-8')

    def receipt(self, **hygiene):
        folder = self.root / '.gsp' / 'runs' / 'work' / 'evidence' / 'x'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'receipt.json'
        atomic(path, {'schema': 1, 'id': 'x', 'task': 'all', 'kind': 'tests',
                      'status': 'pass', 'hygiene': hygiene})
        self.state['evidence'] = ['.gsp/runs/work/evidence/x/receipt.json']
        return path

    def test_no_leftovers_does_not_require_a_decision(self):
        self.receipt(retained=0, truncated=False, errors=[])
        result = _hygiene_release(self.project, self.state)
        self.assertFalse(result['required'])

    def test_unexplained_retention_blocks(self):
        self.receipt(retained=2, truncated=False, errors=[])
        with self.assertRaisesRegex(WorkflowError, 'HYGIENE_LEFTOVER'):
            _hygiene_release(self.project, self.state)

    def test_truncation_and_errors_also_block(self):
        for value in ({'retained': 0, 'truncated': True, 'errors': []},
                      {'retained': 0, 'truncated': False,
                       'errors': [{'name': 'x', 'code': 'TRACKED'}]}):
            with self.subTest(value=value):
                self.receipt(**value)
                with self.assertRaisesRegex(WorkflowError, 'HYGIENE_LEFTOVER'):
                    _hygiene_release(self.project, self.state)

    def test_decision_line_releases_the_gate(self):
        self.receipt(retained=1, truncated=False, errors=[])
        with self.decisions.open('a', encoding='utf-8') as stream:
            stream.write('- 2026-11-24 [user] HYGIENE_LEFTOVER 接受：保留一次失败构建用于取证\n')
        result = _hygiene_release(self.project, self.state)
        self.assertTrue(result['required'])
        self.assertTrue(result['accepted'])
        self.assertIn('HYGIENE_LEFTOVER', result['reason'])

    def git(self, *argv):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith('GIT_CONFIG_')}
        result = subprocess.run(
            ['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
             '-c', 'core.hooksPath=', *argv],
            cwd=self.root, capture_output=True, text=True, encoding='utf-8', env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def test_delivery_records_clean_and_merged_flags(self):
        self.git('init', '-q', '-b', 'main')
        (self.root / 'README.md').write_text('# fixture\n', encoding='utf-8')
        self.git('add', '-A')
        self.git('commit', '-qm', 'baseline')
        state = {'git_topology': {'workspace_root': str(self.root), 'base_branch': 'main'}}
        clean = _delivery_git_state(self.project, state)
        self.assertTrue(clean['git_clean'])
        self.assertTrue(clean['branch_merged'])
        (self.root / 'dirty.txt').write_text('uncommitted', encoding='utf-8')
        dirty = _delivery_git_state(self.project, state)
        self.assertFalse(dirty['git_clean'])
        self.assertIn('dirty.txt', dirty['dirty_files'])
        unmerged = _delivery_git_state(self.project, {
            'git_topology': {'workspace_root': str(self.root), 'base_branch': 'no-such-base'}})
        self.assertFalse(unmerged['branch_merged'])


if __name__ == '__main__':
    unittest.main()
