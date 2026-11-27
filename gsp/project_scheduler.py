"""Readonly project queue views over work ledgers; grants use resource CAS."""
import os
from pathlib import Path

from .core import WorkflowError, read
from .contract_store import load_contract
from .orchestration_scheduler import next_batch, aggregate_usage
from .resource_registry import ResourceRegistry


def preview_schedule(project,*,limit=3):
    registry=ResourceRegistry(project.root)
    snapshot=registry.scheduler_snapshot()
    candidates=[]
    run_ids={}
    skipped=[]
    runs=project.meta/'runs'
    for file in sorted(runs.glob('*/state.json')):
        if file.is_symlink() or file.parent.is_symlink():
            raise WorkflowError('SCHEDULER_LEDGER_PATH_INVALID')
        state=read(file)
        if state.get('status') not in {'execute','needs-verification'} or not state.get('work_uuid'):
            continue
        dispatch_file=file.parent/'sessions/dispatch.json'
        dispatches=read(dispatch_file).get('dispatches',[]) if dispatch_file.is_file() else []
        accepted={task['id'] for task in state.get('tasks',[]) if task.get('status')=='done'}
        accepted.update(row['task'] for row in dispatches if row.get('status')=='accepted')
        active={row.get('task') for row in dispatches if row.get('status') in {
            'preparing','adapter-ready','manual-required','dispatched','running','reported'}}
        for task in state.get('tasks',[]):
            if task.get('status') not in {'pending','needs-verification'} or task['id'] in active:
                continue
            try:
                contract=load_contract(project,state,task['id'],required=True)
            except WorkflowError as exc:
                skipped.append({'run_id':state['id'],'task_id':task['id'],'reason':str(exc)})
                continue
            source=project.workspace_root(state,task_id=task['id'],validate=False)
            # Preserve real parent/child overlap in a portable relative key.
            paths=[]
            for relative in contract.get('writes') or []:
                full=os.path.normcase(str(Path(source)/relative)).replace('\\','/').casefold()
                paths.append('path/'+full.replace(':','').lstrip('/'))
            candidates.append({'work_uid':state['work_uuid'],'task_id':task['id'],
                'status':'ready','depends':task.get('depends') or [],
                'accepted_task_ids':sorted(accepted),'writes':paths,
                'contract_digest':contract['content_digest'],'role':'worker',
                'model_alias':'group/deepseek-2','effort':'max'})
            run_ids[state['work_uuid']]=state['id']
    result=next_batch(candidates,limit=limit,cursor=snapshot['cursor'],occupied=snapshot['occupied'])
    for row in result['batch']:
        row['run_id']=run_ids[row['work_uid']]
    return {**result,'revision':snapshot['revision'],'occupied':snapshot['occupied'],
            'skipped':result['skipped']+skipped,'validation':'NOT_CHECKED',
            'authority':'resource-registry; ledger candidates are advisory until CAS grant'}


def usage_for_work(project,state):
    """Native response records are evidence, not another Goal engine."""
    events=[]
    file=project.paths(state['id'])[0]/'driver/state.json'
    if file.is_file():
        for row in read(file).get('native_usage') or []:
            events.extend(row.get('events') or [])
    return aggregate_usage(events)
