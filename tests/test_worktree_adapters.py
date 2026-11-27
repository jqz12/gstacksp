"""Git-backed worktree inspection preserves ownership and disposal boundaries."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from gsp.worktree_adapters import WorktreeAdapter, WorktreeAdapterError


class WorktreeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-worktree-adapter-")
        self.addCleanup(self.tmp.cleanup)
        self.area = Path(self.tmp.name)
        self.root = self.area / "project"
        self.root.mkdir()
        self.git(self.root, "init", "-q", "-b", "main")
        (self.root / "README.md").write_text("# fixture\n", encoding="utf-8")
        self.git(self.root, "add", "README.md")
        self.git(self.root, "commit", "-qm", "baseline")
        self.external = self.area / "project-external"
        self.git(
            self.root,
            "worktree",
            "add",
            "-b",
            "feature/external",
            str(self.external),
        )
        self.adapter = WorktreeAdapter(self.root)

    def git(self, cwd, *argv):
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_CONFIG_")
        }
        result = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "core.hooksPath=",
                *argv,
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def test_same_common_dir_external_worktree_can_be_bound(self):
        binding = self.adapter.bind(
            self.external, ownership="bound", binding_uid="binding-1"
        )
        self.assertEqual(Path(binding["path"]), self.external.resolve())
        self.assertEqual(binding["branch"], "feature/external")
        self.assertEqual(binding["ownership"], "bound")
        self.assertEqual(binding["cleanup_action"], "release")
        self.assertFalse(binding["deletable"])
        self.assertIsNone(binding["host_action"])
        self.assertTrue(self.external.is_dir())

    def test_bound_external_workspace_is_the_actual_runtime_verification_root(self):
        from gsp.runtime import Project
        project = Project(self.root)
        project.initialize()
        state = {'id': 'fixture', 'project_id': project.identity()['id'], 'tasks': [],
                 'git_topology': {'workspace_root': str(self.external), 'effective_mode': 'worktree',
                                  'branch': 'feature/external', 'enforce': True}}
        self.assertEqual(project.verification_root(state), self.external.resolve())

    def test_runtime_external_binding_cannot_adopt_an_unrelated_repository(self):
        from gsp.runtime import Project, WorkflowError
        project = Project(self.root)
        project.initialize()
        other = self.area / 'other-project'
        other.mkdir()
        self.git(other, 'init', '-q', '-b', 'main')
        (other / 'README.md').write_text('# other\n', encoding='utf-8')
        self.git(other, 'add', 'README.md')
        self.git(other, 'commit', '-qm', 'other baseline')
        state = {'id': 'fixture', 'project_id': project.identity()['id'], 'tasks': [],
                 'git_topology': {'workspace_root': str(other), 'effective_mode': 'worktree',
                                  'branch': 'main', 'enforce': True}}
        with self.assertRaisesRegex(WorkflowError, 'COMMON_DIR_MISMATCH'):
            project.verification_root(state)

    def test_ownership_modes_expose_only_permitted_disposition(self):
        legacy = self.adapter.bind(self.external, ownership="legacy-managed")
        native = self.adapter.bind(self.external, ownership="native")
        bound = self.adapter.bind(self.external, ownership="bound")

        self.assertEqual(legacy["cleanup_action"], "remove")
        self.assertTrue(legacy["deletable"])
        self.assertIsNone(legacy["host_action"])

        self.assertEqual(native["cleanup_action"], "archive")
        self.assertFalse(native["deletable"])
        self.assertEqual(native["host_action"]["kind"], "worktree-archive")
        self.assertEqual(native["host_action"]["state"], "pending")

        self.assertEqual(bound["cleanup_action"], "release")
        self.assertFalse(bound["deletable"])

    def test_bound_release_is_logical_and_never_removes_the_tree(self):
        binding = self.adapter.bind(self.external, ownership="bound")
        released = self.adapter.release(binding)
        self.assertEqual(released["state"], "released")
        self.assertEqual(Path(released["path"]), self.external.resolve())
        self.assertTrue(self.external.is_dir())

    def test_native_and_managed_trees_cannot_be_released_as_bound(self):
        for ownership in ("native", "legacy-managed"):
            with self.subTest(ownership=ownership):
                binding = self.adapter.bind(
                    self.external, ownership=ownership
                )
                with self.assertRaisesRegex(
                    WorktreeAdapterError, "WORKTREE_RELEASE_INVALID"
                ):
                    self.adapter.release(binding)
                self.assertTrue(self.external.is_dir())

    def test_unregistered_copy_is_rejected(self):
        copy = self.area / "plain-copy"
        copy.mkdir()
        with self.assertRaisesRegex(
            WorktreeAdapterError, "WORKTREE_NOT_REGISTERED"
        ):
            self.adapter.bind(copy)

    def test_other_repository_with_same_marker_name_is_rejected(self):
        other = self.area / "other"
        other.mkdir()
        self.git(other, "init", "-q", "-b", "main")
        (other / "README.md").write_text("# other\n", encoding="utf-8")
        self.git(other, "add", "README.md")
        self.git(other, "commit", "-qm", "other")
        with self.assertRaisesRegex(
            WorktreeAdapterError, "WORKTREE_COMMON_DIR_MISMATCH"
        ):
            self.adapter.bind(other)


if __name__ == "__main__":
    unittest.main()
