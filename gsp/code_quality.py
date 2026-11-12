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
import subprocess
import time
import uuid

from .runtime import WorkflowError, atomic, inside, now, redact

MODES = {"change", "baseline", "release"}
KINDS = {"build", "format", "lint", "test", "security", "maintainability", "contract", "runtime"}
MAX_CONFIG_BYTES = 512 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
DEFAULT_TIMEOUT = 900


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bounded(value: str) -> str:
    value = redact(value or "")
    if len(value.encode("utf-8")) <= MAX_OUTPUT_BYTES:
        return value
    raw = value.encode("utf-8")[:MAX_OUTPUT_BYTES]
    return raw.decode("utf-8", errors="ignore") + "\n[TRUNCATED]"


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


def load_stack(root: Path, config_path: str | None = None) -> dict:
    relative_config = config_path or ".gsp/quality.json"
    if Path(relative_config).is_absolute():
        raise WorkflowError("QUALITY_CONFIG_ESCAPE configuration must be project-relative")
    path = (root / relative_config).resolve()
    if not inside(root, path):
        raise WorkflowError("QUALITY_CONFIG_ESCAPE configuration must remain inside the project")
    if not path.exists():
        return {"schema": 1, "source": "missing", "path": str(path.relative_to(root)).replace("\\", "/"),
                "checks": [], "suggestions": _suggestions(root)}
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
        candidate = (root / cwd).resolve()
        if not isinstance(cwd, str) or Path(cwd).is_absolute() or not inside(root, candidate) or not candidate.is_dir():
            raise WorkflowError(f"QUALITY_CHECK_CWD_INVALID {ident}")
        timeout = item.get("timeout_s", DEFAULT_TIMEOUT)
        if not isinstance(timeout, int) or timeout < 1 or timeout > 7200:
            raise WorkflowError(f"QUALITY_CHECK_TIMEOUT_INVALID {ident}")
        seen.add(ident)
        checks.append({
            "id": ident, "label": item.get("label", ident), "kind": kind, "argv": argv,
            "cwd": cwd.replace("\\", "/"), "modes": modes,
            "required": bool(item.get("required", True)), "timeout_s": timeout,
        })
    return {"schema": 1, "source": str(path.relative_to(root)).replace("\\", "/"),
            "path": str(path.relative_to(root)).replace("\\", "/"), "checks": checks,
            "suggestions": _suggestions(root)}


def _route(mode: str, status: str) -> dict:
    if status in {"fail", "blocked"}:
        return {"next_skill": "gsp-execute", "return_to": "gsp-code-quality", "blocking": True}
    if status == "not_checked":
        return {"next_skill": "gsp-workflow", "return_to": "gsp-code-quality", "blocking": True}
    if mode == "release":
        return {"next_skill": "gsp-verify", "return_to": "gsp-code-quality", "blocking": False}
    if mode == "baseline":
        return {"next_skill": "gsp-workflow", "return_to": "gsp-code-quality", "blocking": False,
                "user_decision_required": True, "decision_kind": "quality-baseline"}
    return {"next_skill": "gsp-review", "return_to": "gsp-code-quality", "blocking": False}


def run_quality(root: Path, mode: str = "change", task_id: str = "quality", run_id: str | None = None,
                config_path: str | None = None) -> dict:
    if mode not in MODES:
        raise WorkflowError("QUALITY_MODE_INVALID use change, baseline or release")
    stack = load_stack(root, config_path)
    run_id = run_id or (datetime_id := time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())) + "-" + uuid.uuid4().hex[:8]
    folder = root / ".gsp" / "runs" / task_id / "quality" / run_id
    if not inside(root, folder):
        raise WorkflowError("QUALITY_OUTPUT_ESCAPE")
    evidence = folder / "evidence"
    folder.mkdir(parents=True, exist_ok=False)
    selected = [check for check in stack["checks"] if mode in check["modes"]]
    rows = []
    commands_file = folder / "commands.jsonl"
    with commands_file.open("w", encoding="utf-8", newline="\n") as commands:
        for check in selected:
            started = time.monotonic()
            check_folder = evidence / check["id"]
            check_folder.mkdir(parents=True, exist_ok=False)
            stdout = stderr = ""
            exit_code = None
            status = "pass"
            error = None
            try:
                completed = subprocess.run(
                    check["argv"], cwd=root / check["cwd"], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=check["timeout_s"],
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                exit_code = completed.returncode
                stdout, stderr = _bounded(completed.stdout), _bounded(completed.stderr)
                if completed.returncode != 0:
                    status = "fail"
            except FileNotFoundError as exc:
                status, error = "not_checked", f"tool not found: {exc.filename}"
            except subprocess.TimeoutExpired as exc:
                status, error = "fail", f"timeout after {check['timeout_s']}s"
                stdout, stderr = _bounded(exc.stdout or ""), _bounded(exc.stderr or "")
            except OSError as exc:
                status, error = "not_checked", f"could not start: {exc}"
            atomic(check_folder / "stdout.txt", stdout)
            atomic(check_folder / "stderr.txt", stderr)
            row = {"id": check["id"], "label": check["label"], "kind": check["kind"],
                   "argv": [redact(arg) for arg in check["argv"]], "cwd": check["cwd"],
                   "required": check["required"], "status": status, "exit": exit_code,
                   "error": error, "duration_s": round(time.monotonic() - started, 3),
                   "stdout": str((check_folder / "stdout.txt").relative_to(root)).replace("\\", "/"),
                   "stderr": str((check_folder / "stderr.txt").relative_to(root)).replace("\\", "/")}
            row["logs"] = {"stdout": _digest(check_folder / "stdout.txt"), "stderr": _digest(check_folder / "stderr.txt")}
            atomic(check_folder / "receipt.json", row)
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
    result = {"schema": 1, "quality_run_id": run_id, "task_id": task_id, "mode": mode,
              "at": now(), "project": str(root), "config": stack["source"],
              "status": status, "checks": rows, "suggestions": stack["suggestions"], "route": route,
              "summary": {"selected": len(rows), "pass": sum(row["status"] == "pass" for row in rows),
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
    result["report_json"] = str((folder / "report.json").relative_to(root)).replace("\\", "/")
    result["report_markdown"] = str((folder / "report.md").relative_to(root)).replace("\\", "/")
    return result
