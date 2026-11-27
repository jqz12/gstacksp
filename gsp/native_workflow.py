"""Connect native workspace receipts to the existing topology and close flow."""
from pathlib import Path

from .core import WorkflowError,digest
from .codex_workspace import CodexWorkspaceAdapter
from .git_operations import _git_run,git_identity
from .git_topology import branch_name
from .orchestration_types import RequestRef,WorktreeRequest,digest_value
from .workflow_support import (contract_hash,stable_action_uid,validate_delivery,_hygiene_release)


def plan_topology(project,state,args,binding):
    from .resource_operations import handle_resource
    if state.get('inflight'):
        raise WorkflowError('EXECUTION_UNKNOWN')
    task=getattr(args,'task_id',None) or 'all'
    uid=stable_action_uid('native-topology',state['work_uuid'],task,args.branch,args.path,
                          getattr(args,'base',None),args.quote,getattr(args,'rebind',False))
    saved=(state.get('native_requests') or {}).get(uid)
    if saved is not None:
        if saved.get('confirmed'):
            source=project.workspace_root(state,task_id=None if task=='all' else task)
            return {'task':state['id'],'request_uid':uid,'status':'selected',
                    'workspace_root':str(source),'replayed':True},True
        adapter=CodexWorkspaceAdapter(project.root,state['project_id'])
        intent=adapter.host_action(uid)
        adapter.host_actions.revalidate(intent)
        return {'task':state['id'],'request_uid':uid,'status':'pending-host-action',
                'create_intent':saved['create_intent'],
                'pending_action':{'tool':intent.action,'arguments':dict(intent.action_args)}},True
    source=git_identity(project.root)
    branch=args.branch or branch_name(state['number'],None if task=='all' else task,state['title'],kind='codex')
    base_arg=getattr(args,'base',None) or source.get('head')
    if not base_arg or str(base_arg).startswith('-'):
        raise WorkflowError('NATIVE_BASE_REQUIRED')
    base=_git_run(project.root,'rev-parse','--verify',str(base_arg)+'^{commit}')
    if base.returncode:
        raise WorkflowError('NATIVE_BASE_INVALID')
    base=base.stdout.strip()
    hint=str((project.root/args.path).resolve()) if args.path else None
    body={'base':base,'branch':branch,'path_hint':hint,'task':task}
    documents=project.paths(state['id'])[1]
    if all(project.document(state,kind).is_file() for kind in ('design','plan')):
        contract=contract_hash(project,state,documents)
    else:
        contract=digest_value({'work_uid':state['work_uuid'],'phase':state['status'],
            'design':digest(project.document(state,'design')) if project.document(state,'design').is_file() else None})
    ref=RequestRef(uid,'native-topology:'+uid,digest_value(body),state['project_id'],
                   state['work_uuid'],None,'native-worktree-create',binding.controller_generation,
                   binding.cancel_generation,contract_digest=contract)
    request=WorktreeRequest(ref,'codex-native-worktree',str(project.root),base,branch,hint)
    payload={'request':ref.to_dict(),'worktree':request.to_dict(),'task_uid':task,'contract_digest':contract,
             'owner':getattr(args,'native_owner','local')}
    result,ok=handle_resource(project,state,{'command':'native-workspace','action':'create',
        'request_uid':uid,'payload':payload,'task':task},binding=binding)
    if not ok:
        return result,False
    state.setdefault('native_requests',{})[uid]={'task':task,'create_intent':result}
    target=state.setdefault('git_topology',{}) if task=='all' else project.item(state,task).setdefault('topology',{})
    target.update(status='pending-native',effective_mode=None,enforce=True)
    project.write(state,'native-topology-intent',uid,action_uid='native-topology-intent:'+uid)
    intent=CodexWorkspaceAdapter(project.root,state['project_id']).host_action(uid)
    return {'task':state['id'],'request_uid':uid,'status':'pending-host-action','create_intent':result,
            'pending_action':{'tool':intent.action,'arguments':dict(intent.action_args)}},True


def adopt_confirmed(project,state,uid,result):
    value=result.get('binding') or (result if result.get('ownership')=='native' else None)
    if not value or not value.get('registered'):
        return
    association=(state.get('native_requests') or {}).get(uid)
    if association is None and result.get('action')=='restore_worktree':
        if (state.get('git_topology') or {}).get('workspace_root')==value['path']:
            association={'task':'all'}
        else:
            found=[row for row in state.get('tasks',[]) if (row.get('topology') or {}).get('workspace_root')==value['path']]
            if len(found)==1:
                association={'task':found[0]['id']}
        if association:
            state.setdefault('native_requests',{})[uid]=association
    if association is None:
        return  # Resource-only callers do not silently replace a task's source.
    task=association['task']
    target=state.setdefault('git_topology',{}) if task=='all' else project.item(state,task).setdefault('topology',{})
    if target.get('native_request_uid')==uid and target.get('workspace_root')==value['path']:
        return
    topology={**target,'schema':1,'status':'selected','enforce':True,'effective_mode':'worktree',
        'control_root':str(project.root),'workspace_root':value['path'],'branch':value.get('branch'),
        'base_head':value['base_sha'],'head':value['head'],'native_request_uid':uid,
        'native_binding':value,'owner_adapter':value['owner_adapter'],'managed_worktree':False,
        'worktree_mode':'native','cleanup_action':'archive'}
    if task=='all':
        state['git_topology']=topology
        state['git']=git_identity(value['path'])
    else:
        project.item(state,task)['topology']=topology
    if target.get('workspace_root')!=value['path'] or target.get('head')!=value['head']:
        state['review']=None
        state['verification_epoch']=int(state.get('verification_epoch') or 0)+1
    association['confirmed']=True
    project.write(state,'native-topology-bind',uid,action_uid='native-topology-bind:'+uid)


def _close_inputs(project,state):
    from .todos import Backlog
    from .architecture_inputs import freeze_architecture_inputs
    backlog=Backlog(project.root).summary()
    relevant=[row for section in ('items','orphaned_records') for row in backlog.get(section,[])
              if row.get('origin') in {state['id'],state['work_uuid']}]
    policy=state.get('architecture_policy') or {'required':False}
    freeze_architecture_inputs(project.root,policy)
    report=project.paths(state['id'])[1]/'review.md'
    quality=project.meta/'quality.json'
    return digest_value({'contract':contract_hash(project,state,report.parent),
        'tasks':[(row['id'],row.get('status'),row.get('evidence')) for row in state.get('tasks',[])],
        'evidence':state.get('evidence'),'review':state.get('review'),'issues':state.get('issues'),
        'epoch':state.get('verification_epoch'),'review_digest':digest(report),
        'quality_config':digest(quality) if quality.is_file() else None,'backlog':relevant,'architecture':policy})


def prepare_archive(project,state,args,binding,payload):
    """Freeze an already verified primary candidate before its host removes it."""
    workspace=payload.get('workspace') or payload.get('binding') or {}
    topology=state.get('git_topology') or {}
    if workspace.get('path')!=topology.get('workspace_root'):
        return None
    if state.get('status')!='ready':
        raise WorkflowError('CLOSE_REQUIRES_READY primary native source must be verified before disposal')
    from .resource_operations import preserve_work_evidence
    from .evidence_store import verify_git_reachability
    validate_delivery(project,state,project.paths(state['id'])[1])
    _hygiene_release(project,state)
    manifest=preserve_work_evidence(project,state,binding=binding,
        request_uid='native-archive-evidence:'+args.request_uid,commits=[workspace['head']])
    if not verify_git_reachability(project.root,manifest).get('accepted'):
        raise WorkflowError('EVIDENCE_ARCHIVE_PROOF_REQUIRED')
    payload['manifest']=manifest
    return {'request_uid':args.request_uid,'input_digest':_close_inputs(project,state),
            'manifest_uid':manifest['manifest']['uid'],'work_uid':state['work_uuid'],
            'head':workspace['head'],'path':workspace['path']}


def missing_workspace_close(project,state):
    """Minimal control-root recovery; absence alone never proves disposal."""
    proof=state.get('native_archive_proof') or {}
    topology=state.get('git_topology') or {}
    raw=topology.get('workspace_root')
    if not raw or Path(raw).is_dir() or not proof:
        return False
    from .evidence_store import load_manifest,verify_git_reachability
    if (state.get('status') not in {'ready','closed'} or state.get('inflight')
            or proof.get('path')!=raw or proof.get('work_uid')!=state['work_uuid']
            or proof.get('input_digest')!=_close_inputs(project,state)):
        raise WorkflowError('CLOSE_RECOVERY_INPUTS_CHANGED')
    adapter=CodexWorkspaceAdapter(project.root,state['project_id'])
    intent=adapter.host_action(proof['request_uid'])
    result=adapter.host_action_result(proof['request_uid'])
    if (result is None or result.action!='archive_worktree' or result.state!='confirmed'
            or result.head!=proof['head'] or result.action_digest!=intent.input_digest
            or result.attachment_identity_key!=raw):
        raise WorkflowError('NATIVE_ARCHIVE_PROOF_REQUIRED')
    manifest=load_manifest(project.root,proof['work_uid'],proof['manifest_uid'])
    if not verify_git_reachability(project.root,manifest).get('accepted'):
        raise WorkflowError('EVIDENCE_ARCHIVE_PROOF_REQUIRED')
    from .resource_registry import ResourceRegistry
    if any(row['kind']=='slot' for row in ResourceRegistry(project.root).list_for_work(state['work_uuid'])):
        raise WorkflowError('EXECUTION_UNKNOWN')
    return True
