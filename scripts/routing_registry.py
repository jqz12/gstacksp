"""Central, machine-checkable GSP route registry.

The registry describes handoffs only. It does not execute skills or grant user
authorization. Unknown observations always have an explicit workflow fallback.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import json


@dataclass(frozen=True)
class Route:
    route_id: str
    source: str
    condition: str
    next_skill: str
    return_to: str
    blocking: bool
    user_decision_required: bool
    decision_kind: str | None
    evidence_kind: str
    unknown_fallback: str


ROUTES = (
    Route("start.no-active-task", "gsp-workflow", "no_active_task", "gsp-brainstorming", "gsp-workflow", False, False, None, "project-state", "gsp-workflow"),
    Route("workflow.unknown", "gsp-workflow", "unknown", "gsp-workflow", "gsp-workflow", True, True, "investigation", "workflow-state", "gsp-workflow"),
    Route("start.active-mismatch", "gsp-workflow", "active_task_mismatch", "gsp-context-restore", "gsp-workflow", True, False, "investigation", "project-state", "gsp-workflow"),
    Route("git.clean.no-task", "gsp-health", "clean_and_no_active_task", "gsp-brainstorming", "gsp-workflow", False, False, None, "git-health", "gsp-workflow"),
    Route("git.clean.active-task", "gsp-health", "clean_and_active_task", "gsp-workflow", "gsp-workflow", False, False, None, "git-health", "gsp-workflow"),
    Route("git.needs-decision", "gsp-health", "dirty_or_untracked_or_detached", "gsp-git", "gsp-workflow", False, True, "worktree", "git-health", "gsp-workflow"),
    Route("git.no-repository", "gsp-health", "repository_missing", "gsp-git", "gsp-workflow", True, True, "repository", "git-health", "gsp-workflow"),
    Route("git.unknown", "gsp-health", "git_probe_unknown", "gsp-git", "gsp-workflow", True, True, "investigation", "git-health", "gsp-workflow"),
    Route("execute.test-failure", "gsp-execute", "test_failure", "gsp-systematic-debugging", "gsp-execute", True, False, None, "test-receipt", "gsp-workflow"),
    Route("quality.failure", "gsp-code-quality", "required_check_failed", "gsp-execute", "gsp-code-quality", True, False, None, "quality-report", "gsp-workflow"),
    Route("quality.not-checked", "gsp-code-quality", "quality_stack_missing_or_tool_unavailable", "gsp-workflow", "gsp-code-quality", True, True, "quality-stack", "quality-report", "gsp-workflow"),
    Route("quality.change-pass", "gsp-code-quality", "change_checks_passed", "gsp-review", "gsp-code-quality", False, False, None, "quality-report", "gsp-workflow"),
    Route("quality.baseline-pass", "gsp-code-quality", "baseline_checks_passed", "gsp-workflow", "gsp-code-quality", False, True, "quality-baseline", "quality-report", "gsp-workflow"),
    Route("quality.release-pass", "gsp-code-quality", "release_checks_passed", "gsp-verify", "gsp-code-quality", False, False, None, "quality-report", "gsp-workflow"),
    Route("review.blocking-finding", "gsp-review", "blocking_finding", "gsp-execute", "gsp-review", True, False, None, "review", "gsp-workflow"),
    Route("verify.stale-evidence", "gsp-verify", "evidence_stale", "gsp-verify", "gsp-workflow", True, False, None, "verification", "gsp-workflow"),
    Route("documents.findings", "gsp-health", "document_findings_with_authorized_governance", "gsp-document-governance", "gsp-workflow", False, False, None, "document-report", "gsp-workflow"),
    Route("documents.readonly", "gsp-health", "document_findings_readonly", "gsp-health", "gsp-workflow", False, True, "governance", "document-report", "gsp-workflow"),
    Route("restore.unknown", "gsp-context-restore", "resume_mismatch_or_unknown", "gsp-context-restore", "gsp-workflow", True, True, "investigation", "resume-state", "gsp-workflow"),
    Route("complete.review", "gsp-execute", "all_tasks_done", "gsp-review", "gsp-workflow", False, False, None, "task-state", "gsp-workflow"),
    Route("review.verify", "gsp-review", "review_passed", "gsp-verify", "gsp-review", False, False, None, "review", "gsp-workflow"),
    Route("verify.ship", "gsp-verify", "evidence_valid", "gsp-ship", "gsp-verify", False, False, None, "verification", "gsp-workflow"),
    Route("ship.ready", "gsp-ship", "delivery_prepared", "gsp-workflow", "gsp-ship", False, False, None, "delivery", "gsp-workflow"),
)


def route_dicts() -> list[dict]:
    return [asdict(route) for route in ROUTES]


def validate_routes(known_skills: set[str] | None = None) -> list[str]:
    """Return structural errors; an empty list means the registry is valid."""
    known = known_skills or {route.next_skill for route in ROUTES} | {route.source for route in ROUTES} | {"gsp-workflow"}
    errors: list[str] = []
    ids: set[str] = set()
    for route in ROUTES:
        if route.route_id in ids:
            errors.append(f"duplicate route_id: {route.route_id}")
        ids.add(route.route_id)
        for field in (route.source, route.next_skill, route.return_to, route.unknown_fallback):
            if field not in known:
                errors.append(f"unknown skill {field} in {route.route_id}")
        if not route.condition or not route.evidence_kind:
            errors.append(f"missing condition/evidence_kind: {route.route_id}")
        if route.user_decision_required and not route.decision_kind:
            errors.append(f"decision_kind required: {route.route_id}")
    if not any(route.next_skill == "gsp-workflow" and route.condition == "unknown" for route in ROUTES):
        errors.append("missing workflow unknown fallback")
    return errors


def validate_generated_catalog(root: Path) -> list[str]:
    catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    known = {item["name"] for item in catalog.get("skills", [])}
    return validate_routes(known)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    errors = validate_generated_catalog(Path(args.root).resolve())
    print(json.dumps({"routes": len(ROUTES), "errors": errors, "valid": not errors}, ensure_ascii=False))
    raise SystemExit(1 if errors else 0)
