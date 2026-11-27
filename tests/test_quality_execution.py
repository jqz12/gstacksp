"""Quality checks use the shared streaming executor and fail closed."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import gsp.command_execution as command_execution
from gsp.code_quality import (
    evaluate_required_quality,
    quality_phase,
    quality_contract,
    run_quality,
)
from gsp.core import atomic
from gsp.evidence import EvidenceCache, QualityReportLocator
from gsp.runtime import Project
from gsp.runtime import atomic as runtime_atomic


class QualityExecutionTests(unittest.TestCase):
    def test_quality_contract_provider_is_public_and_shared(self):
        Project(self.root).initialize()
        state = {
            "id": "W1",
            "status": "checking",
            "tasks": [],
            "current_contract": {"content_digest": "c" * 64},
        }
        self.assertEqual(
            quality_contract(Project(self.root), state),
            "c" * 64,
        )

    def test_equivalent_declared_requirements_start_one_actual_process(self):
        (self.root / 'source.py').write_text('value = 1\n', encoding='utf-8')
        first = self.check('one', "print('ok')")
        second = self.check('two', "print('ok')")
        for row in (first, second):
            row.update(reuse_evidence=True, inputs=['source.py'])
        self.write_stack([first, second])
        started = []
        with patch('gsp.code_quality.execute_command', wraps=command_execution.execute_command) as execute:
            result = run_quality(
                self.root,
                'change',
                'T1',
                'equivalent-one',
                on_started=started.append,
            )
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(len(started), 1)
        self.assertEqual(
            execute.call_args.kwargs['on_started'].__self__,
            started,
        )
        self.assertTrue(result['checks'][1]['reused'])
        self.assertEqual(result['checks'][0]['stdout'], result['checks'][1]['stdout'])

    def test_unknown_input_closure_runs_each_requirement(self):
        self.write_stack([self.check('one', "print('ok')"), self.check('two', "print('ok')")])
        with patch('gsp.code_quality.execute_command', wraps=command_execution.execute_command) as execute:
            result = run_quality(self.root, 'change', 'T1', 'unknown-two')
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(execute.call_count, 2)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gsp-quality-exec-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        (self.root / ".gsp").mkdir(parents=True)

    def write_stack(self, checks):
        (self.root / ".gsp" / "quality.json").write_text(
            json.dumps({"schema": 1, "checks": checks}), encoding="utf-8"
        )

    def check(
        self,
        ident,
        code,
        *,
        required=True,
        timeout_s=20,
        modes=None,
    ):
        return {
            "id": ident,
            "label": ident,
            "kind": "test",
            "argv": [sys.executable, "-c", code],
            "cwd": ".",
            "modes": modes or ["change"],
            "required": required,
            "timeout_s": timeout_s,
        }

    def test_quality_uses_execute_command_once_without_subprocess_run(self):
        self.write_stack([self.check("unit", "print('ok')")])
        with patch(
            "gsp.code_quality.execute_command",
            wraps=command_execution.execute_command,
            create=True,
        ) as execute:
            with patch(
                "subprocess.run",
                side_effect=AssertionError("quality bypassed execute_command"),
            ):
                result = run_quality(
                    self.root, "change", "T1", "quality-one"
                )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(execute.call_count, 1)
        folder = (
            self.root
            / ".gsp/runs/T1/quality/quality-one/evidence/unit"
        )
        self.assertTrue((folder / "command-result.json").is_file())
        self.assertTrue((folder / "stdout.txt").is_file())
        self.assertTrue((folder / "stderr.txt").is_file())

    def test_nonzero_result_is_fail_and_existing_logs_are_not_rewritten(self):
        self.write_stack([
            self.check(
                "unit",
                "print('before-failure', flush=True)\n"
                "raise SystemExit(3)\n",
            )
        ])
        with patch(
            "gsp.code_quality.atomic", wraps=runtime_atomic
        ) as atomic:
            result = run_quality(
                self.root, "change", "T2", "quality-fail"
            )
        self.assertEqual(result["status"], "fail")
        written = {
            Path(call.args[0]).name for call in atomic.call_args_list
        }
        self.assertNotIn("stdout.txt", written)
        self.assertNotIn("stderr.txt", written)
        receipt = (
            self.root
            / ".gsp/runs/T2/quality/quality-fail/evidence/unit/receipt.json"
        )
        row = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(row["exit"], 3)
        self.assertEqual(row["status"], "fail")
        self.assertIn(
            "before-failure",
            (self.root / row["stdout"]).read_text(encoding="utf-8"),
        )

    def test_timeout_unknown_and_truncated_never_pass(self):
        self.write_stack([
            self.check(
                "timeout",
                "import time\ntime.sleep(10)\n",
                timeout_s=1,
            ),
            self.check("unknown", "print('unknown')"),
            self.check(
                "truncated",
                "import sys\nsys.stdout.write('x' * 4096)\n",
            ),
        ])
        real_execute = command_execution.execute_command

        def execute(argv, cwd, env, timeout, folder, metadata, **kwargs):
            if metadata.get("check_id") == "unknown":
                folder = Path(folder)
                folder.mkdir(parents=True, exist_ok=True)
                stdout = folder / "stdout.txt"
                stderr = folder / "stderr.txt"
                stdout.write_text("unknown\n", encoding="utf-8")
                stderr.write_text("", encoding="utf-8")
                return {
                    "schema": 1,
                    "status": "unknown-state",
                    "returncode": None,
                    "launch_error": "UnknownState",
                    "interruption": None,
                    "duration_s": 0.01,
                    "stdout_path": str(stdout),
                    "stderr_path": str(stderr),
                    "log_integrity": {
                        "stdout": {"truncated": False, "bytes": 8},
                        "stderr": {"truncated": False, "bytes": 0},
                    },
                }
            return real_execute(argv, cwd, env, timeout, folder, metadata, **kwargs)

        with patch(
            "gsp.code_quality.execute_command",
            side_effect=execute,
            create=True,
        ), patch("gsp.command_execution.MAX_STREAM_BYTES", 64):
            result = run_quality(
                self.root, "change", "T3", "quality-uncertain"
            )
        rows = {row["id"]: row for row in result["checks"]}
        self.assertEqual(rows["timeout"]["status"], "fail")
        self.assertEqual(rows["unknown"]["status"], "not_checked")
        self.assertEqual(rows["truncated"]["status"], "fail")
        self.assertEqual(result["status"], "fail")

    def test_missing_tool_remains_blocked_not_checked(self):
        self.write_stack([
            {
                "id": "missing",
                "label": "missing",
                "kind": "lint",
                "argv": ["definitely-not-a-real-executable-xyz"],
                "cwd": ".",
                "modes": ["change"],
                "required": True,
                "timeout_s": 5,
            }
        ])
        result = run_quality(
            self.root, "change", "T4", "quality-missing"
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["checks"][0]["status"], "not_checked")

    def test_bound_quality_uses_source_workdir_and_control_output(self):
        control = Path(self.tmp.name) / "control"
        control.mkdir()
        (control / ".gsp").mkdir()
        source = Path(self.tmp.name) / "source"
        source.mkdir()
        Project(control).initialize()
        marker = "isolated-build-env"
        argv = [
            sys.executable,
            "-c",
            "import os; print(os.getcwd()); print(os.environ['GSP_BUILD_MARKER'])",
        ]
        (control / ".gsp" / "quality.json").write_text(
            json.dumps({
                "schema": 1,
                "checks": [{
                    "id": "release",
                    "label": "release",
                    "kind": "test",
                    "argv": argv,
                    "cwd": ".",
                    "modes": ["release"],
                    "required": True,
                    "timeout_s": 20,
                }],
            }),
            encoding="utf-8",
        )
        environment = dict(os.environ)
        environment["GSP_BUILD_MARKER"] = marker
        fingerprint = "a" * 64
        contract = "b" * 64
        result = run_quality(
            control,
            "release",
            "W1",
            "bound",
            control_root=control,
            source_root=source,
            source_fingerprint=fingerprint,
            contract=contract,
            phase=quality_phase("W1", "release"),
            env=environment,
        )

        self.assertEqual(result["binding_status"], "bound")
        self.assertEqual(result["control_root"], str(control.resolve()))
        self.assertEqual(result["project"], str(source.resolve()))
        row = result["checks"][0]
        self.assertEqual(row["source_fingerprint"], fingerprint)
        self.assertEqual(row["configuration_digest"], result["config_digest"])
        stdout = (control / row["stdout"]).read_text(encoding="utf-8")
        self.assertIn(str(source.resolve()), stdout)
        self.assertIn(marker, stdout)
        self.assertTrue((control / result["report_json"]).is_file())
        self.assertFalse((source / ".gsp" / "runs").exists())

    def test_ready_provider_requires_current_bound_release_receipts(self):
        Project(self.root).initialize()
        self.write_stack([
            self.check(
                "release",
                "print('release-ok')",
                modes=["release"],
            )
        ])
        contract = "c" * 64
        state = {
            "id": "W1",
            "work_uuid": "W1",
            "status": "checking",
            "tasks": [],
            "verification_epoch": 7,
            "current_contract": {"content_digest": contract},
        }
        fingerprint = "d" * 64
        phase = quality_phase("W1", "release", 7)
        environment = dict(os.environ)

        missing = evaluate_required_quality(
            Project(self.root), state, fingerprint,
        )
        self.assertEqual([row["code"] for row in missing], ["QUALITY_REQUIRED"])

        result = run_quality(
            self.root,
            "release",
            "W1",
            "ready-proof",
            control_root=self.root,
            source_root=self.root,
            source_fingerprint=fingerprint,
            contract=contract,
            phase=phase,
            env=environment,
            config_path=".gsp/quality.json",
            verification_epoch=7,
        )
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["locator_published"])
        self.assertEqual(QualityReportLocator(self.root).count("W1"), 1)
        self.assertEqual(
            evaluate_required_quality(Project(self.root), state, fingerprint),
            [],
        )
        original_select_bound = QualityReportLocator.select_bound

        def tracked_select_bound(locator, *args, **kwargs):
            return original_select_bound(locator, *args, **kwargs)

        with patch.object(
            QualityReportLocator, "select_bound",
            autospec=True,
            side_effect=tracked_select_bound,
        ) as select_bound:
            self.assertEqual(
                evaluate_required_quality(
                    Project(self.root), state, fingerprint,
                ),
                [],
            )
        select_bound.assert_called_once()
        stale = evaluate_required_quality(
            Project(self.root), state, "e" * 64,
        )
        self.assertEqual(stale[0]["code"], "QUALITY_STALE")
        env_drift = evaluate_required_quality(
            Project(self.root),
            state,
            fingerprint,
            env={**environment, "GSP_QUALITY_ENV_DRIFT": "1"},
        )
        self.assertEqual(env_drift[0]["code"], "QUALITY_STALE")
        epoch_drift = {
            **state,
            "verification_epoch": 8,
        }
        epoch_gate = evaluate_required_quality(
            Project(self.root),
            epoch_drift,
            fingerprint,
            env=environment,
        )
        self.assertEqual(epoch_gate[0]["code"], "QUALITY_STALE")

        config_file = self.root / ".gsp" / "quality.json"
        original_config = config_file.read_bytes()
        config_file.write_text(
            original_config.decode("utf-8").replace(
                "release-ok", "release-changed",
            ),
            encoding="utf-8",
        )
        changed_config = evaluate_required_quality(
            Project(self.root), state, fingerprint,
        )
        self.assertEqual(changed_config[0]["code"], "QUALITY_STALE")
        config_file.write_bytes(original_config)

        logout = self.root / result["checks"][0]["stdout"]
        logout.write_text("tampered", encoding="utf-8")
        unknown = evaluate_required_quality(
            Project(self.root), state, fingerprint,
        )
        self.assertEqual(unknown[0]["code"], "QUALITY_EVIDENCE_UNKNOWN")

    def test_ready_provider_uses_work_uid_separately_from_run_slug(self):
        Project(self.root).initialize()
        self.write_stack([
            self.check(
                "release",
                "print('release-ok')",
                modes=["release"],
            )
        ])
        contract = "8" * 64
        fingerprint = "9" * 64
        work_uid = "a" * 32
        state = {
            "id": "work",
            "work_uuid": work_uid,
            "status": "checking",
            "tasks": [],
            "verification_epoch": 0,
            "current_contract": {"content_digest": contract},
        }
        atomic(self.root / ".gsp/runs/work/state.json", state)
        result = run_quality(
            self.root,
            "release",
            "work",
            "slugged",
            control_root=self.root,
            source_root=self.root,
            source_fingerprint=fingerprint,
            contract=contract,
        )
        self.assertEqual(result["task_id"], "work")
        self.assertEqual(result["work_uid"], work_uid)
        self.assertEqual(result["phase"], quality_phase(work_uid, "release"))
        self.assertTrue(result["locator_published"])
        self.assertEqual(QualityReportLocator(self.root).count(work_uid), 1)
        self.assertEqual(QualityReportLocator(self.root).count("work"), 0)
        self.assertEqual(
            evaluate_required_quality(Project(self.root), state, fingerprint),
            [],
        )

    def test_explicit_quality_config_cannot_satisfy_default_ready_config(self):
        Project(self.root).initialize()
        check = self.check(
            "release",
            "print('release-ok')",
            modes=["release"],
        )
        self.write_stack([check])
        alternate = self.check(
            "release",
            "print('alternate-release-ok')",
            modes=["release"],
        )
        (self.root / ".gsp/alternate-quality.json").write_text(
            json.dumps({"schema": 1, "checks": [alternate]}),
            encoding="utf-8",
            newline="\n",
        )
        contract = "6" * 64
        fingerprint = "7" * 64
        result = run_quality(
            self.root,
            "release",
            "W1",
            "alternate-config",
            config_path=".gsp/alternate-quality.json",
            control_root=self.root,
            source_root=self.root,
            source_fingerprint=fingerprint,
            contract=contract,
        )
        state = {
            "id": "W1",
            "work_uuid": "W1",
            "status": "checking",
            "tasks": [],
            "verification_epoch": 0,
            "current_contract": {"content_digest": contract},
        }
        self.assertTrue(result["locator_published"])
        self.assertEqual(
            evaluate_required_quality(
                Project(self.root), state, fingerprint,
            )[0]["code"],
            "QUALITY_STALE",
        )

    def test_tampered_locator_report_is_unknown_and_does_not_fall_back(self):
        Project(self.root).initialize()
        self.write_stack([
            self.check(
                "release",
                "print('release-ok')",
                modes=["release"],
            )
        ])
        contract = "3" * 64
        fingerprint = "4" * 64
        environment = dict(os.environ)
        result = run_quality(
            self.root,
            "release",
            "W1",
            "tamper-proof",
            control_root=self.root,
            source_root=self.root,
            source_fingerprint=fingerprint,
            contract=contract,
            phase=quality_phase("W1", "release"),
            env=environment,
        )
        state = {
            "id": "W1",
            "work_uuid": "W1",
            "status": "checking",
            "tasks": [],
            "verification_epoch": 0,
            "current_contract": {"content_digest": contract},
        }
        self.assertEqual(
            evaluate_required_quality(
                Project(self.root), state, fingerprint, env=environment,
            ),
            [],
        )
        report = self.root / result["report_json"]
        report.write_text(
            json.dumps({**json.loads(report.read_text(encoding="utf-8")),
                        "status": "pass", "checks": []}) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        gates = evaluate_required_quality(
            Project(self.root), state, fingerprint, env=environment,
        )
        self.assertEqual(gates[0]["code"], "QUALITY_EVIDENCE_UNKNOWN")
        self.assertIn("report", gates[0]["detail"])

    def test_current_tool_digest_drift_blocks_old_bound_pass(self):
        Project(self.root).initialize()
        tool = Path(self.tmp.name) / Path(sys.executable).name
        shutil.copy2(sys.executable, tool)
        check = self.check(
            "release",
            "print('tool-ok')",
            modes=["release"],
        )
        check["argv"][0] = str(tool)
        self.write_stack([check])
        contract = "1" * 64
        fingerprint = "2" * 64
        environment = dict(os.environ)
        run_quality(
            self.root,
            "release",
            "W1",
            "tool-proof",
            control_root=self.root,
            source_root=self.root,
            source_fingerprint=fingerprint,
            contract=contract,
            phase=quality_phase("W1", "release", 0),
            env=environment,
            verification_epoch=0,
        )
        state = {
            "id": "W1",
            "status": "checking",
            "tasks": [],
            "verification_epoch": 0,
            "current_contract": {"content_digest": contract},
        }
        self.assertEqual(
            evaluate_required_quality(
                Project(self.root), state, fingerprint, env=environment,
            ),
            [],
        )
        with tool.open("ab") as stream:
            stream.write(b"\0")
        gates = evaluate_required_quality(
            Project(self.root), state, fingerprint, env=environment,
        )
        self.assertEqual(gates[0]["code"], "QUALITY_STALE")
        self.assertIn("tool", gates[0]["detail"])
        tool.unlink()
        unavailable = evaluate_required_quality(
            Project(self.root), state, fingerprint, env=environment,
        )
        self.assertEqual(unavailable[0]["code"], "QUALITY_EVIDENCE_UNKNOWN")
        self.assertTrue(unavailable[0]["detail"].startswith("NOT_CHECKED"))

    def test_legacy_quality_is_labeled_unbound(self):
        self.write_stack([
            self.check(
                "release",
                "print('legacy')",
                modes=["release"],
            )
        ])
        result = run_quality(
            self.root, "release", "W1", "legacy",
        )
        self.assertEqual(result["binding_status"], "legacy_unbound")
        self.assertIsNone(result["source_fingerprint"])

    def test_missing_quality_config_preserves_no_gate_semantics(self):
        Project(self.root).initialize()
        state = {
            "id": "W1",
            "status": "checking",
            "tasks": [],
            "current_contract": {"content_digest": "f" * 64},
        }
        self.assertEqual(
            evaluate_required_quality(Project(self.root), state, "a" * 64),
            [],
        )

    def test_later_bound_failure_cannot_fall_back_to_older_pass(self):
        Project(self.root).initialize()
        check = self.check(
            "release",
            "print('release-ok')",
            modes=["release"],
        )
        self.write_stack([check])
        contract = "c" * 64
        fingerprint = "d" * 64
        environment = dict(os.environ)
        result = run_quality(
            self.root,
            "release",
            "W1",
            "first-pass",
            control_root=self.root,
            source_root=self.root,
            source_fingerprint=fingerprint,
            contract=contract,
            phase=quality_phase("W1", "release"),
            env=environment,
        )
        identity = result["checks"][0]["execution_identity"]
        later = self.root / ".gsp/runs/W1/quality/later/evidence/release"
        execution = command_execution.execute_command(
            check["argv"], self.root, environment, 20, later, {},
        )
        receipt = later / "receipt.json"
        atomic(receipt, {
            "status": "fail",
            "execution_identity": identity,
            "execution_result": str(later / "command-result.json"),
        })
        self.assertTrue(EvidenceCache(self.root).publish(identity, receipt))

        state = {
            "id": "W1",
            "status": "checking",
            "tasks": [],
            "current_contract": {"content_digest": contract},
        }
        gates = evaluate_required_quality(
            Project(self.root), state, fingerprint,
        )
        self.assertEqual(gates[0]["code"], "QUALITY_EVIDENCE_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
