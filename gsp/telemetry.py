"""Privacy-preserving local GSP workflow telemetry and reports."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import uuid


SENSITIVE_KEYS = {
    "prompt", "response", "stdout", "stderr", "secret", "password", "token",
    "api_key", "apikey", "authorization", "cookie", "environment", "env",
}
MAX_STRING = 512


def _redact(value, key=""):
    lowered = key.casefold().replace("-", "_")
    if lowered in SENSITIVE_KEYS or any(part in lowered for part in ("password", "secret", "token", "api_key", "authorization", "cookie")):
        return "<REDACTED>"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items() if str(k).casefold() not in SENSITIVE_KEYS}
    if isinstance(value, list):
        return [_redact(v, key) for v in value[:32]]
    if isinstance(value, str):
        text = value[:MAX_STRING]
        text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+", r"\1<REDACTED>", text)
        text = re.sub(r"(?i)((?:api[_-]?key|password|secret|token)\s*[=:]\s*[\"']?)[^\s\"']+", r"\1<REDACTED>", text)
        return text
    return value


def _atomic_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def telemetry_path(root) -> Path:
    return Path(root).resolve() / ".gsp" / "telemetry" / "events.jsonl"


def append_event(root, event: dict) -> dict | None:
    """Append one sanitized event. Telemetry failure is deliberately non-fatal."""
    try:
        path = telemetry_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _redact({
            "schema": 1,
            "event_id": uuid.uuid4().hex,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **event,
        })
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        return payload
    except (OSError, TypeError, ValueError):
        return None


def read_events(root) -> tuple[list[dict], list[str]]:
    path = telemetry_path(root)
    events, malformed = [], []
    if not path.exists():
        return events, malformed
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("event is not an object")
            events.append(value)
        except (ValueError, json.JSONDecodeError):
            malformed.append(str(number))
    return events, malformed


def _report_markdown(report: dict) -> str:
    lines = [
        "# GSP 观测报告", "", f"运行：`{report['run_id']}`", "",
        f"事件数：{report['event_count']}；损坏行：{len(report['malformed_lines'])}", "",
        "## 路由与技能", "",
        "| 技能/事件 | 次数 | unknown | 偏离 | 证据入口 |", "|---|---:|---:|---:|---:|",
    ]
    for row in report["summary"]:
        lines.append(f"| `{row['name']}` | {row['count']} | {row['unknown']} | {row['deviations']} | {row['evidence']} |")
    lines += ["", "## 观测限制", "", "日志只证明 CLI 或 hook 产生了可配对事件。没有原生宿主事件的技能调用记录为 `unknown`；日志不证明方法完整遵循。", "", "## 改进候选", ""]
    if report["improvement_candidates"]:
        lines.extend(f"- {item}" for item in report["improvement_candidates"])
    else:
        lines.append("- 暂无自动生成候选；继续积累实验事件。")
    if report["malformed_lines"]:
        lines += ["", "损坏 JSONL 行：" + ", ".join(report["malformed_lines"])]
    return "\n".join(lines) + "\n"


def build_report(root, run_id: str | None = None) -> dict:
    root = Path(root).resolve()
    events, malformed = read_events(root)
    report_id = run_id or (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    grouped = {}
    deviations = []
    unknown = 0
    for event in events:
        name = event.get("skill") or event.get("event") or "unknown"
        row = grouped.setdefault(name, {"name": name, "count": 0, "unknown": 0, "deviations": 0, "evidence": 0})
        row["count"] += 1
        row["evidence"] += len(event.get("evidence", [])) if isinstance(event.get("evidence", []), list) else 0
        status = event.get("observation_status") or event.get("status") or event.get("observed", {}).get("status")
        if status in ("unknown", "not_checked") or (
            "observation_status" not in event and event.get("observed") in (None, {}, "unknown")
        ):
            row["unknown"] += 1
            unknown += 1
        if event.get("deviation"):
            row["deviations"] += 1
            deviations.append(event["deviation"])
    candidates = []
    if unknown:
        candidates.append(f"有 {unknown} 个事件缺少可配对的宿主证据，优先补充 Codex/Claude 原生事件关联。")
    if deviations:
        candidates.append(f"发现 {len(deviations)} 个路由偏离，按事件 evidence 逐项复核后再修改路由契约。")
    report = {
        "schema": 1,
        "run_id": report_id,
        "project": str(root),
        "event_count": len(events),
        "malformed_lines": malformed,
        "summary": sorted(grouped.values(), key=lambda item: item["name"]),
        "deviations": deviations,
        "improvement_candidates": candidates,
        "limits": ["native host execution is NOT_CHECKED unless a hook/host event is paired"],
    }
    out = root / "docs" / "gsp" / "reports" / "observability" / report_id
    _atomic_text(out / "report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    _atomic_text(out / "report.md", _report_markdown(report))
    return {"report_id": report_id, "report_dir": str(out), **{k: report[k] for k in ("event_count", "malformed_lines", "summary", "improvement_candidates")}}
