"""Single-stage handoff checks for the GSP workflow controller."""
import json
from pathlib import Path


STAGES = ('design', 'plan', 'execute', 'review', 'checking', 'ready', 'ship')
NEXT_SKILL = {
    'design': 'gsp-brainstorming', 'plan': 'gsp-writing-plans',
    'execute': 'gsp-execute', 'review': 'gsp-review',
    'checking': 'gsp-review', 'ready': 'gsp-ship', 'ship': 'gsp-workflow',
}
TRANSITIONS = {'design': 'plan', 'plan': 'execute', 'execute': 'review',
               'review': 'checking', 'checking': 'ready', 'ready': 'ship'}


def _gate(code, detail, action, stage, documents=None, evidence=None):
    from .gate_rules import _gate as render
    return render(code,detail,action,stage,documents,evidence)


def _docs(project, state):
    run, doc = project.paths(state['id'])
    return [str(project.document(state, 'design')), str(project.document(state, 'plan')),
            str(doc / 'review.md'), str(doc / 'delivery.md')]


def _checks(project,state,target):
    from .gate_rules import stage_rules
    return stage_rules(project,state,target)


def blocked_preview(project, state, target, blockers):
    current = state.get('status')
    target = target or TRANSITIONS.get(current)
    return {'task': state['id'], 'from_stage': current, 'to_stage': target,
            'allowed': False, 'blockers': blockers,
            'next_skill': NEXT_SKILL.get(target or current, 'gsp-workflow'),
            'documents': _docs(project, state), 'scope': state.get('scope', {}).get('mode')}


def preview(project, state, target=None):
    current = state.get('status')
    target = target or TRANSITIONS.get(current)
    blockers = _checks(project, state, target) if target else [_gate('NO_NEXT_STAGE', 'task has no automatic next stage', 'resume or close the task', current)]
    result = blocked_preview(project, state, target, blockers)
    result['allowed'] = not blockers
    return result


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
        from .workflow_support import (fingerprint)
        state['delivery_fingerprint'] = fingerprint(project.verification_root(state))
    payload = {'at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(timespec='seconds'),
               'from_stage': before, 'to_stage': target, 'completed_gates': [],
               'pending_gates': [], 'next_skill': NEXT_SKILL.get(target, 'gsp-workflow'),
               'owner': 'controller', 'documents': result['documents'], 'evidence': state.get('evidence', [])}
    record_handoff(run, payload)
    return {'task': state['id'], 'from_stage': before, 'to_stage': target, 'allowed': True,
            'next_skill': payload['next_skill'], 'handoff': payload}, True
