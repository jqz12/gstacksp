"""Task-scoped acceptance receipts and delivery summaries."""
import json
from pathlib import Path
import uuid
from typing import Any

from .acceptance import validate_acceptance_card
from .runtime import atomic, now, redact


def record_acceptance_card(root: Path, run_id: str, task_id: str, card: dict[str, Any]) -> str:
    validate_acceptance_card(card)
    root = Path(root).resolve()
    ident = uuid.uuid4().hex
    folder = root / ".gsp" / "runs" / run_id / "acceptance" / task_id / ident
    receipt = {
        "schema": 1,
        "id": ident,
        "task": task_id,
        "card": card["id"],
        "kind": card["kind"],
        "required": card["required"],
        "status": card["status"],
        "steps": [redact(step) for step in card.get("steps", [])],
        "expected": redact(card.get("expected", "")),
        "observed": redact(card.get("observed", "")),
        "evidence": [redact(path) for path in card.get("evidence", [])],
        "at": now(),
    }
    if card.get("not_checked_reason") is not None:
        receipt["not_checked_reason"] = redact(card["not_checked_reason"])
    if card.get("next_action") is not None:
        receipt["next_action"] = redact(card["next_action"])
    target = folder / "receipt.json"
    atomic(target, receipt)
    return target.relative_to(root).as_posix()


def build_acceptance_summary(receipts: list[dict[str, Any]], *, required: set[tuple[str, str]]) -> dict[str, Any]:
    task_rows = {(row.get("task"), row.get("card")): row for row in receipts if row.get("task") != "all"}
    missing = sorted(required - set(task_rows))
    unknown = sorted(f"{task}:{card}" for (task, card), row in task_rows.items()
                     if row.get("status") not in {"PASS", "FAIL", "NOT_CHECKED", "BLOCKED"})
    failed = sorted(f"{task}:{card}" for (task, card), row in task_rows.items() if row.get("status") in {"FAIL", "BLOCKED"})
    not_checked = sorted(f"{task}:{card}" for (task, card), row in task_rows.items() if row.get("status") == "NOT_CHECKED")
    shared = sorted(row.get("card") for row in receipts if row.get("task") == "all")
    return {
        "status": "verified" if not missing and not failed and not not_checked and not unknown else "pending",
        "missing": missing,
        "failed": failed,
        "not_checked": not_checked,
        "unknown": unknown,
        "shared_regression": shared,
    }
