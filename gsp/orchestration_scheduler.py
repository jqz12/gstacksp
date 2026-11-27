"""Pure flat scheduling and usage aggregation; callers own persistence."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping

from .task_contracts import canonical_write_path


SCHEMA = 1
MAX_SLOTS = 3
MODEL_ALIAS = "group/deepseek-2"
EFFORT = "max"
ROLES = frozenset({"worker", "reviewer"})
NO_REDISPATCH = frozenset({"accepted", "running", "unknown"})
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens")
FLAT_SCOPES = frozenset({"flat", "response", "leaf", "descendant-free"})
INCLUSIVE_SCOPES = frozenset(
    {"parent", "tree", "inclusive", "includes-descendants"}
)

def _text(value):
    return isinstance(value, str) and bool(value.strip())

def _strings(value, code):
    if value is None:
        return ()
    if not isinstance(value, (list, tuple, set, frozenset)) or any(
        not _text(item) for item in value
    ):
        raise ValueError(code)
    return tuple(value)

def _writes(value):
    if not isinstance(value, (list, tuple)) or not value:
        return (), False
    try:
        paths = tuple(sorted({
            canonical_write_path(item).casefold() for item in value
        }))
    except ValueError:
        return (), False
    # Glob tails cannot be compared safely with concrete paths.
    return ((), False) if any("*" in p or "?" in p or "[" in p for p in paths) \
        else (paths, True)

def _overlap(left, right):
    return (
        left == right
        or left.startswith(right + "/")
        or right.startswith(left + "/")
    )

def _candidate(raw, index):
    """Return a normalized row or a stable skip record."""
    if not isinstance(raw, Mapping):
        return None, {"index": index, "reason": "candidate-invalid"}
    work, task = raw.get("work_uid"), raw.get("task_id")
    if not _text(work) or not _text(task):
        return None, {"index": index, "reason": "identity-invalid"}
    base = {"work_uid": work, "task_id": task}
    status = raw.get("status")
    if status in NO_REDISPATCH:
        return None, base | {"reason": "status-" + str(status)}
    if status != "ready":
        return None, base | {"reason": "not-ready"}
    try:
        depends = set(_strings(raw.get("depends"), "SCHEDULER_DEPENDS_INVALID"))
        accepted = set(
            _strings(raw.get("accepted_task_ids"), "SCHEDULER_ACCEPTED_INVALID")
        )
    except ValueError as exc:
        return None, base | {"reason": str(exc)}
    if task in accepted:
        return None, base | {"reason": "already-accepted"}
    missing = sorted(depends - accepted)
    if missing:
        return None, base | {
            "reason": "dependency-not-accepted", "missing": missing
        }
    if raw.get("role") not in ROLES:
        return None, base | {"reason": "role-invalid"}
    if raw.get("model_alias") != MODEL_ALIAS or raw.get("effort") != EFFORT:
        return None, base | {"reason": "model-route-invalid"}
    if not _text(raw.get("contract_digest")):
        return None, base | {"reason": "contract-invalid"}
    paths, known = _writes(raw.get("writes"))
    return {
        "index": index, "work_uid": work, "task_id": task,
        "contract_digest": raw["contract_digest"], "writes": list(paths),
        "role": raw["role"], "model_alias": raw["model_alias"],
        "effort": raw["effort"], "known_writes": known,
    }, None

def next_batch(candidates, *, limit=3, cursor=None, occupied=0):
    """Return a fair, conflict-free batch from caller-owned candidate facts."""
    if (
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        or isinstance(occupied, bool) or not isinstance(occupied, int)
        or occupied < 0
    ):
        raise ValueError("SCHEDULER_LIMIT_INVALID")
    slots = max(0, min(limit, MAX_SLOTS) - occupied)
    previous = None
    if cursor is not None:
        if not isinstance(cursor, Mapping) or (
            cursor.get("work_uid") is not None
            and not _text(cursor.get("work_uid"))
        ):
            raise ValueError("SCHEDULER_CURSOR_INVALID")
        previous = cursor.get("work_uid")
    if not slots:
        return {
            "schema": SCHEMA, "batch": [], "cursor": cursor,
            "remaining_slots": 0, "mode": "flat", "skipped": [],
            "persisted": False,
        }

    ready, skipped, seen = [], [], set()
    for index, raw in enumerate(candidates):
        identity = (
            (raw.get("work_uid"), raw.get("task_id"))
            if isinstance(raw, Mapping) else None
        )
        if identity and all(map(_text, identity)) and identity in seen:
            skipped.append({
                "work_uid": identity[0], "task_id": identity[1],
                "reason": "duplicate-candidate",
            })
            continue
        row, reason = _candidate(raw, index)
        if identity and all(map(_text, identity)):
            seen.add(identity)
        (ready if row else skipped).append(row if row else reason)

    by_work = defaultdict(list)
    for row in ready:
        by_work[row["work_uid"]].append(row)
    for rows in by_work.values():
        rows.sort(key=lambda row: (row["task_id"], row["index"]))
    works = sorted(by_work)
    if works and previous in works:
        offset = works.index(previous) + 1
        works = works[offset:] + works[:offset]

    selected, legacy = [], False
    while len(selected) < slots:
        progressed = False
        for work in works:
            queue = by_work[work]
            while queue:
                row = queue.pop(0)
                if legacy or (not row["known_writes"] and selected):
                    continue
                if row["known_writes"] and any(
                    any(_overlap(a, b) for a in row["writes"] for b in item["writes"])
                    for item in selected if item["known_writes"]
                ):
                    continue
                selected.append(row)
                legacy = not row["known_writes"]
                progressed = True
                break
            if legacy or len(selected) >= slots:
                break
        if not progressed or legacy or len(selected) >= slots:
            break

    batch = [
        {
            key: value for key, value in row.items()
            if key not in {"index", "known_writes"}
        } | {"write_scope": "known" if row["known_writes"] else "unknown"}
        for row in selected
    ]
    out_cursor = cursor
    if batch:
        out_cursor = {
            "schema": SCHEMA, "work_uid": batch[-1]["work_uid"],
            "task_id": batch[-1]["task_id"],
        }
    return {
        "schema": SCHEMA, "batch": batch, "cursor": out_cursor,
        "remaining_slots": slots - len(batch),
        "mode": "legacy-sequential" if legacy else "flat",
        "skipped": skipped, "persisted": False,
    }

def _scope(value):
    if not isinstance(value, Mapping):
        return None, False
    raw = value.get("scope", value.get("usage_scope", value.get("event_scope")))
    scope = raw.casefold() if isinstance(raw, str) else None
    inclusive = (
        scope in INCLUSIVE_SCOPES
        or value.get("includes_descendants") is True
        or value.get("contains_children") is True
        or bool(value.get("descendant_ids") or value.get("child_ids"))
    )
    return scope, inclusive

def _identity(row, index):
    for name in ("response_id", "event_id", "id"):
        if _text(row.get(name)):
            return name + ":" + row[name], True
    return "anonymous:" + str(index), False

def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0

def aggregate_usage(events):
    """Aggregate only explicitly flat usage; preserve unknown facts as None."""
    if isinstance(events, Mapping):
        rows, envelope = events.get("events"), events
    else:
        rows, envelope = events, {}
    rows = [] if rows is None else rows
    if not isinstance(rows, (list, tuple)):
        raise ValueError("USAGE_EVENTS_INVALID")
    default_scope, inclusive = _scope(envelope)
    warnings = set()
    if inclusive:
        warnings.add("usage-parent-includes-descendants")
    elif default_scope is not None and default_scope not in FLAT_SCOPES:
        warnings.add(
            "usage-parent-includes-descendants"
            if default_scope in INCLUSIVE_SCOPES else "usage-scope-unknown"
        )

    groups, anonymous = {}, 0
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            warnings.add("usage-event-invalid")
            continue
        scope, row_inclusive = _scope(row)
        scope = scope or default_scope
        if row_inclusive:
            warnings.add("usage-parent-includes-descendants")
        if scope not in FLAT_SCOPES:
            warnings.add("usage-scope-unknown")
        key, identified = _identity(row, index)
        anonymous += int(not identified)
        source = row.get("usage") if isinstance(row.get("usage"), Mapping) else row
        usage = tuple(source.get(name, source.get("cached_input") if name == "cached_input_tokens" else None)
                      for name in TOKEN_FIELDS)
        if key in groups and groups[key] != usage:
            warnings.add("usage-same-id-changed")
        else:
            groups.setdefault(key, usage)
    if anonymous > 1:
        warnings.add("usage-anonymous-events")
    blocking = warnings & {
        "usage-parent-includes-descendants", "usage-scope-unknown",
        "usage-same-id-changed", "usage-anonymous-events",
        "usage-event-invalid",
    }
    unknown = bool(blocking) or not groups
    result = {
        "schema": SCHEMA, "usage_scope": "unknown" if unknown else "flat",
        "unique_responses": None if unknown else len(groups),
        "warnings": [], "remaining_budget": None, "budget_scope": "unknown",
    }
    for position, name in enumerate(TOKEN_FIELDS):
        values = [group[position] for group in groups.values()]
        result[name] = (
            None if unknown or not all(map(_count, values)) else sum(values)
        )
    result["total_tokens"] = (
        result["input_tokens"] + result["output_tokens"]
        if result["input_tokens"] is not None
        and result["output_tokens"] is not None else None
    )

    budgets = []
    direct = envelope.get("native_remaining_budget")
    if _count(direct):
        budgets.append(direct)
    elif envelope.get("remaining_budget") is not None:
        if envelope.get("budget_scope") == "native" and _count(
            envelope["remaining_budget"]
        ):
            budgets.append(envelope["remaining_budget"])
        else:
            warnings.add("usage-budget-scope-unknown")
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        value = row.get("native_remaining_budget")
        if _count(value):
            budgets.append(value)
        elif value is not None:
            warnings.add("usage-budget-invalid")
    if len(set(budgets)) == 1:
        result["remaining_budget"] = budgets[0]
        result["budget_scope"] = "native"
    elif len(set(budgets)) > 1:
        warnings.add("usage-budget-conflict")
    result["warnings"] = sorted(warnings)
    return result

__all__ = ["aggregate_usage", "next_batch"]
