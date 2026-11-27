"""Bounded, declaration-driven project quality checks.

The quality runner deliberately does not walk the repository or guess commands
from a tool name.  A project declares its commands in ``.gsp/quality.json``;
the runner executes only those argv arrays and stores small, redacted receipts.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid
from collections.abc import Mapping

from .core import WorkflowError, atomic, inside, now, redact
from .command_execution import execute_command
from .evidence import (
    EvidenceCache,
    QualityReportLocator,
    declared_inputs,
    execution_identity,
)

MODES = {"change", "baseline", "release"}
KINDS = {"build", "format", "lint", "test", "security", "maintainability", "contract", "runtime"}
MAX_CONFIG_BYTES = 512 * 1024
DEFAULT_TIMEOUT = 900
READY_MODE = "release"
SOURCE_FINGERPRINT_INPUT = "__source_fingerprint__"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _object_digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _valid_digest(value) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _contract_digest(value) -> str | None:
    if _valid_digest(value):
        return value
    return _object_digest(value) if value is not None else None


def quality_phase(task_id, mode: str = READY_MODE,
                  verification_epoch=0) -> str:
    """Stable phase accepted by the ready-time quality provider."""
    epoch = verification_epoch if type(verification_epoch) is int else 0
    return f"quality:{mode}:{task_id}:epoch:{max(0, epoch)}"


def quality_work_uid(state):
    """Return the exact work identity, independent of its run-directory slug."""
    value = state.get("work_uuid") or state.get("id")
    return value if isinstance(value, str) and value else None


def bound_quality_phase(state, mode=READY_MODE):
    """A final candidate cannot reuse a task's earlier validation phase."""
    phase = quality_phase(quality_work_uid(state), mode, state.get('verification_epoch', 0))
    tasks = state.get('tasks') or []
    if mode == READY_MODE and tasks and all(row.get('status') == 'done' for row in tasks):
        phase += ':final-candidate'
    return phase


def _run_work_uid(control_root, task_id, explicit=None):
    """Resolve the work UID from the run state while retaining ``task_id`` as slug."""
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit:
            raise WorkflowError("QUALITY_WORK_UID_INVALID")
        return explicit
    if not isinstance(task_id, str) or not task_id:
        raise WorkflowError("QUALITY_TASK_ID_INVALID")
    state_path = control_root / ".gsp" / "runs" / task_id / "state.json"
    if not inside(control_root, state_path):
        raise WorkflowError("QUALITY_TASK_ID_INVALID")
    if not state_path.is_file():
        return task_id
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise WorkflowError("QUALITY_WORK_UID_UNKNOWN run state is unreadable") from exc
    if not isinstance(state, dict) or state.get("id") != task_id:
        raise WorkflowError("QUALITY_WORK_UID_UNKNOWN run state does not match task")
    work_uid = quality_work_uid(state)
    if work_uid is None:
        raise WorkflowError("QUALITY_WORK_UID_UNKNOWN current work UID is unavailable")
    return work_uid


def _quality_gate(code, detail, state, evidence=None):
    return {
        "code": code,
        "detail": detail,
        "required_action": "run the current bound release quality checks and resolve their evidence",
        "stage": state.get("status"),
        "documents": [],
        "evidence": evidence or [],
    }


def _expected_contract(project, state):
    current = state.get("current_contract")
    if isinstance(current, dict):
        value = current.get("content_digest") or current.get("digest")
        return value if _valid_digest(value) else None
    if isinstance(current, str):
        return current if _valid_digest(current) else None
    try:
        design = project.document(state, "design")
        plan = project.document(state, "plan")
        return _object_digest({
            "design": _digest(design),
            "plan": _digest(plan),
            "tasks": project.task_definition_hash(state),
        })
    except (AttributeError, OSError, WorkflowError):
        return None


# Public provider for callers that must use the same contract source for the
# producer and ready-time consumer without reimplementing the digest format.
quality_contract = _expected_contract


def _identity_inputs(source_root, check, source_fingerprint, include_source):
    inputs = declared_inputs(
        source_root, check["inputs"], check["argv"],
    )
    if not include_source:
        return inputs
    value = dict(inputs or {})
    value[SOURCE_FINGERPRINT_INPUT] = source_fingerprint
    return value


def quality_environment(env=None):
    """Return the exact environment used by both the producer and consumer."""
    value = os.environ if env is None else env
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise WorkflowError("QUALITY_ENV_INVALID expected a string mapping")
    return dict(value)


def quality_identity(check, *, source_root, source_fingerprint, contract,
                     phase, environment, configuration_digest):
    """Build the one execution proof shared by quality and ready evaluation."""
    source_root = Path(source_root).resolve()
    include_source = _valid_digest(source_fingerprint)
    inputs = _identity_inputs(
        source_root, check, source_fingerprint, include_source,
    )
    return execution_identity(
        argv=check["argv"],
        cwd=source_root / check["cwd"],
        env=environment,
        inputs=inputs,
        contract=contract,
        phase=phase,
        coverage="quality:" + check["kind"],
        closed_inputs=bool(
            inputs and (include_source or check["reuse_evidence"])
        ),
        configuration=configuration_digest,
        source_fingerprint=(
            source_fingerprint if include_source else None
        ),
    )


def _suggestions(root: Path) -> list[dict]:
    """Suggest likely checks without running or inventing a project contract."""
    found: list[dict] = []
    if (root / "Cargo.toml").exists():
        found.extend([
            {"kind": "build", "label": "Rust type/build check", "argv": ["cargo", "check", "--locked"]},
            {"kind": "test", "label": "Rust tests", "argv": ["cargo", "test", "--locked"]},
            {"kind": "lint", "label": "Rust lint", "argv": ["cargo", "clippy", "--locked", "--", "-D", "warnings"]},
        ])
    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists() or (root / "tests").is_dir():
        found.extend([
            {"kind": "test", "label": "Python tests", "argv": ["python", "-m", "pytest"]},
            {"kind": "lint", "label": "Python lint", "argv": ["ruff", "check", "."]},
        ])
    package = root / "package.json"
    if package.exists():
        found.extend([
            {"kind": "build", "label": "JavaScript build", "argv": ["npm", "run", "build"]},
            {"kind": "test", "label": "JavaScript tests", "argv": ["npm", "test"]},
            {"kind": "lint", "label": "JavaScript lint", "argv": ["npm", "run", "lint"]},
        ])
    if (root / "go.mod").exists():
        found.extend([
            {"kind": "build", "label": "Go build", "argv": ["go", "build", "./..."]},
            {"kind": "test", "label": "Go tests", "argv": ["go", "test", "./..."]},
            {"kind": "lint", "label": "Go vet", "argv": ["go", "vet", "./..."]},
        ])
    return found


def load_stack(root: Path, config_path: str | None = None,
               source_root: Path | None = None) -> dict:
    root = Path(root).resolve()
    source_root = Path(source_root or root).resolve()
    relative_config = config_path or ".gsp/quality.json"
    if Path(relative_config).is_absolute():
        raise WorkflowError("QUALITY_CONFIG_ESCAPE configuration must be project-relative")
    path = (root / relative_config).resolve()
    if not inside(root, path):
        raise WorkflowError("QUALITY_CONFIG_ESCAPE configuration must remain inside the project")
    if not path.exists():
        return {"schema": 1, "source": "missing", "path": str(path.relative_to(root)).replace("\\", "/"),
                "checks": [], "suggestions": _suggestions(source_root)}
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise WorkflowError("QUALITY_CONFIG_TOO_LARGE limit is 512 KiB")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise WorkflowError(f"QUALITY_CONFIG_INVALID {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("checks"), list):
        raise WorkflowError("QUALITY_CONFIG_INVALID expected schema 1 and checks list")
    checks = []
    seen = set()
    for index, item in enumerate(data["checks"]):
        if not isinstance(item, dict):
            raise WorkflowError(f"QUALITY_CHECK_INVALID index {index}")
        ident = item.get("id")
        argv = item.get("argv")
        kind = item.get("kind", "build")
        modes = item.get("modes", sorted(MODES))
        if not isinstance(ident, str) or not ident or ident in seen:
            raise WorkflowError(f"QUALITY_CHECK_ID_INVALID index {index}")
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) and arg for arg in argv):
            raise WorkflowError(f"QUALITY_CHECK_ARGV_INVALID {ident}")
        if kind not in KINDS or not isinstance(modes, list) or not modes or any(mode not in MODES for mode in modes):
            raise WorkflowError(f"QUALITY_CHECK_METADATA_INVALID {ident}")
        cwd = item.get("cwd", ".")
        if (not isinstance(cwd, str) or Path(cwd).is_absolute()
                or not (candidate := (source_root / cwd).resolve()).is_relative_to(source_root)
                or not candidate.is_dir()):
            raise WorkflowError(f"QUALITY_CHECK_CWD_INVALID {ident}")
        timeout = item.get("timeout_s", DEFAULT_TIMEOUT)
        if not isinstance(timeout, int) or timeout < 1 or timeout > 7200:
            raise WorkflowError(f"QUALITY_CHECK_TIMEOUT_INVALID {ident}")
        seen.add(ident)
        checks.append({
            "id": ident, "label": item.get("label", ident), "kind": kind, "argv": argv,
            "cwd": cwd.replace("\\", "/"), "modes": modes,
            "required": bool(item.get("required", True)), "timeout_s": timeout,
            "reuse_evidence": item.get("reuse_evidence") is True,
            "inputs": item.get("inputs", []),
        })
    return {"schema": 1, "source": str(path.relative_to(root)).replace("\\", "/"),
            "path": str(path.relative_to(root)).replace("\\", "/"), "checks": checks,
            "suggestions": _suggestions(source_root)}


def _route(mode: str, status: str) -> dict:
    from dataclasses import asdict
    from .routing import route_for
    condition = ('required_check_failed' if status in {'fail', 'blocked'} else
                 'quality_stack_missing_or_tool_unavailable' if status == 'not_checked' else
                 'release_checks_passed' if mode == 'release' else
                 'baseline_checks_passed' if mode == 'baseline' else 'change_checks_passed')
    return asdict(route_for(condition))


def _execution_outcome(execution, check):
    status = execution.get("status")
    returncode = execution.get("returncode")
    integrity = execution.get("log_integrity") or {}
    truncated = [
        name for name in ("stdout", "stderr")
        if bool((integrity.get(name) or {}).get("truncated"))
    ]
    if truncated:
        return "fail", "log truncated: " + ", ".join(truncated)
    if status == "timeout":
        return "fail", f"timeout after {check['timeout_s']}s"
    if status == "interrupted":
        return "fail", "interrupted: " + str(
            execution.get("launch_error") or execution.get("interruption")
            or "unknown")
    if status == "failed" or (
        isinstance(returncode, int) and returncode != 0
    ):
        return "fail", None
    if status == "launch-failed":
        return "not_checked", "could not start: " + str(
            execution.get("launch_error") or "unknown error")
    if status != "completed" or returncode != 0:
        return "not_checked", "unknown terminal state: " + str(status)
    if not all(
        isinstance(integrity.get(name), dict)
        for name in ("stdout", "stderr")
    ):
        return "not_checked", "unknown log integrity"
    return "pass", None


def _validate_quality_report(report, report_path, control_root, state, work_uid,
                             source_root,
                             source_fingerprint, contract, config_digest,
                             checks, environment):
    evidence = [str(report_path.relative_to(control_root)).replace("\\", "/")]
    if not isinstance(report, dict):
        return "QUALITY_EVIDENCE_UNKNOWN", "quality report is unreadable", evidence
    binding = report.get("binding")
    if not isinstance(binding, dict):
        return "QUALITY_UNBOUND", "quality report has no binding facts", evidence
    if binding.get("status") != "bound" or report.get("binding_status") != "bound":
        return "QUALITY_UNBOUND", "legacy quality reports cannot prove ready delivery", evidence
    expected_phase = bound_quality_phase(state)
    verification_epoch = (
        state.get("verification_epoch")
        if type(state.get("verification_epoch")) is int else 0
    )
    expected = {
        "task_id": state.get("id"),
        "work_uid": work_uid,
        "mode": READY_MODE,
        "source_fingerprint": source_fingerprint,
        "contract": contract,
        "config_digest": config_digest,
        "phase": expected_phase,
        "verification_epoch": verification_epoch,
        "source_root": str(source_root),
        "environment_digest": _object_digest(environment),
    }
    reported_work_uid = report.get("work_uid")
    if reported_work_uid is None:
        reported_work_uid = (
            report.get("task_id")
            if state.get("work_uuid") in {None, state.get("id")}
            else None
        )
    actual = {
        "task_id": report.get("task_id"),
        "work_uid": reported_work_uid,
        "mode": report.get("mode"),
        "source_fingerprint": report.get("source_fingerprint"),
        "contract": report.get("contract"),
        "config_digest": report.get("config_digest"),
        "phase": report.get("phase"),
        "verification_epoch": report.get("verification_epoch", 0),
        "source_root": binding.get("source_root"),
        "environment_digest": binding.get("environment_digest"),
    }
    changed = [name for name, value in expected.items() if actual[name] != value]
    if changed:
        return "QUALITY_STALE", "quality binding changed: " + ", ".join(changed), evidence
    if report.get("status") not in {"pass", "warn"}:
        return "QUALITY_REQUIRED_CHECK_FAILED", (
            "bound quality run did not pass: " + str(report.get("status"))
        ), evidence

    rows = {
        row.get("id"): row
        for row in report.get("checks") or []
        if isinstance(row, dict)
    }
    cache = EvidenceCache(control_root)
    for check in checks:
        row = rows.get(check["id"])
        if row is None or row.get("required") is not True:
            return "QUALITY_REQUIRED", "required check has no bound result: " + check["id"], evidence
        if row.get("status") != "pass":
            return "QUALITY_REQUIRED_CHECK_FAILED", (
                f"{check['id']} status is {row.get('status')}"
            ), evidence
        identity = row.get("execution_identity")
        if not isinstance(identity, dict):
            return "QUALITY_UNBOUND", "required check has no execution identity: " + check["id"], evidence
        expected_identity = quality_identity(
            check,
            source_root=source_root,
            source_fingerprint=source_fingerprint,
            contract=contract,
            phase=expected_phase,
            environment=environment,
            configuration_digest=config_digest,
        )
        if expected_identity is None:
            return "QUALITY_EVIDENCE_UNKNOWN", (
                f"NOT_CHECKED cannot prove current environment/tool for {check['id']}"
            ), evidence
        if identity != expected_identity:
            changed = sorted(
                key for key in set(identity) | set(expected_identity)
                if identity.get(key) != expected_identity.get(key)
            )
            return "QUALITY_STALE", (
                f"{check['id']} current execution proof changed: "
                + ", ".join(changed)
            ), evidence
        if cache.select(expected_identity, timeout=check["timeout_s"]) is None:
            return "QUALITY_EVIDENCE_UNKNOWN", (
                f"{check['id']} receipt, result, or log is missing, failed later, or changed"
            ), evidence
    return None


def evaluate_required_quality(project, state, fp, *, env=None):
    """Read-only ready gate for configured release quality checks.

    ``fp`` must be a current measurement made by the runtime immediately
    around this evaluation. The environment defaults to this process and can
    be supplied explicitly when the runtime already normalized a build env.
    """
    if state.get('inflight'):
        return [_quality_gate('QUALITY_EXECUTION_UNKNOWN',
            'reconcile the original inflight result before evaluating delivery', state)]
    control_root = Path(project.root).resolve()
    try:
        stack = load_stack(control_root)
    except WorkflowError as exc:
        return [_quality_gate("QUALITY_CONFIG_INVALID", str(exc), state)]
    if stack["source"] == "missing":
        return []
    checks = [
        check for check in stack["checks"]
        if check["required"] and READY_MODE in check["modes"]
    ]
    if not checks:
        return []
    if not _valid_digest(fp):
        return [_quality_gate(
            "QUALITY_EVIDENCE_UNKNOWN",
            "NOT_CHECKED current source fingerprint is not measurable",
            state,
        )]
    try:
        environment = quality_environment(env)
    except WorkflowError as exc:
        return [_quality_gate("QUALITY_EVIDENCE_UNKNOWN",
                              "NOT_CHECKED " + str(exc), state)]
    try:
        source_root = Path(project.verification_root(state)).resolve()
    except (AttributeError, OSError, WorkflowError) as exc:
        return [_quality_gate("QUALITY_SOURCE_UNAVAILABLE", str(exc), state)]
    contract = quality_contract(project, state)
    if not contract:
        return [_quality_gate(
            "QUALITY_CONTRACT_UNBOUND",
            "the current work contract digest is unavailable",
            state,
        )]
    config_digest = _digest(control_root / stack["source"])
    work_uid = quality_work_uid(state)
    if work_uid is None:
        return [_quality_gate(
            "QUALITY_EVIDENCE_UNKNOWN",
            "NOT_CHECKED current work UID is unavailable",
            state,
        )]
    verification_epoch = (
        state.get("verification_epoch")
        if type(state.get("verification_epoch")) is int else 0
    )
    expected_phase = bound_quality_phase(state)
    locator = QualityReportLocator(control_root)
    row = locator.select_bound(
        work_uid, READY_MODE, expected_phase, verification_epoch, fp,
        contract, config_digest,
    )
    if row is None:
        latest = locator.latest(work_uid, READY_MODE)
        if latest is not None and latest.get("binding_status") != "bound":
            return [_quality_gate(
                "QUALITY_UNBOUND",
                "legacy quality runs cannot satisfy the configured ready gate",
                state,
            )]
        if latest is None:
            return [_quality_gate(
                "QUALITY_REQUIRED",
                "configured release quality checks have not run for the current source",
                state,
            )]
        return [_quality_gate(
            "QUALITY_STALE",
            "no bound release quality run matches the current source, config, and contract",
            state,
        )]
    report_path = (control_root / row["report"]).resolve()
    evidence = [
        str(report_path.relative_to(control_root)).replace("\\", "/")
    ] if inside(control_root, report_path) else []
    try:
        if (not report_path.is_file()
                or not inside(control_root, report_path)
                or _digest(report_path) != row["report_digest"]):
            return [_quality_gate(
                "QUALITY_EVIDENCE_UNKNOWN",
                "located quality report is missing or its digest changed",
                state,
                evidence,
            )]
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return [_quality_gate(
            "QUALITY_EVIDENCE_UNKNOWN",
            "located quality report is unreadable",
            state,
            evidence,
        )]
    verdict = _validate_quality_report(
        report, report_path, control_root, state, work_uid, source_root, fp,
        contract, config_digest, checks, environment,
    )
    if verdict is None:
        return []
    code, detail, report_evidence = verdict
    return [_quality_gate(code, detail, state, report_evidence)]


def run_quality(root: Path, mode: str = "change", task_id: str = "quality",
                run_id: str | None = None, config_path: str | None = None,
                *, control_root: Path | None = None,
                work_uid: str | None = None,
                source_root: Path | None = None,
                source_fingerprint: str | None = None,
                contract=None, phase: str | None = None,
                env=None, verification_epoch=0, on_started=None) -> dict:
    if mode not in MODES:
        raise WorkflowError("QUALITY_MODE_INVALID use change, baseline or release")
    if source_fingerprint is not None and not _valid_digest(source_fingerprint):
        raise WorkflowError("QUALITY_SOURCE_FINGERPRINT_INVALID")
    if phase is not None and not isinstance(phase, str):
        raise WorkflowError("QUALITY_PHASE_INVALID expected text")
    control_root = Path(control_root or root).resolve()
    source_root = Path(source_root or root).resolve()
    work_uid = _run_work_uid(control_root, task_id, work_uid)
    stack = load_stack(control_root, config_path, source_root=source_root)
    run_id = run_id or (datetime_id := time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())) + "-" + uuid.uuid4().hex[:8]
    folder = control_root / ".gsp" / "runs" / task_id / "quality" / run_id
    if not inside(control_root, folder):
        raise WorkflowError("QUALITY_OUTPUT_ESCAPE")
    evidence = folder / "evidence"
    folder.mkdir(parents=True, exist_ok=False)
    selected = [check for check in stack["checks"] if mode in check["modes"]]
    rows = []
    evidence_cache = EvidenceCache(control_root)
    environment = quality_environment(env)
    configuration = (_digest(control_root / stack["source"])
                     if stack["source"] != "missing" else "missing")
    phase = phase or quality_phase(work_uid, mode, verification_epoch)
    contract_digest = _contract_digest(contract)
    bound = bool(
        _valid_digest(source_fingerprint)
        and contract_digest
        and isinstance(phase, str)
        and phase
    )
    binding = {
        "schema": 1,
        "status": "bound" if bound else "legacy_unbound",
        "control_root": str(control_root),
        "source_root": str(source_root),
        "work_uid": work_uid,
        "source_fingerprint": (
            source_fingerprint if _valid_digest(source_fingerprint) else None
        ),
        "contract": contract_digest,
        "phase": phase,
        "verification_epoch": verification_epoch,
        "configuration_digest": configuration,
        "environment_digest": _object_digest(environment),
    }
    commands_file = folder / "commands.jsonl"
    evidence_persisted = True
    with commands_file.open("w", encoding="utf-8", newline="\n") as commands:
        for check in selected:
            check_folder = evidence / check["id"]
            before_inputs = _identity_inputs(
                source_root, check, source_fingerprint, bound,
            )
            identity = quality_identity(
                check,
                source_root=source_root,
                source_fingerprint=source_fingerprint if bound else None,
                contract=contract_digest if bound else configuration,
                phase=phase,
                environment=environment,
                configuration_digest=configuration,
            )
            cached = (evidence_cache.select(identity, timeout=check['timeout_s'])
                      if check['reuse_evidence'] else None)
            execution = cached['execution'] if cached else execute_command(
                check["argv"],
                source_root / check["cwd"],
                environment,
                check["timeout_s"],
                check_folder,
                {
                    "task": task_id,
                    "quality_run": run_id,
                    "quality_mode": mode,
                    "check_id": check["id"],
                    "argv": check["argv"],
                },
                on_started=on_started,
            )
            exit_code = execution.get("returncode")
            status, error = _execution_outcome(execution, check)
            if status == 'pass' and not execution.get('folder'):
                status, error = 'not_checked', 'execution result reference unknown'
            after_inputs = _identity_inputs(
                source_root, check, source_fingerprint, bound,
            )
            if identity is not None and before_inputs != after_inputs:
                status, error = 'fail', 'QUALITY_INPUTS_CHANGED during execution'
            stdout_path = Path(execution["stdout_path"])
            stderr_path = Path(execution["stderr_path"])
            row = {"id": check["id"], "label": check["label"], "kind": check["kind"],
                   "argv": [redact(arg) for arg in check["argv"]], "cwd": check["cwd"],
                   "required": check["required"], "status": status, "exit": exit_code,
                   "error": error, "duration_s": 0.0 if cached else execution.get("duration_s"),
                   "source_duration_s": execution.get('duration_s'), "reused": bool(cached),
                   "execution_identity": identity,
                   "binding_status": binding["status"],
                   "source_fingerprint": binding["source_fingerprint"],
                   "configuration_digest": configuration,
                   "contract": contract_digest,
                   "phase": phase,
                   "verification_epoch": verification_epoch,
                   "execution_result": (str(Path(execution['folder']) / 'command-result.json')
                                        if execution.get('folder') else None),
                   "stdout": str(stdout_path.relative_to(control_root)).replace("\\", "/"),
                   "stderr": str(stderr_path.relative_to(control_root)).replace("\\", "/")}
            row["logs"] = {"stdout": _digest(stdout_path), "stderr": _digest(stderr_path)}
            atomic(check_folder / "receipt.json", row)
            if not cached and identity is not None:
                published = evidence_cache.publish(identity, check_folder / 'receipt.json')
                evidence_persisted = evidence_persisted and published
            commands.write(json.dumps(row, ensure_ascii=False) + "\n")
            rows.append(row)
    if not selected:
        status = "not_checked"
    elif any(row["status"] == "fail" and row["required"] for row in rows):
        status = "fail"
    elif any(row["status"] == "not_checked" and row["required"] for row in rows):
        status = "blocked"
    elif any(row["status"] in {"fail", "not_checked"} for row in rows):
        status = "warn"
    else:
        status = "pass"
    route = _route(mode, status)
    result = {"schema": 1, "quality_run_id": run_id, "task_id": task_id,
              "work_uid": work_uid, "mode": mode,
              "at": now(), "project": str(source_root), "control_root": str(control_root),
              "binding": binding, "binding_status": binding["status"],
              "source_fingerprint": binding["source_fingerprint"],
              "contract": contract_digest, "phase": phase,
              "verification_epoch": verification_epoch,
              "config_digest": configuration, "config": stack["source"],
              "evidence_persisted": evidence_persisted,
              "status": status, "checks": rows, "suggestions": stack["suggestions"], "route": route,
              "summary": {"selected": len(rows), "pass": sum(row["status"] == "pass" for row in rows),
                          "executed": sum(not row['reused'] for row in rows),
                          "reused": sum(row['reused'] for row in rows),
                          "fail": sum(row["status"] == "fail" for row in rows),
                          "not_checked": sum(row["status"] == "not_checked" for row in rows)}}
    atomic(folder / "report.json", result)
    lines = [f"# 项目代码质量报告 — {run_id}", "", f"- 模式：`{mode}`", f"- 状态：`{status.upper()}`",
             f"- 配置：`{stack['source']}`", "", "## 检查结果", "", "| 检查 | 类型 | 状态 | 退出码 |", "| --- | --- | --- | --- |"]
    if rows:
        lines.extend(f"| `{row['label']}` | `{row['kind']}` | `{row['status'].upper()}` | `{row['exit'] if row['exit'] is not None else '—'}` |" for row in rows)
    else:
        lines.append("| 没有声明式检查 | — | NOT_CHECKED | — |")
    lines.extend(["", "## 路由", "", f"- 下一技能：`{route['next_skill']}`", f"- 阻塞：`{route['blocking']}`"])
    if stack["suggestions"]:
        lines.extend(["", "## 仅供配置的候选检查", "", "以下命令只由文件特征推断，未自动执行；确认后写入 `.gsp/quality.json`："])
        lines.extend(f"- `{item['label']}`：`{' '.join(item['argv'])}`" for item in stack["suggestions"])
    atomic(folder / "report.md", "\n".join(lines) + "\n")
    result["report_json"] = str((folder / "report.json").relative_to(control_root)).replace("\\", "/")
    result["report_markdown"] = str((folder / "report.md").relative_to(control_root)).replace("\\", "/")
    result["locator_published"] = QualityReportLocator(control_root).publish(
        folder / "report.json",
        work_uid,
        result["mode"],
        result["phase"],
        result["verification_epoch"],
        result["binding_status"],
        result["source_fingerprint"],
        result["contract"],
        result["config_digest"],
    )
    return result
