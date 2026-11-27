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
    Route(**{'route_id': 'start.no-active-task', 'source': 'gsp-workflow', 'condition': 'no_active_task', 'next_skill': 'gsp-brainstorming', 'return_to': 'gsp-workflow', 'blocking': False, 'user_decision_required': False, 'decision_kind': None, 'evidence_kind': 'project-state', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'workflow.unknown', 'source': 'gsp-workflow', 'condition': 'unknown', 'next_skill': 'gsp-workflow', 'return_to': 'gsp-workflow', 'blocking': True, 'user_decision_required': True, 'decision_kind': 'investigation', 'evidence_kind': 'workflow-state', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'start.active-mismatch', 'source': 'gsp-workflow', 'condition': 'active_task_mismatch', 'next_skill': 'gsp-workflow', 'return_to': 'gsp-workflow', 'blocking': True, 'user_decision_required': False, 'decision_kind': 'investigation', 'evidence_kind': 'project-state', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'git.clean.no-task', 'source': 'gsp-health', 'condition': 'clean_and_no_active_task', 'next_skill': 'gsp-brainstorming', 'return_to': 'gsp-workflow', 'blocking': False, 'user_decision_required': False, 'decision_kind': None, 'evidence_kind': 'git-health', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'git.clean.active-task', 'source': 'gsp-health', 'condition': 'clean_and_active_task', 'next_skill': 'gsp-workflow', 'return_to': 'gsp-workflow', 'blocking': False, 'user_decision_required': False, 'decision_kind': None, 'evidence_kind': 'git-health', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'git.dirty', 'source': 'gsp-health', 'condition': 'dirty_or_untracked', 'next_skill': 'gsp-ship', 'return_to': 'gsp-workflow', 'blocking': False, 'user_decision_required': True, 'decision_kind': 'worktree', 'evidence_kind': 'git-health', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'git.detached', 'source': 'gsp-health', 'condition': 'detached_head', 'next_skill': 'gsp-ship', 'return_to': 'gsp-workflow', 'blocking': True, 'user_decision_required': True, 'decision_kind': 'branch', 'evidence_kind': 'git-health', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'git.no-repository', 'source': 'gsp-health', 'condition': 'repository_missing', 'next_skill': 'gsp-ship', 'return_to': 'gsp-workflow', 'blocking': True, 'user_decision_required': True, 'decision_kind': 'repository', 'evidence_kind': 'git-health', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'git.unknown', 'source': 'gsp-health', 'condition': 'git_probe_unknown', 'next_skill': 'gsp-ship', 'return_to': 'gsp-workflow', 'blocking': True, 'user_decision_required': True, 'decision_kind': 'investigation', 'evidence_kind': 'git-health', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'git.topology.required', 'source': 'gsp-health', 'condition': 'git_topology_required', 'next_skill': 'gsp-ship', 'return_to': 'gsp-workflow', 'blocking': True, 'user_decision_required': True, 'decision_kind': 'worktree', 'evidence_kind': 'git-topology', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'git.topology.invalid', 'source': 'gsp-health', 'condition': 'git_topology_invalid', 'next_skill': 'gsp-ship', 'return_to': 'gsp-workflow', 'blocking': True, 'user_decision_required': True, 'decision_kind': 'investigation', 'evidence_kind': 'git-topology', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'execute.test-failure', 'source': 'gsp-execute', 'condition': 'test_failure', 'next_skill': 'gsp-execute', 'return_to': 'gsp-execute', 'blocking': True, 'user_decision_required': False, 'decision_kind': None, 'evidence_kind': 'test-receipt', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'quality.failure', 'source': 'gsp-execute', 'condition': 'required_check_failed', 'next_skill': 'gsp-execute', 'return_to': 'gsp-execute', 'blocking': True, 'user_decision_required': False, 'decision_kind': None, 'evidence_kind': 'quality-report', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'quality.not-checked', 'source': 'gsp-execute', 'condition': 'quality_stack_missing_or_tool_unavailable', 'next_skill': 'gsp-workflow', 'return_to': 'gsp-execute', 'blocking': True, 'user_decision_required': True, 'decision_kind': 'quality-stack', 'evidence_kind': 'quality-report', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'quality.change-pass', 'source': 'gsp-execute', 'condition': 'change_checks_passed', 'next_skill': 'gsp-review', 'return_to': 'gsp-execute', 'blocking': False, 'user_decision_required': False, 'decision_kind': None, 'evidence_kind': 'quality-report', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'quality.baseline-pass', 'source': 'gsp-execute', 'condition': 'baseline_checks_passed', 'next_skill': 'gsp-workflow', 'return_to': 'gsp-execute', 'blocking': False, 'user_decision_required': True, 'decision_kind': 'quality-baseline', 'evidence_kind': 'quality-report', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'quality.release-pass', 'source': 'gsp-execute', 'condition': 'release_checks_passed', 'next_skill': 'gsp-review', 'return_to': 'gsp-execute', 'blocking': False, 'user_decision_required': False, 'decision_kind': None, 'evidence_kind': 'quality-report', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'review.blocking-finding', 'source': 'gsp-review', 'condition': 'blocking_finding', 'next_skill': 'gsp-execute', 'return_to': 'gsp-review', 'blocking': True, 'user_decision_required': False, 'decision_kind': None, 'evidence_kind': 'review', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'verify.stale-evidence', 'source': 'gsp-review', 'condition': 'evidence_stale', 'next_skill': 'gsp-review', 'return_to': 'gsp-workflow', 'blocking': True, 'user_decision_required': False, 'decision_kind': None, 'evidence_kind': 'verification', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'documents.findings', 'source': 'gsp-health', 'condition': 'document_findings_with_authorized_governance', 'next_skill': 'gsp-document-governance', 'return_to': 'gsp-workflow', 'blocking': False, 'user_decision_required': False, 'decision_kind': None, 'evidence_kind': 'document-report', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'documents.readonly', 'source': 'gsp-health', 'condition': 'document_findings_readonly', 'next_skill': 'gsp-health', 'return_to': 'gsp-workflow', 'blocking': False, 'user_decision_required': True, 'decision_kind': 'governance', 'evidence_kind': 'document-report', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'restore.unknown', 'source': 'gsp-workflow', 'condition': 'resume_mismatch_or_unknown', 'next_skill': 'gsp-workflow', 'return_to': 'gsp-workflow', 'blocking': True, 'user_decision_required': True, 'decision_kind': 'investigation', 'evidence_kind': 'resume-state', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'complete.review', 'source': 'gsp-execute', 'condition': 'all_tasks_done_and_regression_passed', 'next_skill': 'gsp-review', 'return_to': 'gsp-workflow', 'blocking': False, 'user_decision_required': False, 'decision_kind': None, 'evidence_kind': 'task-state', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'review.verify', 'source': 'gsp-review', 'condition': 'review_passed', 'next_skill': 'gsp-ship', 'return_to': 'gsp-review', 'blocking': False, 'user_decision_required': False, 'decision_kind': None, 'evidence_kind': 'review', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'verify.ship', 'source': 'gsp-review', 'condition': 'evidence_valid', 'next_skill': 'gsp-ship', 'return_to': 'gsp-review', 'blocking': False, 'user_decision_required': False, 'decision_kind': None, 'evidence_kind': 'verification', 'unknown_fallback': 'gsp-workflow'}),
    Route(**{'route_id': 'ship.ready', 'source': 'gsp-ship', 'condition': 'delivery_prepared', 'next_skill': 'gsp-workflow', 'return_to': 'gsp-ship', 'blocking': False, 'user_decision_required': False, 'decision_kind': None, 'evidence_kind': 'delivery', 'unknown_fallback': 'gsp-workflow'}),
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


def route_for(condition: str) -> Route:
    for route in ROUTES:
        if route.condition == condition:
            return route
    return next(route for route in ROUTES if route.condition == "unknown")


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
