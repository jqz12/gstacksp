import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from gsp.runtime import Project, dispatch, parser, read


class NewNumberSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-new-number-")
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Number Sync Test")
        self.git("config", "user.email", "numbersync@example.invalid")
        (self.root / "README.md").write_text("# sync\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-qm", "chore: baseline")
        self.project = Project(self.root)
        self.project.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        result = subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def cmd(self, *argv):
        return dispatch(self.project, parser().parse_args(list(argv)))[0]

    def set_counter(self, value):
        counter = self.root / ".gsp" / "work-counter.json"
        counter.parent.mkdir(parents=True, exist_ok=True)
        counter.write_text(json.dumps({"next": value}), encoding="utf-8")

    def counter(self):
        return read(self.root / ".gsp" / "work-counter.json")["next"]

    def test_new_explicit_id_advances_counter(self):
        self.set_counter(27)
        self.cmd("new", "GSP-000030", "--title", "显式编号")

        state = read(self.root / ".gsp/runs/GSP-000030/state.json")
        self.assertEqual(state["number"], "GSP-000030")
        self.assertGreaterEqual(self.counter(), 31)

        self.cmd("new", "auto", "--title", "自动编号")
        auto_state = read(self.root / ".gsp/runs/auto/state.json")
        self.assertGreaterEqual(int(auto_state["number"].split("-")[1]), 31)

    def test_new_explicit_id_does_not_rewind(self):
        self.set_counter(31)
        self.cmd("new", "GSP-000005", "--title", "回退编号")

        state = read(self.root / ".gsp/runs/GSP-000005/state.json")
        self.assertEqual(state["number"], "GSP-000005")
        self.assertGreaterEqual(self.counter(), 32)


if __name__ == "__main__":
    unittest.main()
