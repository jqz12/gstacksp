"""Pure convergence policies for slices, review findings and stop points.

No function performs I/O; callers persist these decisions in the existing
task, contract, issue and execution state.
"""

from datetime import datetime, timezone
import re
from typing import Any, Mapping

from .orchestration_types import IssueRef, digest_value

_CRITICAL = frozenset({"introduced_regression", "regression", "permission_bypass",
    "authorization_bypass", "data_corruption", "current_prerequisite_failure",
    "required_check_failure"})
_SEQ = (list, tuple, set)

def _error(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}

def _result(status: str, *, structure: bool = False, complete: bool = False,
            requirement: str | None = None, errors: list | None = None,
            **extra: Any) -> dict[str, Any]:
    result = {"status": status, "structure_ok": structure, "complete": complete,
              "errors": errors or []}
    if requirement is not None: result["required_reason"] = requirement
    return result | extra

def _slice_requirement(task: Mapping[str, Any]) -> str | None:
    if task.get("requires_slices") is True: return "requires_slices"
    for key in ("multiple_independent_outcomes", "independent_outcomes"):
        if isinstance(task.get(key), _SEQ) and len(task[key]) > 1: return key
    boundaries = task.get("integration_boundaries")
    if boundaries is True or (isinstance(boundaries, _SEQ) and len(boundaries) > 1):
        return "integration_boundaries"
    required = str(task.get("slice_requirement") or "").lower() == "required"
    return "slice_requirement" if required else None


def _single_outcome_declared(task: Mapping[str, Any]) -> bool:
    outcome = task.get("single_outcome")
    if isinstance(outcome, str) and outcome.strip():
        return True
    if isinstance(outcome, (list, tuple)) and len(outcome) == 1:
        return isinstance(outcome[0], str) and bool(outcome[0].strip())
    return (
        task.get("small") is True
        or str(task.get("execution_mode") or "").strip().lower() == "small"
    )


def _topological_order(ids: list[str],
                       deps: dict[str, list[str]]) -> list[str] | None:
    visiting, visited, order = set(), set(), []
    def visit(node: str) -> bool:
        if node in visited: return True
        if node in visiting: return False
        visiting.add(node)
        if any(dep in deps and not visit(dep) for dep in deps.get(node, [])): return False
        visiting.remove(node); visited.add(node); order.append(node)
        return True
    return order if all(visit(node) for node in ids) else None

def validate_task_slices(task: Mapping[str, Any]) -> dict[str, Any]:
    """Validate explicit slices; unknown legacy work remains in triage."""
    if not isinstance(task, Mapping):
        return _result("invalid", errors=[_error("task_invalid", "task must be a mapping")])
    required = _slice_requirement(task)
    if "slices" not in task:
        if required: return _result("needs_slices", requirement=required,
                                    errors=[_error("slices_required", required)])
        explicit_none = (task.get("requires_slices") is False or
                         str(task.get("slice_requirement") or "").lower() == "not_required")
        if explicit_none and not _single_outcome_declared(task):
            return _result(
                "invalid",
                errors=[_error(
                    "single_outcome_required",
                    "requires_slices=false needs single_outcome or small execution mode",
                )],
            )
        return _result("not_required" if explicit_none else "needs_triage",
                       structure=explicit_none)
    raw = task.get("slices")
    if not isinstance(raw, (list, tuple)) or not raw:
        return _result("invalid", requirement=required,
                       errors=[_error("slices_empty", "slices must be a non-empty list")])
    if required and len(raw) < 2:
        return _result("invalid", requirement=required,
                       errors=[_error("slice_split_required", required)])
    errors: list[dict[str, str]] = []; ids: list[str] = []
    deps: dict[str, list[str]] = {}; accepted: dict[str, bool] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            errors.append(_error("slice_invalid", str(index))); continue
        slice_id = item.get("id")
        if not isinstance(slice_id, str) or not slice_id.strip():
            errors.append(_error("slice_id_required", str(index))); continue
        slice_id = slice_id.strip()
        if slice_id in deps:
            errors.append(_error("duplicate_slice_id", slice_id)); continue
        outcome, checks, raw_deps = item.get("outcome"), item.get("checks"), item.get("deps", [])
        if not isinstance(outcome, str) or not outcome.strip():
            errors.append(_error("slice_outcome_required", slice_id))
        if (not isinstance(checks, (list, tuple)) or not checks or
                any(not isinstance(check, str) or not check.strip() for check in checks)):
            errors.append(_error("slice_checks_required", slice_id))
        deps_valid = (isinstance(raw_deps, (list, tuple)) and
                      all(isinstance(dep, str) and dep.strip() for dep in raw_deps) and
                      len(set(raw_deps)) == len(raw_deps))
        if not deps_valid:
            errors.append(_error("slice_deps_invalid", slice_id))
        ids.append(slice_id)
        deps[slice_id] = [dep.strip() for dep in raw_deps] if deps_valid else []
        accepted[slice_id] = (item.get("status") == "accepted" or
                              item.get("accepted") is True)
    errors += [_error("unknown_slice_dependency", f"{sid}:{dep}")
               for sid, values in deps.items() for dep in values if dep not in deps]
    order = _topological_order(ids, deps)
    if order is None:
        errors.append(_error("slice_dependency_cycle", "slice dependencies must be acyclic"))
    if errors:
        return _result("invalid", requirement=required, errors=errors)
    return _result("ready", structure=True, complete=all(accepted.values()),
                   requirement=required, order=order, ordered_slice_ids=order)

def _kind(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _candidate_digest(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else None


def _issue(finding: Mapping[str, Any], contract_digest: str, owner: str) -> IssueRef:
    existing = finding.get("issue_ref") or finding.get("issue")
    if isinstance(existing, IssueRef): return existing
    existing = existing if isinstance(existing, Mapping) else {}
    acceptance = str(finding.get("acceptance_key") or finding.get("affected_acceptance")
                     or contract_digest)
    root = str(finding.get("root_cause_key") or finding.get("kind") or "unclassified")
    signature = str(finding.get("signature") or finding.get("trigger")
                    or finding.get("evidence") or "missing-signature")
    count = finding.get("repair_count", existing.get("repair_count", 0))
    count = count if isinstance(count, int) and not isinstance(count, bool) else 0
    identity = {"work_uid": str(finding.get("work_uid") or existing.get("work_uid") or ""),
                "task_uid": str(finding.get("task_uid") or existing.get("task_uid") or ""),
                "acceptance_key": acceptance, "root_cause_key": root, "signature": signature}
    uid = str(existing.get("uid") or finding.get("issue_uid") or digest_value(identity))
    return IssueRef(uid, identity["work_uid"], identity["task_uid"], acceptance,
                    root, signature, count, owner, "open")

def _investigate(reason: str | None = None) -> dict[str, Any]:
    result = {"status": "needs_investigation", "classification": None,
              "accepted": False, "blocking_current_acceptance": True}
    return result | ({"reason": reason} if reason else {})

def classify_review_finding(finding: Mapping[str, Any],
                            current_contract_digest: str) -> dict[str, Any]:
    """Classify one confirmed finding against the frozen review candidate."""
    if not isinstance(finding, Mapping): return _investigate()
    confirmed = finding.get("confirmed") is True or finding.get("confidence") == "confirmed"
    if not confirmed:
        return _investigate("unknown findings cannot be accepted")
    kind = _kind(finding.get("kind"))
    reviewed = _candidate_digest(finding.get("reviewed_candidate_digest"))
    current = _candidate_digest(finding.get("current_candidate_digest"))
    if reviewed is None or current is None:
        return _investigate(
            "reviewed and current candidate digests are required for a formal finding"
        )
    drift = reviewed != current
    critical = kind in _CRITICAL or drift or finding.get("introduced") is True
    contract = finding.get("contract_digest") or finding.get("acceptance_contract_digest")
    in_current = bool(finding.get("affects_current_acceptance") is True or
                      contract == current_contract_digest)
    if critical or in_current:
        classification, accepted = "blocking_current_acceptance", False
        reason = ("candidate drift" if drift else "introduced or unsafe defect" if critical
                  else "finding belongs to current acceptance")
    elif finding.get("independent_of_current_acceptance") is True:
        classification, reason = "correctness_defect", "confirmed independent defect"
        accepted = bool(finding.get("follow_up_owner") or finding.get("owner"))
    elif finding.get("non_blocking_improvement") is True:
        classification, accepted = "non_blocking_improvement", True
        reason = "record once outside the review loop"
    else:
        return _investigate("current-acceptance relationship is unknown")
    existing = finding.get('issue_ref') or finding.get('issue') or {}
    existing_count = (existing.repair_count if isinstance(existing, IssueRef) else
                      existing.get('repair_count', 0) if isinstance(existing, Mapping) else 0)
    reported_count = finding.get('repair_count', 0)
    count = max(value if type(value) is int and value >= 0 else 0
                for value in (reported_count, existing_count))
    count = count if isinstance(count, int) and not isinstance(count, bool) else 0
    owner = str(finding.get("follow_up_owner") or finding.get("owner") or "")
    if count >= 1 and classification != "non_blocking_improvement":
        owner = "sol"
    elif classification == "blocking_current_acceptance": owner = owner or "worker"
    issue = _issue(finding, current_contract_digest, owner)
    return {"status": "classified", "classification": classification,
            "disposition": classification,
            "blocking_current_acceptance": classification == "blocking_current_acceptance",
            "accepted": accepted,
            "owner_required": classification == "correctness_defect" and not owner,
            "requires_sol_takeover": owner == "sol", "issue_ref": issue,
            "trigger": str(finding.get("trigger") or ""),
            "affected_acceptance": str(finding.get("affected_acceptance") or ""),
            "severity": str(finding.get("severity") or ""), "owner": owner,
            "reason": reason}

def _time(value: Any) -> datetime:
    if isinstance(value, datetime): parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        raise ValueError("timestamp required")
    if parsed.tzinfo is None: raise ValueError("timezone required")
    return parsed.astimezone(timezone.utc)

def checkpoint_decision(task: Mapping[str, Any],
                        now: datetime | str) -> dict[str, Any]:
    """Return the 45/90-minute decision without touching runs or leases."""
    if not isinstance(task, Mapping):
        return {"status": "needs_triage", "checkpoint_due": False,
                "sol_decision_required": False, "reason": "task must be a mapping",
                "preserve_leases": True}
    try:
        current = _time(now); start = _time(task.get("original_started_at") or task.get("started_at"))
        accepted = [(_time(item["accepted_at"]), str(item.get("id") or ""))
                    for item in task.get("slices") or []
                    if isinstance(item, Mapping) and item.get("accepted_at") and
                    (item.get("status") == "accepted" or item.get("accepted") is True)]
        if task.get("last_accepted_at"):
            accepted.append((_time(task["last_accepted_at"]), "recorded"))
        accepted.sort(key=lambda row: row[0])
        accepted_at = accepted[-1][0] if accepted else None
        checkpoint_at = _time(task.get("last_checkpoint_at") or start)
        if accepted_at is not None and not start <= accepted_at <= current:
            raise ValueError("accepted slice is outside active timeline")
        if start > current:
            raise ValueError('task start is in the future')
        if checkpoint_at < start or checkpoint_at > current:
            raise ValueError("checkpoint anchor is outside active timeline")
    except (TypeError, ValueError) as exc:
        return {"status": "needs_triage", "checkpoint_due": False,
                "sol_decision_required": False, "reason": str(exc),
                "preserve_leases": True}
    since_checkpoint = max(0.0, (current - checkpoint_at).total_seconds() / 60)
    since_accepted = max(0.0, (current - (accepted_at or start)).total_seconds() / 60)
    sol, due = since_accepted >= 90, since_checkpoint >= 45
    return {"status": ("sol_decision_required" if sol else
                       "checkpoint_due" if due else "active"),
            "checkpoint_due": due, "sol_decision_required": sol,
            "anchor": "accepted_slice" if accepted_at else "original_start",
            "original_started_at": start.isoformat(),
            "last_accepted_at": accepted_at.isoformat() if accepted_at else None,
            "minutes_since_checkpoint": round(since_checkpoint, 3),
            "minutes_since_accepted": round(since_accepted, 3),
            "preserve_leases": True,
            "reason": "no accepted slice for at least 90 minutes" if sol else None}

def execution_decision(task: Mapping[str, Any]) -> dict[str, Any]:
    """Choose local work or a DeepSeek executor without creating either yet."""
    if not isinstance(task, Mapping):
        return {"mode": "needs_triage", "reason": "task must be a mapping"}
    estimate, independent = task.get("estimated_minutes"), task.get("independent") is True
    bounded = (isinstance(estimate, (int, float)) and not isinstance(estimate, bool)
               and 0 <= estimate <= 15 and task.get("local_change") is True
               and not independent and not _slice_requirement(task))
    if bounded:
        return {"mode": "local", "reason": "bounded local change of at most 15 minutes",
                "model_alias": "current_host", "effort": "current",
                "independent_review_required": False, "requires_session": False,
                "requires_worktree": False, "helper_depth": 0, "slot_limit": 1}
    complex_task = (independent or (isinstance(estimate, (int, float))
                    and not isinstance(estimate, bool) and estimate > 15)
                    or bool(_slice_requirement(task))
                    or str(task.get("complexity") or "").lower() == "complex")
    if complex_task:
        return {"mode": "deepseek_worker", "reason": "independent or multi-slice work",
                "model_alias": "group/deepseek-2", "effort": "max",
                "independent_review_required": True, "requires_session": True,
                "requires_worktree": bool(task.get("parallel_writable") is True or
                                          task.get("requires_worktree") is True),
                "helper_depth": 0, "slot_limit": 3}
    return {"mode": "needs_triage", "reason": "size or independence is unknown",
            "requires_session": False, "requires_worktree": False}
