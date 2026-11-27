"""Immutable task-contract artifacts used by every side-effect entry."""
from pathlib import Path

from .core import WorkflowError, atomic, inside, read
from .task_contracts import compile_task_contract, verify_task_contract


def _source(project,state):
    plan=project.document(state,'plan')
    spec=project.document(state,'design')
    return plan.read_text(encoding='utf-8-sig'),spec.read_text(encoding='utf-8-sig'),{
        'plan':str(plan),'design':str(spec)}


def publish_contracts(project,state):
    """Called after authorized seal checks; immutable orphans are not authority."""
    plan,spec,refs=_source(project,state)
    if state.get('architecture_policy') is not None:
        from .architecture_inputs import freeze_architecture_inputs
        state['architecture_policy']=freeze_architecture_inputs(project.root,state['architecture_policy'])
    try:
        contracts={task['id']:compile_task_contract(state,task['id'],plan,spec,refs)
                   for task in state.get('tasks',[])}
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    base=project.paths(state['id'])[0]/'contracts'
    result={}
    for task_id,contract in contracts.items():
        path=base/(contract['contract_uid']+'.json')
        if path.exists():
            if read(path)!=contract:
                raise WorkflowError('TASK_CONTRACT_OBJECT_CHANGED')
        else:
            atomic(path,contract)
        result[task_id]={'path':path.relative_to(project.root).as_posix(),
                         'contract_uid':contract['contract_uid'],
                         'content_digest':contract['content_digest']}
    state['task_contracts']=result
    return result


def load_contract(project,state,task_id,*,required=False):
    reference=(state.get('task_contracts') or {}).get(task_id)
    if reference is None:
        if required:
            raise WorkflowError('TASK_CONTRACT_REQUIRED seal the current task plan')
        return None
    if not isinstance(reference,dict) or not isinstance(reference.get('path'),str):
        raise WorkflowError('TASK_CONTRACT_REFERENCE_INVALID')
    candidate=project.root/reference['path']
    if not inside(project.root,candidate) or candidate.is_symlink():
        raise WorkflowError('TASK_CONTRACT_PATH_ESCAPE')
    for parent in candidate.parents:
        if parent==project.root:
            break
        if parent.is_symlink() or getattr(parent,'is_junction',lambda:False)():
            raise WorkflowError('TASK_CONTRACT_PATH_ESCAPE')
    contract=read(candidate)
    if any(contract.get(key)!=reference.get(key) for key in ('contract_uid','content_digest')):
        raise WorkflowError('TASK_CONTRACT_REFERENCE_CHANGED')
    plan,spec,refs=_source(project,state)
    try:
        verify_task_contract(contract,state,plan,spec,refs)
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    return contract
