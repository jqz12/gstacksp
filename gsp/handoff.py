"""Single-stage handoff checks for the GSP workflow controller."""
import json
from pathlib import Path


STAGES = ('design', 'plan', 'execute', 'review', 'checking', 'ready', 'ship')
NEXT_SKILL = {
    'design': 'gsp-brainstorming', 'plan': 'gsp-writing-plans',
    'execute': 'gsp-execute', 'review': 'gsp-review',
    'checking': 'gsp-verify', 'ready': 'gsp-ship', 'ship': 'gsp-workflow',
}
TRANSITIONS = {'design': 'plan', 'plan': 'execute', 'execute': 'review',
               'review': 'checking', 'checking': 'ready', 'ready': 'ship'}


def _gate(code, detail, action, stage, documents=None, evidence=None):
    return {'code': code, 'detail': detail, 'required_action': action,
            'stage': stage, 'documents': documents or [], 'evidence': evidence or []}


def _docs(project, state):
    run, doc = project.paths(state['id'])
    return [str(project.document(state, 'design')), str(project.document(state, 'plan')),
            str(doc / 'review.md'), str(doc / 'delivery.md')]


def _checks(project, state, target):
    current = state.get('status')
    if target == current and current in ('execute', 'review', 'checking', 'ready', 'ship'):
        return []
    expected = TRANSITIONS.get(current)
    if current == 'needs-verification' and target == 'execute':
        expected = 'execute'
    if current in ('checking', 'ready') and target == 'review':
        expected = 'review'
    if target != expected:
        return [_gate('STAGE_ORDER_INVALID', f'{current} cannot advance directly to {target}',
                      f'advance to {expected}' if expected else 'resume the current task', current)]
    if current == 'design':
        try:
            project.valid_design(state, project.paths(state['id'])[1])
        except Exception as exc:
            return [_gate('DESIGN_NOT_APPROVED', str(exc), 'approve the current design', current)]
    elif target == 'execute':
        if not state.get('tasks'):
            return [_gate('TASKS_MISSING', 'no implementation tasks are registered', 'task-add the complete plan', current)]
        try:
            project.valid_design(state, project.paths(state['id'])[1])
            project.valid_plan(state, project.paths(state['id'])[1])
        except Exception as exc:
            return [_gate('PLAN_NOT_SEALED', str(exc), 'plan-check then seal-plan', current)]
        if state.get('scope', {}).get('mode') not in ('frozen', 'off'):
            return [_gate('SCOPE_UNDECIDED', 'scope must be frozen or explicitly off', 'choose scope once', current)]
        from .runtime import topology_required, topology_selected
        if topology_required(state) and not topology_selected(state):
            return [_gate('GIT_TOPOLOGY_REQUIRED', 'the task has no effective Git topology selection',
                          'run topology and topology-select once before implementation', current)]
    elif target == 'review':
        pending = [t['id'] for t in state.get('tasks', []) if t.get('status') != 'done']
        if pending:
            return [_gate('TASKS_INCOMPLETE', ', '.join(pending), 'finish all registered tasks', current)]
        from .runtime import validate_parallel_bindings
        parallel = validate_parallel_bindings(state)
        if not parallel['valid']:
            first = parallel['blocking'][0]
            return [_gate(first['code'], '; '.join(item.get('detail', item['code']) for item in parallel['blocking']),
                          'bind unique child workspaces and complete the integration task', current)]
        if state.get('inflight'):
            return [_gate('EXECUTION_UNKNOWN', 'an inflight command has not been recovered', 'recover-run and revalidate', current)]
        from .runtime import evidence_for
        try:
            evidence_for(project, state, 'all')
            for task in state['tasks']:
                evidence_for(project, state, task['id'], allow_historical=True)
        except Exception as exc:
            return [_gate('FINAL_REGRESSION_REQUIRED', str(exc), 'run current task and final regression evidence', current)]
    elif current == 'review':
        review = state.get('review') or {}
        if not review or review.get('blocking'):
            return [_gate('REVIEW_BLOCKED', 'review is missing or blocking', 'write review.md and register a non-blocking review', current)]
        from .runtime import digest, fingerprint, contract_hash
        if (review.get('sha256') != digest(project.paths(state['id'])[1] / 'review.md') or
                review.get('fingerprint') != fingerprint(project.verification_root(state)) or
                review.get('contract') != contract_hash(project, state, project.paths(state['id'])[1])):
            return [_gate('REVIEW_BLOCKED', 'review is stale', 'revalidate and review current inputs', current)]
    elif current == 'checking':
        if state.get('inflight'):
            return [_gate('EXECUTION_UNKNOWN', 'verification has an inflight command', 'recover-run and revalidate', current)]
        from .runtime import validate_parallel_bindings
        parallel = validate_parallel_bindings(state)
        if not parallel['valid']:
            first = parallel['blocking'][0]
            return [_gate(first['code'], '; '.join(item.get('detail', item['code']) for item in parallel['blocking']),
                          'complete the child and integration topology checks', current)]
        from .runtime import validate_delivery
        try:
            validate_delivery(project, state, project.paths(state['id'])[1])
        except Exception as exc:
            return [_gate('DELIVERY_BLOCKED', str(exc), 'resolve verification gates before ready', current)]
    return []


def preview(project, state, target=None):
    current = state.get('status')
    target = target or TRANSITIONS.get(current)
    blockers = _checks(project, state, target) if target else [_gate('NO_NEXT_STAGE', 'task has no automatic next stage', 'resume or close the task', current)]
    return {'task': state['id'], 'from_stage': current, 'to_stage': target,
            'allowed': not blockers, 'blockers': blockers,
            'next_skill': NEXT_SKILL.get(target or current, 'gsp-workflow'),
            'documents': _docs(project, state), 'scope': state.get('scope', {}).get('mode')}


def record_handoff(run_dir, payload):
    path = Path(run_dir) / 'handoffs.jsonl'
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(payload, ensure_ascii=False) + '\n')


def advance(project, state, run, target):
    result = preview(project, state, target)
    if not result['allowed']:
        return result, False
    before = state['status']
    state['status'] = target
    if target == 'ready':
        from .runtime import fingerprint
        state['delivery_fingerprint'] = fingerprint(project.verification_root(state))
    payload = {'at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(timespec='seconds'),
               'from_stage': before, 'to_stage': target, 'completed_gates': [],
               'pending_gates': [], 'next_skill': NEXT_SKILL.get(target, 'gsp-workflow'),
               'owner': 'controller', 'documents': result['documents'], 'evidence': state.get('evidence', [])}
    record_handoff(run, payload)
    return {'task': state['id'], 'from_stage': before, 'to_stage': target, 'allowed': True,
            'next_skill': payload['next_skill'], 'handoff': payload}, True
