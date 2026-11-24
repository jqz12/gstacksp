"""Build policy defaults, validation and bounded temporary-root reclamation."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gsp.build_policy import (load_policy, reclaim_new_entries, snapshot_names,
                              temp_env)
from gsp.runtime import WorkflowError


def write_policy(root, value):
    path = Path(root) / '.gsp' / 'build-policy.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return path


def git(root, *argv):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('GIT_CONFIG_')}
    result = subprocess.run(
        ['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
         '-c', 'core.hooksPath=', *argv],
        cwd=root, capture_output=True, text=True, encoding='utf-8', env=env)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return result.stdout.strip()


class BuildPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-build-policy-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def policy(self):
        return load_policy(self.root)

    def test_defaults_and_cargo_detection(self):
        policy = self.policy()
        self.assertEqual(policy['version'], 1)
        self.assertTrue(policy['temp_isolation'])
        self.assertEqual(policy['temp_roots'], [])
        self.assertEqual(policy['retain_on_failure'],
                         {'max_entries': 1, 'max_bytes': 50 * 1024 * 1024})
        self.assertEqual(policy['worktree_build'], 'managed')
        (self.root / 'Cargo.toml').write_text('[package]\nname = "fixture"\n', encoding='utf-8')
        self.assertEqual(self.policy()['temp_roots'], ['target/tmp'])

    def test_invalid_paths_are_rejected_not_silently_ignored(self):
        for value in ('/absolute/tmp', '../escape', 'C:/escape', ''):
            with self.subTest(value=value):
                write_policy(self.root, {'temp_roots': [value]})
                with self.assertRaisesRegex(WorkflowError, 'BUILD_POLICY_INVALID'):
                    self.policy()

    def test_invalid_json_and_unknown_values_are_rejected(self):
        path = self.root / '.gsp' / 'build-policy.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{not json', encoding='utf-8')
        with self.assertRaisesRegex(WorkflowError, 'BUILD_POLICY_INVALID'):
            self.policy()
        write_policy(self.root, {'worktree_build': 'maybe'})
        with self.assertRaisesRegex(WorkflowError, 'BUILD_POLICY_INVALID'):
            self.policy()
        write_policy(self.root, {'version': 2})
        with self.assertRaisesRegex(WorkflowError, 'BUILD_POLICY_INVALID'):
            self.policy()
        write_policy(self.root, {'retain_on_failure': {'max_entries': -1, 'max_bytes': 0}})
        with self.assertRaisesRegex(WorkflowError, 'BUILD_POLICY_INVALID'):
            self.policy()

    def test_explicit_values_merge_over_defaults(self):
        write_policy(self.root, {
            'temp_isolation': False,
            'temp_roots': ['target/tmp', 'coverage/tmp'],
            'retain_on_failure': {'max_entries': 3},
            'limits': {'max_temp_entries': 50},
            'profile_expectations': {'debug': False},
        })
        policy = self.policy()
        self.assertFalse(policy['temp_isolation'])
        self.assertEqual(policy['temp_roots'], ['target/tmp', 'coverage/tmp'])
        self.assertEqual(policy['retain_on_failure']['max_entries'], 3)
        self.assertEqual(policy['retain_on_failure']['max_bytes'], 50 * 1024 * 1024)
        self.assertEqual(policy['limits'], {'max_build_bytes': 0, 'max_temp_entries': 50})
        self.assertEqual(policy['profile_expectations'],
                         {'debug': False, 'incremental_for_tests': None})

    def test_temp_env_points_every_variable_at_the_run_directory(self):
        run_dir = self.root / '.gsp' / 'tmp' / 'runs' / 'abc'
        run_dir.mkdir(parents=True)
        env = temp_env(run_dir)
        self.assertEqual(set(env), {'TMP', 'TEMP', 'TMPDIR'})
        self.assertEqual({value for value in env.values()}, {str(run_dir)})

    def test_snapshot_is_one_level_and_empty_for_missing_root(self):
        self.assertEqual(snapshot_names(self.root, 'target/tmp'), {})
        base = self.root / 'target' / 'tmp'
        base.mkdir(parents=True)
        (base / 'one').mkdir()
        (base / 'two').write_text('x', encoding='utf-8')
        self.assertEqual(set(snapshot_names(self.root, 'target/tmp')), {'one', 'two'})

    def test_success_reclaims_only_new_entries_and_keeps_preexisting(self):
        base = self.root / 'target' / 'tmp'
        base.mkdir(parents=True)
        (base / 'preexisting').mkdir()
        before = snapshot_names(self.root, 'target/tmp')
        (base / 'new-a').mkdir()
        (base / 'new-b').write_text('payload', encoding='utf-8')
        result = reclaim_new_entries(self.root, 'target/tmp', before, success=True)
        self.assertEqual(result['created'], 2)
        self.assertEqual(result['removed'], 2)
        self.assertEqual(result['retained'], 0)
        self.assertTrue((base / 'preexisting').is_dir())
        self.assertFalse((base / 'new-a').exists())
        self.assertFalse((base / 'new-b').exists())

    def test_failure_retains_only_the_newest_entry_within_the_limit(self):
        base = self.root / 'target' / 'tmp'
        base.mkdir(parents=True)
        before = snapshot_names(self.root, 'target/tmp')
        for index in range(3):
            folder = base / f'failed-{index}'
            folder.mkdir()
            (folder / 'payload.bin').write_bytes(b'x' * 1024)
            os.utime(folder, (1_700_000_000 + index, 1_700_000_000 + index))
        result = reclaim_new_entries(
            self.root, 'target/tmp', before,
            retain={'max_entries': 1, 'max_bytes': 50 * 1024 * 1024}, success=False)
        self.assertEqual(result['created'], 3)
        self.assertEqual(result['retained'], 1)
        self.assertEqual(result['removed'], 2)
        self.assertGreater(result['retained_bytes'], 0)
        self.assertEqual([path.name for path in base.iterdir()], ['failed-2'])

    def test_git_tracked_and_symlink_entries_are_not_deleted(self):
        git(self.root, 'init', '-q', '-b', 'main')
        base = self.root / 'target' / 'tmp'
        base.mkdir(parents=True)
        (self.root / '.gitignore').write_text('target/\n', encoding='utf-8')
        git(self.root, 'add', '.gitignore')
        git(self.root, 'commit', '-qm', 'baseline')
        before = snapshot_names(self.root, 'target/tmp')
        tracked = base / 'tracked.txt'
        tracked.write_text('keep', encoding='utf-8')
        git(self.root, 'add', '-f', 'target/tmp/tracked.txt')
        outside = self.root.parent / 'outside-entry'
        outside.mkdir(exist_ok=True)
        link = base / 'link'
        linked = False
        try:
            os.symlink(outside, link, target_is_directory=True)
            linked = True
        except (OSError, NotImplementedError):
            pass
        result = reclaim_new_entries(self.root, 'target/tmp', before, success=True)
        codes = {error['code'] for error in result['errors']}
        self.assertIn('TRACKED', codes)
        self.assertTrue(tracked.exists())
        if linked:
            self.assertIn('SYMLINK', codes)
            self.assertTrue(link.exists())

    def test_truncated_enumeration_deletes_nothing(self):
        base = self.root / 'target' / 'tmp'
        base.mkdir(parents=True)
        before = snapshot_names(self.root, 'target/tmp')
        for index in range(4):
            (base / f'entry-{index}').mkdir()
        result = reclaim_new_entries(self.root, 'target/tmp', before, success=True,
                                     max_entries=2)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['removed'], 0)
        self.assertEqual(len(list(base.iterdir())), 4)


if __name__ == '__main__':
    unittest.main()
