"""Git delivery through existing work authority, verification and cleanup.

Preparation constructs immutable candidates. Only an explicitly authorized
``land``/``ship`` publishes the target. A missing candidate verification is a
pending action, never a successful delivery. The current controller is checked
again at the publication boundary.
"""
from pathlib import Path

from .core import WorkflowError
from .gate_rules import guard_inputs
from .git_operations import _git_run, git_identity
from .identity_store import IdentityStore
from .landing import (prepare_landing, land, recover_landing, load_landing,
                      record_delivery_push)
from .orchestration_types import LandingRequest, RequestRef, digest_value
from .session_context import _load_authoritative_binding, require_decision_controller, require_capability
from .workflow_support import (validate_delivery, evidence_for, _hygiene_release,
                               contract_hash, fingerprint)


def require_authorization(state,action,scope):
    if not any(row.get('action')==action and row.get('scope')==scope
               and str(row.get('quote') or '').strip()
               for row in state.get('authorizations') or []):
        raise WorkflowError('AUTHORIZATION_MISSING '+action+' '+scope)


def _actor(project,state,binding):
    current=_load_authoritative_binding(IdentityStore(project.root),binding)
    require_capability(current,'write',state['work_uuid'])
    require_capability(current,'accept',state['work_uuid'])
    require_decision_controller(IdentityStore(project.root),current,state['work_uuid'])
    return current


def delivery_callbacks(project,run_id,binding):
    """Same quality provider serves prepare, publication and recovery."""
    def check(context):
        state=project.ledger_state(run_id)
        _actor(project,state,binding)
        if context['work_uid']!=state['work_uuid']:
            raise WorkflowError('WORK_UID_MISMATCH')
        require_authorization(state,'merge',context['target_ref'])
        decision=guard_inputs(project,state,binding=binding,phase='landing')
        validate_delivery(project,state,project.paths(run_id)[1],allow_head_change=True)
        _hygiene_release(project,state)
        return {'accepted':True,'rule_version':decision['rule_version'],
                'input_digest':decision['input_digest']}

    def verify(context):
        state=project.ledger_state(run_id)
        _actor(project,state,binding)
        source=project.verification_root(state,allow_head_change=True)
        if git_identity(source).get('head')!=context['candidate_sha']:
            raise WorkflowError('LANDING_VERIFICATION_UNAVAILABLE verify the exact merged candidate '+context['candidate_sha'])
        validate_delivery(project,state,project.paths(run_id)[1],allow_head_change=True)
        receipt=evidence_for(project,state,'all',allow_head_change=True)
        return {'accepted':True,'candidate_sha':context['candidate_sha'],
                'target_ref':context['target_ref'],'expected_target_sha':context['expected_target_sha'],
                'evidence_uid':context['evidence_uid'],'receipt_id':receipt['id'],
                'fingerprint':fingerprint(source),
                'contract':contract_hash(project,state,project.paths(run_id)[1])}
    return check,verify


def handle_delivery(project,state,args,*,binding=None):
    action=args.action
    uid=args.request_uid
    if action=='status':
        record=load_landing(project.root,uid)
        if record['work_uid']!=state.get('work_uuid'):
            raise WorkflowError('WORK_UID_MISMATCH')
        return record,True
    binding=_actor(project,state,binding)
    check,verify=delivery_callbacks(project,state['id'],binding)
    if action in {'land','recover'}:
        record=load_landing(project.root,uid)
        if record['work_uid']!=state['work_uuid']:
            raise WorkflowError('LANDING_ACTOR_MISMATCH')
        require_authorization(state,'merge',record['target_ref'])
        if action=='recover':
            result=recover_landing(project.root,uid,check_gate=check,binding=binding)
        else:
            result=land(project.root,uid,check_gate=check,binding=binding)
        return result,result['state']=='landed'
    if action not in {'prepare','ship'}:
        raise WorkflowError('DELIVERY_ACTION_INVALID')
    if not args.target or not args.expected_target:
        raise WorkflowError('LANDING_TARGET_REQUIRED target and expected target SHA are required')
    require_authorization(state,'merge',args.target)
    remote=None
    if action=='ship' and getattr(args,'push',False):
        remote=getattr(args,'remote',None) or 'origin'
        if remote.startswith('-') or not remote.replace('-','').replace('_','').isalnum():
            raise WorkflowError('PUSH_REMOTE_INVALID')
        require_authorization(state,'push',remote+':'+args.target)
        configured=_git_run(project.root,'remote','get-url','--push',remote)
        if configured.returncode or not configured.stdout.strip():
            raise WorkflowError('PUSH_REMOTE_MISSING '+remote)
    guard_inputs(project,state,binding=binding,phase='landing')
    source=project.workspace_root(state,validate=False)
    identity=git_identity(source)
    candidate=getattr(args,'candidate',None) or identity.get('head')
    if not candidate:
        raise WorkflowError('LANDING_CANDIDATE_REQUIRED commit the authorized candidate first')
    # Commit remains an explicit Git action on the reviewed source. No implicit
    # ``git add .`` or unrelated index ownership is introduced by delivery.
    dirty=_git_run(source,'status','--porcelain','-uall')
    if dirty.returncode or dirty.stdout.strip():
        raise WorkflowError('LANDING_SOURCE_DIRTY commit the reviewed, authorized files before delivery')
    payload={'target_ref':args.target,'expected_target_sha':args.expected_target,
             'candidate_sha':candidate,'binding_uid':binding.binding_uid}
    request=RequestRef(uid,'delivery:'+uid,digest_value(payload),binding.project_uid,
                       state['work_uuid'],None,'landing',binding.controller_generation,
                       binding.cancel_generation,contract_digest=contract_hash(project,state,project.paths(state['id'])[1]))
    value=LandingRequest(request,binding,args.target,args.expected_target,candidate,uid+':verification')
    try:
        prepared=prepare_landing(project.root,value,workspace=source,check_gate=check,verify_candidate=verify)
    except WorkflowError as exc:
        # An exact merged candidate can require another verification workspace.
        # Preserve its intent and expose the real pending action to the caller.
        if str(exc).startswith('LANDING_VERIFICATION_UNAVAILABLE'):
            return load_landing(project.root,uid),False
        raise
    if action=='prepare':
        return prepared,prepared['state']=='prepared'
    push_stage=(record_delivery_push(project.root,uid,remote,binding=binding)
                if remote is not None else None)
    landed=land(project.root,uid,check_gate=check,binding=binding)
    if landed['state']!='landed':
        return landed,False
    if push_stage is not None and push_stage['state']!='pushed':
        require_authorization(project.ledger_state(state['id']),'push',remote+':'+args.target)
        _actor(project,project.ledger_state(state['id']),binding)
        pushed=_git_run(source,'push',remote,landed['final_sha']+':'+args.target)
        if pushed.returncode:
            record_delivery_push(project.root,uid,remote,binding=binding,error=pushed.stderr)
            return {**landed,'pending_action':'push','error':pushed.stderr},False
        record_delivery_push(project.root,uid,remote,binding=binding,pushed=True)
    if getattr(args,'cleanup',False):
        # Reuse the one close/resource recovery operation; closed is written last.
        from argparse import Namespace
        from .workflow_operations import dispatch
        result,ok=dispatch(project,Namespace(command='close',id=state['id'],
            reason='verified Git delivery '+uid,cleanup=True,abandoned=False,
            reconcile=False,allow_dirty=False),enforce_context=True)
        return {'landing':landed,'close':result},ok
    return landed,True
