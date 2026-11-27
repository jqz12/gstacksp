"""Use existing plans, task state and decisions for bounded execution."""
import copy
from datetime import datetime, timedelta, timezone
import json
import re

from .execution_policy import checkpoint_decision, validate_task_slices


def _time(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('CONVERGENCE_TIMEZONE_REQUIRED')
    return result


def plan_policies(plan):
    """Optional single-line Convergence JSON belongs to its task section."""
    from .task_contracts import _sections
    _, sections = _sections(plan)
    result = {}
    for task_id, body in sections.items():
        matches = re.findall(r'^Convergence:\s*(\{[^\n]+\})\s*$', body, re.M)
        if not matches:
            continue
        if len(matches) != 1:
            raise ValueError('CONVERGENCE_AMBIGUOUS ' + task_id)
        policy = json.loads(matches[0])
        if not isinstance(policy, dict):
            raise ValueError('CONVERGENCE_INVALID ' + task_id)
        for item in policy.get('slices', []):
            if any(key in item for key in ('status', 'accepted', 'accepted_at', 'evidence')):
                raise ValueError('PLAN_CANNOT_ACCEPT_SLICE ' + task_id)
        verdict = validate_task_slices(policy)
        if not verdict['structure_ok']:
            raise ValueError('TASK_SLICES_REQUIRED ' + task_id + ' ' + json.dumps(verdict['errors']))
        result[task_id] = policy
    return result


def _policy_value(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _invalidate_slice_acceptance(task, previous_policy, reason):
    accepted = task.get('accepted_slices') or {}
    last_accepted = task.get('last_accepted_at')
    decision = task.get('convergence_decision')
    if accepted or last_accepted or decision:
        task.setdefault('convergence_history', []).append({
            'event': 'policy-invalidated',
            'reason': reason,
            'previous_policy': copy.deepcopy(previous_policy),
            'accepted_slices': copy.deepcopy(accepted),
            'last_accepted_at': last_accepted,
            'convergence_decision': copy.deepcopy(decision),
        })
    task['accepted_slices'] = {}
    task.pop('last_accepted_at', None)
    task.pop('convergence_decision', None)


def seal_policies(state, plan):
    """Bind plan policies to task state and invalidate superseded acceptance.

    ``state["convergence_schema"] == 1`` marks a new ledger. Every task must then
    declare either ordered slices or an explicit single/small outcome; absence is
    recorded as needs-triage and later rejected by start/complete guards. A
    ledger without the field is genuinely legacy and is marked NOT_CHECKED.
    """
    if not isinstance(state, dict) or not isinstance(state.get('tasks'), list):
        raise ValueError('CONVERGENCE_STATE_INVALID')
    schema = state.get('convergence_schema')
    if schema not in (None, 1):
        raise ValueError('CONVERGENCE_SCHEMA_UNSUPPORTED')
    policies = plan_policies(plan)
    summary = {
        'schema': schema,
        'status': 'declared' if schema == 1 else 'legacy-aware',
        'tasks': {},
        'needs_triage': [],
        'invalidated': [],
    }
    for task in state['tasks']:
        if not isinstance(task, dict) or not str(task.get('id') or '').strip():
            raise ValueError('CONVERGENCE_TASK_INVALID')
        task_id = str(task['id'])
        declared = policies.get(task_id)
        previous = task.get('execution_policy')
        if schema == 1:
            policy = declared if declared is not None else {
                'schema': 1,
                'status': 'needs_triage',
                'reason': 'CONVERGENCE_DECLARATION_REQUIRED',
            }
            declared_ok = declared is not None
        else:
            if declared is None:
                changed = previous is not None
                if changed:
                    _invalidate_slice_acceptance(
                        task, previous, 'plan policy removed or changed',
                    )
                    task.pop('execution_policy', None)
                    summary['invalidated'].append(task_id)
                task['convergence_status'] = {
                    'status': 'legacy',
                    'validation': 'NOT_CHECKED',
                }
                summary['tasks'][task_id] = {
                    'status': 'legacy',
                    'validation': 'NOT_CHECKED',
                    'policy_changed': changed,
                }
                continue
            policy = declared
            declared_ok = True
        changed = previous is None or _policy_value(previous) != _policy_value(policy)
        if changed:
            _invalidate_slice_acceptance(
                task, previous, 'plan policy removed or changed',
            )
            task['execution_policy'] = copy.deepcopy(policy)
            summary['invalidated'].append(task_id)
        status = 'declared' if declared_ok else 'needs_triage'
        task['convergence_status'] = {
            'status': status,
            'validation': 'CHECKED' if declared_ok else 'NOT_READY',
        }
        if not declared_ok:
            summary['needs_triage'].append(task_id)
        summary['tasks'][task_id] = {
            'status': status,
            'validation': task['convergence_status']['validation'],
            'policy_changed': changed,
        }
    return summary


def _require_policy(task, convergence_schema=None):
    policy = task.get('execution_policy')
    if policy is None and convergence_schema == 1:
        raise ValueError('CONVERGENCE_TRIAGE_REQUIRED no declared slices or single outcome')
    if policy is not None and policy.get('status') == 'needs_triage':
        raise ValueError('CONVERGENCE_TRIAGE_REQUIRED explicit task classification is missing')
    return policy


def start_guard(task, at, *, convergence_schema=None):
    policy = _require_policy(task, convergence_schema)
    if policy is not None:
        verdict = validate_task_slices(policy)
        if not verdict['structure_ok']:
            raise ValueError('TASK_SLICES_REQUIRED ' + str(task.get('id')))
    if not task.get('original_started_at'):
        return {'status': 'active', 'checkpoint_due': False, 'preserve_leases': True}
    decision = checkpoint_decision(task, at)
    if decision['status'] == 'needs_triage':
        raise ValueError('CONVERGENCE_TIMELINE_INVALID ' + decision['reason'])
    if decision['sol_decision_required']:
        continuation = task.get('convergence_decision') or {}
        anchor = decision['last_accepted_at'] or decision['original_started_at']
        if (continuation.get('anchor') != anchor or
                not continuation.get('outcome') or not continuation.get('reason') or
                _time(continuation.get('expires_at', '1970-01-01T00:00:00+00:00')) <= _time(at)):
            raise ValueError('SOL_CONVERGENCE_DECISION_REQUIRED no accepted slice for 90 minutes')
    return decision


def complete_guard(task, *, convergence_schema=None):
    policy = _require_policy(task, convergence_schema)
    if policy is not None:
        verdict = validate_task_slices(policy)
        if not verdict['structure_ok']:
            raise ValueError('TASK_SLICES_REQUIRED ' + str(task.get('id')))
    if policy and policy.get('slices'):
        accepted = task.get('accepted_slices', {})
        if any(item['id'] not in accepted for item in policy['slices']):
            raise ValueError('TASK_SLICES_INCOMPLETE internal checkpoints do not complete a task')


def apply_decision(task, value, at, *, candidate_digest=None, evidence=None):
    """Apply an authorized controller decision, not an executor self-acceptance.

    Authority and actual candidate/evidence checks belong to the existing CLI
    boundary. No separate monitor, review ledger or user approval is created.
    """
    result = copy.deepcopy(task)
    action = value.get('action')
    if action == 'checkpoint':
        if not str(value.get('summary', '')).strip():
            raise ValueError('CONVERGENCE_SUMMARY_REQUIRED')
        result.update(last_checkpoint_at=at, checkpoint_summary=value['summary'])
    elif action in {'continue', 'split', 'takeover'}:
        minutes = value.get('minutes')
        if type(minutes) is not int or not 1 <= minutes <= 45:
            raise ValueError('CONVERGENCE_TIMEBOX_REQUIRED 1..45 minutes')
        if not str(value.get('reason', '')).strip() or not str(value.get('outcome', '')).strip():
            raise ValueError('CONVERGENCE_HYPOTHESIS_AND_OUTCOME_REQUIRED')
        timeline = checkpoint_decision(result, at)
        if timeline['status'] == 'needs_triage':
            raise ValueError('CONVERGENCE_TIMELINE_INVALID')
        result['convergence_decision'] = {
            'action': action, 'reason': value['reason'], 'outcome': value['outcome'],
            'at': at, 'expires_at': (_time(at) + timedelta(minutes=minutes)).isoformat(),
            'anchor': timeline['last_accepted_at'] or timeline['original_started_at'],
        }
        # The no-delivery clock is intentionally not reset by this decision.
    elif action == 'accept_slice':
        policy = result.get('execution_policy') or {}
        item = next((row for row in policy.get('slices', []) if row['id'] == value.get('slice')), None)
        if item is None:
            raise ValueError('TASK_SLICE_UNKNOWN')
        accepted = result.setdefault('accepted_slices', {})
        if any(dep not in accepted for dep in item.get('deps', [])):
            raise ValueError('SLICE_DEPENDENCIES_PENDING')
        if (not candidate_digest or value.get('candidate_digest') != candidate_digest
                or not evidence or not value.get('spec_review') or not value.get('quality_review')):
            raise ValueError('SLICE_ACCEPTANCE_EVIDENCE_REQUIRED')
        if item['id'] in accepted:
            previous = accepted[item['id']]
            if any(previous.get(key) != current for key, current in (
                ('candidate_digest', candidate_digest), ('evidence', evidence),
                ('spec_review', value['spec_review']), ('quality_review', value['quality_review']))):
                raise ValueError('SLICE_ALREADY_ACCEPTED reopen explicitly before replacing its proof')
            return result
        accepted[item['id']] = {'candidate_digest': candidate_digest,
                                'evidence': evidence, 'spec_review': value['spec_review'],
                                'quality_review': value['quality_review'], 'accepted_at': at}
        result['last_accepted_at'] = at
        result.setdefault('original_started_at', at)
        # Parent status and sibling tasks remain untouched.
    else:
        raise ValueError('CONVERGENCE_ACTION_INVALID')
    return result
