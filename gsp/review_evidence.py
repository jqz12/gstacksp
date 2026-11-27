"""Review dispositions in the existing report and work issue records.

This module does no I/O. A candidate digest identifies reviewed content; it does
not restrict which directories an authorized executor may edit.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re

from .execution_policy import classify_review_finding
from .orchestration_types import digest_value

_REPORT = re.compile(r'<!--\s*gsp-review-evidence\s*\n(.*?)-->', re.S)
_DIGEST = re.compile(r'[0-9a-f]{64}')


def review_metadata(text):
    matches = _REPORT.findall(text)
    if len(matches) != 1:
        raise ValueError('REVIEW_EVIDENCE_REQUIRED one candidate-bound review block is required')
    try:
        report = json.loads(matches[0])
    except (ValueError, TypeError) as exc:
        raise ValueError('REVIEW_EVIDENCE_REQUIRED invalid review JSON') from exc
    if not isinstance(report, dict) or not isinstance(report.get('findings'), list):
        raise ValueError('REVIEW_EVIDENCE_REQUIRED findings must be an array')
    return report


def evaluate_review(text, candidate_digest, contract_digest, previous_issues,
                    *, work_uid, task_uid='all'):
    report = review_metadata(text)
    if not _DIGEST.fullmatch(str(candidate_digest)) or report.get('candidate_digest') != candidate_digest:
        raise ValueError('CANDIDATE_STALE review must name the current candidate digest')
    if not _DIGEST.fullmatch(str(contract_digest)) or report.get('contract_digest') != contract_digest:
        raise ValueError('CONTRACT_STALE review must name the current contract digest')
    issues, findings, seen = deepcopy(previous_issues or {}), [], set()
    for original in report['findings']:
        if not isinstance(original, dict) or any(
                not isinstance(original.get(key), str) or not original[key].strip()
                for key in ('acceptance_key', 'root_cause_key', 'signature')):
            raise ValueError('REVIEW_FINDING_ID_REQUIRED acceptance, root cause and signature are required')
        identity = {key: original[key] for key in ('acceptance_key', 'root_cause_key', 'signature')}
        identity.update(work_uid=work_uid, task_uid=task_uid)
        # Labels, task placement and contract versions may evolve while the
        # root problem remains. Reuse its work-scoped identity and repair count.
        matches = [row for row in issues.values()
                   if row.get('work_uid') == work_uid
                   and row.get('root_cause_key') == identity['root_cause_key']
                   and row.get('signature') == identity['signature']]
        if len(matches) > 1:
            raise ValueError('REVIEW_ISSUE_AMBIGUOUS reference and reconcile the existing problem identities')
        uid = matches[0]['uid'] if matches else digest_value(identity)
        if uid in seen:
            raise ValueError('REVIEW_FINDING_DUPLICATE ' + uid)
        seen.add(uid)
        previous = issues.get(uid, {})
        finding = {**original, **identity, 'issue_uid': uid,
                   'reviewed_candidate_digest': candidate_digest,
                   'current_candidate_digest': candidate_digest,
                   'repair_count': previous.get('repair_count', 0)}
        finding.pop('issue', None)
        finding['issue_ref'] = previous
        result = classify_review_finding(finding, contract_digest)
        result['issue_ref'] = (result['issue_ref'].to_dict()
                               if result.get('issue_ref') else None)
        blocking = (result.get('blocking_current_acceptance', True) or not result.get('accepted')
                    or (previous.get('status') != 'resolved'
                        and previous.get('blocking_current_acceptance', False)))
        owner = result.get('owner') or 'sol'
        issues[uid] = {**previous, **identity, 'uid': uid, 'status': 'open',
                       'repair_count': previous.get('repair_count', 0),
                       'current_owner': owner, 'classification': result.get('classification'),
                       'blocking_current_acceptance': blocking,
                       'reviewed_candidate_digest': candidate_digest,
                       'contract_digest': contract_digest}
        findings.append({**result, 'issue_uid': uid})
    # An empty/new report cannot silently erase an unresolved required issue.
    blocking = sum(row.get('status') != 'resolved' and row.get('blocking_current_acceptance', False)
                   for row in issues.values())
    return {'findings': findings, 'issues': issues, 'blocking': blocking,
            'candidate_digest': candidate_digest, 'contract_digest': contract_digest}


def record_issue_decision(previous_issues, decision):
    issues = deepcopy(previous_issues or {})
    uid = decision.get('uid')
    if uid not in issues:
        raise ValueError('ISSUE_UNKNOWN ' + str(uid))
    issue = issues[uid]
    action = decision.get('action')
    if action == 'worker_repair':
        if issue.get('status') == 'resolved':
            raise ValueError('ISSUE_ALREADY_RESOLVED ' + uid)
        # This counts corrections of one issue, never correction batches/tasks.
        if issue.get('repair_count', 0) >= 1:
            raise ValueError('ISSUE_SOL_TAKEOVER_REQUIRED same issue already received its worker correction')
        else:
            issue['repair_count'] = 1
            issue['current_owner'] = 'worker'
    elif action == 'sol_takeover':
        binding_uid = decision.get('controller_binding_uid')
        if not isinstance(binding_uid, str) or not binding_uid:
            raise ValueError('ISSUE_CONTROLLER_BINDING_REQUIRED')
        issue.update(current_owner='sol', takeover_binding_uid=binding_uid)
    elif action == 'resolve':
        evidence = decision.get('evidence')
        if not isinstance(evidence, list) or not evidence or any(
                not isinstance(row, dict) or not row.get('path')
                or not _DIGEST.fullmatch(str(row.get('sha256', '')))
                or not _DIGEST.fullmatch(str(row.get('candidate_digest', '')))
                for row in evidence):
            raise ValueError('RESOLUTION_EVIDENCE_REQUIRED current verified evidence is required')
        issue.update(status='resolved', resolution=deepcopy(evidence),
                     resolution_reason=str(decision.get('reason') or ''))
    else:
        raise ValueError('ISSUE_DECISION_INVALID ' + str(action))
    return issues


def validate_reviewer(binding, state, excluded_threads, *, excluded_binding_uid=None):
    from .orchestration_types import SessionBinding
    if (not isinstance(binding, SessionBinding) or binding.kind not in {'reviewer', 'observer'}
            or binding.binding_uid == excluded_binding_uid or binding.cancelled
            or binding.host != 'codex'
            or not binding.host_thread_id or binding.host_thread_id in excluded_threads
            or binding.work_uid != state.get('work_uuid') or 'report' not in binding.capabilities
            or binding.controller_generation != int(state.get('controller_generation') or 1)
            or binding.cancel_generation != int(state.get('cancel_generation') or 1)):
        raise ValueError('INDEPENDENT_REVIEW_REQUIRED use a verified, separate reviewer context for this work')
    return {'binding_uid': binding.binding_uid, 'host': binding.host,
            'host_thread_id': binding.host_thread_id, 'independence': 'verified-context',
            # The authority verifies the context/role; it does not manufacture
            # an observation that Codex has launched a particular host turn.
            'host_observation': binding.host_state}
