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
    if target != expected:
        return [_gate('STAGE_ORDER_INVALID', f'{current} cannot advance directly to {target}',
                      f'advance to {expected}' if expected else 'resume the current task', current)]
    if current == 'design':
        try:
            project.valid_design(state, project.paths(state['id'])[1])
        except Exception as exc:
            return [_gate('DESIGN_NOT_APPROVED', str(exc), 'approve the current design', current)]
    elif current == 'plan':
        if not state.get('tasks'):
            return [_gate('TASKS_MISSING', 'no implementation tasks are registered', 'task-add the complete plan', current)]
        try:
            project.valid_design(state, project.paths(state['id'])[1])
            project.valid_plan(state, project.paths(state['id'])[1])
        except Exception as exc:
            return [_gate('PLAN_NOT_SEALED', str(exc), 'plan-check then seal-plan', current)]
        if state.get('scope', {}).get('mode') not in ('frozen', 'off'):
            return [_gate('SCOPE_UNDECIDED', 'scope must be frozen or explicitly off', 'choose scope once', current)]
    elif current == 'execute':
        pending = [t['id'] for t in state.get('tasks', []) if t.get('status') != 'done']
        if pending:
            return [_gate('TASKS_INCOMPLETE', ', '.join(pending), 'finish all registered tasks', current)]
        if state.get('inflight'):
            return [_gate('EXECUTION_UNKNOWN', 'an inflight command has not been recovered', 'recover-run and revalidate', current)]
        final = [p for p in state.get('evidence', []) if p.endswith('/receipt.json') and '/all/' in p]
        if not final:
            return [_gate('FINAL_REGRESSION_REQUIRED', 'no final all-lane evidence is recorded', 'run the final regression with --task all', current)]
    elif current == 'review':
        review = state.get('review') or {}
        if not review or review.get('blocking'):
            return [_gate('REVIEW_BLOCKED', 'review is missing or blocking', 'write review.md and register a non-blocking review', current)]
    elif current == 'checking':
        if state.get('inflight'):
            return [_gate('EXECUTION_UNKNOWN', 'verification has an inflight command', 'recover-run and revalidate', current)]
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
    payload = {'at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(timespec='seconds'),
               'from_stage': before, 'to_stage': target, 'completed_gates': [],
               'pending_gates': [], 'next_skill': NEXT_SKILL.get(target, 'gsp-workflow'),
               'owner': 'controller', 'documents': result['documents'], 'evidence': state.get('evidence', [])}
    record_handoff(run, payload)
    return {'task': state['id'], 'from_stage': before, 'to_stage': target, 'allowed': True,
            'next_skill': payload['next_skill'], 'handoff': payload}, True
