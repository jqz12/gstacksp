"""One provider for cheap preflight and authoritative stage/effect decisions."""
import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import contextlib
try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None
try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX
    _msvcrt = None
from . import __version__
from . import core as _core
from .validation_context import (ValidationContext, activate, current_context,
                                 deactivate, invalidate)
from .operation_journal import REPLAYABLE_STATE_COMMANDS

from .command_policies import (SESSION_CONTEXT_ACCEPT_COMMANDS, SESSION_CONTEXT_BOUND_READ_COMMANDS, SESSION_CONTEXT_DOCUMENT_WRITE_COMMANDS, SESSION_CONTEXT_PLAN_COMMANDS, SESSION_CONTEXT_PROJECT_COMMANDS, SESSION_CONTEXT_READ_COMMANDS, SESSION_CONTEXT_REPORT_COMMANDS, SESSION_CONTEXT_TODO_WRITE_COMMANDS, SESSION_CONTEXT_WRITE_COMMANDS)
from .core import (WorkflowError, atomic, digest, inside, now, read, redact)
from .git_operations import (_git_run, git_identity, is_ancestor)
from .project import (Project)
from .workflow_support import (_delivery_git_state, _hygiene_release, _independent_review_proof, _measure_quality_source, _sequential_parent_group, archive_delivery, commit_number_warnings, contract_hash, evidence_for, fingerprint, head_observation_change, integration_dependencies, parallel_required, request_action_uid, revalidation_action_uid, run_check, slug, stable_action_uid, topology_required, topology_selected, validate_delivery, validate_parallel_bindings)


from .handoff import TRANSITIONS


def guard_inputs(project,state,task_id=None,*,binding=None,phase='execute',actual_paths=()):
    """One readonly contract/architecture/ownership guard for effect providers.

    Diagnostic execution can produce Red/failed evidence; acceptance still
    requires all applicable quality obligations. A brief view grants no rights.
    """
    from .contract_store import load_contract
    from .architecture_inputs import validate_architecture_inputs
    from .orchestration_types import digest_value
    required=(state.get('operation_schema')==2 or
              bool((state.get('architecture_policy') or {}).get('required')))
    if binding is not None:
        from .identity_store import IdentityStore
        from .session_context import _load_authoritative_binding, require_capability
        binding=_load_authoritative_binding(IdentityStore(project.root),binding)
        require_capability(binding,'write',state.get('work_uuid'))
        if binding.cancelled:
            raise WorkflowError('SESSION_BINDING_CANCELLED')
    tasks=([task_id] if task_id and task_id!='all'
           else [task['id'] for task in state.get('tasks',[])])
    contracts=[]
    claims=[]
    architecture=[]
    for selected in tasks:
        contract=load_contract(project,state,selected,required=required)
        if contract is None:
            continue
        contracts.append(contract['contract_uid'])
        source=project.workspace_root(state,task_id=selected,validate=False)
        for relative in actual_paths or contract.get('writes') or ['.']:
            prefix=re.split(r'[*?\[]',str(relative))[0].rstrip('/\\') or '.'
            claims.append({'kind':'path','identity':str(Path(source)/prefix)})
        changed=list(actual_paths)
        if (contract.get('architecture') or {}).get('required'):
            changed.extend(_architecture_changes(project,state,source))
        result=validate_architecture_inputs(project.root,contract,source_root=source,
                                            actual_paths=changed,phase=phase)
        if not result['allowed']:
            _architecture_manual_review(project,state,result,binding)
        architecture.append(result)
    if phase in {'accept','landing','close'}:
        issues=state.get('issues') or {}
        if not isinstance(issues,dict):
            raise WorkflowError('ISSUE_STATE_INVALID')
        open_issues=[issue for issue in issues.values()
                     if issue.get('status')!='resolved'
                     and issue.get('blocking_current_acceptance',False)]
        if open_issues:
            raise WorkflowError('UNRESOLVED_ISSUES')
    if claims:
        from .resource_registry import ResourceRegistry, ResourceRegistryError
        registry=ResourceRegistry(project.root)
        if registry.path.is_file():
            try:
                # Duplicate paths in several contracts describe one resource.
                unique={row['identity']:row for row in claims}
                conflicts=registry.conflicts(unique.values())
            except ResourceRegistryError as exc:
                raise WorkflowError(str(exc)) from exc
            if any(row['owner']['work_uid']!=state.get('work_uuid') for row in conflicts):
                raise WorkflowError('RESOURCE_CONFLICT task inputs overlap another live work')
    facts={'work_uid':state.get('work_uuid'),'contracts':contracts,
           'architecture':architecture,'phase':phase}
    return {'allowed':True,'rule_version':1,'input_digest':digest_value(facts),
            'validation':'CHECKED' if contracts else 'LEGACY_COMPATIBLE',**facts}


def _architecture_changes(project,state,source):
    topology=state.get('git_topology') or {}
    base=topology.get('base_head') or (state.get('git') or {}).get('head')
    if not base:
        # Non-Git projects cannot prove a narrower changed set.
        return ['.']
    if not re.fullmatch(r'[a-fA-F0-9]{40,64}',base):
        raise WorkflowError('ARCHITECTURE_CHANGE_BASE_INVALID')
    paths=[]
    for arguments in [('diff','--name-only','--no-renames','-z',base,'--'),
                      ('ls-files','--others','--exclude-standard','-z')]:
        result=_git_run(source,*arguments)
        if result.returncode:
            raise WorkflowError('ARCHITECTURE_CHANGE_INPUTS_UNKNOWN')
        paths.extend(path for path in result.stdout.split('\0') if path)
    return paths


def _architecture_manual_review(project,state,result,binding):
    """Unknown languages need a current independent, input-bound assessment."""
    from .review_evidence import review_metadata
    report=project.paths(state['id'])[1]/'review.md'
    recorded=state.get('review') or {}
    if not report.is_file() or recorded.get('sha256')!=digest(report):
        raise WorkflowError('ARCHITECTURE_MANUAL_REVIEW_REQUIRED')
    text=report.read_text(encoding='utf-8-sig')
    try:
        metadata=review_metadata(text)
    except ValueError as exc:
        raise WorkflowError('ARCHITECTURE_MANUAL_REVIEW_REQUIRED') from exc
    unknown=set(result['coverage']['unknown']).intersection(result['coverage']['affected'])
    reviews=metadata.get('architecture_reviews') or []
    matched=any(isinstance(row,dict) and row.get('accepted') is True
                and row.get('input_digest')==result['input_digest']
                and row.get('baseline_digest')==result['baseline_digest']
                and unknown.issubset(set(row.get('covered_paths') or [])) for row in reviews)
    if not matched or recorded.get('blocking'):
        raise WorkflowError('ARCHITECTURE_MANUAL_REVIEW_REQUIRED')
    _independent_review_proof(project,state,text,
                              fingerprint(project.verification_root(state)),
                              contract_hash(project,state,report.parent),binding)



def _gate(code, detail, action, stage, documents=None, evidence=None):
    return {'code': code, 'detail': detail, 'required_action': action,
            'stage': stage, 'documents': documents or [], 'evidence': evidence or []}


def stage_rules(project, state, target):
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
        from .workflow_support import (parallel_required, topology_required, topology_selected, validate_parallel_bindings)
        if topology_required(state) and not topology_selected(state):
            return [_gate('GIT_TOPOLOGY_REQUIRED', 'the task has no effective Git topology selection',
                          'run topology and topology-select once before implementation', current)]
        if parallel_required(state):
            parallel = validate_parallel_bindings(state)
            ownership_codes = {'GIT_INTEGRATION_REQUIRED', 'GIT_INTEGRATION_OWNER_CONFLICT',
                               'GIT_INTEGRATION_DEPENDENCY_MISSING'}
            ownership = [item for item in parallel['blocking'] if item['code'] in ownership_codes]
            if ownership:
                first = ownership[0]
                detail = '; '.join(item.get('detail', item['code']) for item in ownership)
                return [_gate(first['code'], detail,
                              '补齐唯一 integration 任务并声明全部子任务依赖后再进入 execute', current)]
    elif target == 'review':
        pending = [t['id'] for t in state.get('tasks', []) if t.get('status') != 'done']
        if pending:
            return [_gate('TASKS_INCOMPLETE', ', '.join(pending), 'finish all registered tasks', current)]
        from .workflow_support import (validate_parallel_bindings)
        parallel = validate_parallel_bindings(state)
        if not parallel['valid']:
            first = parallel['blocking'][0]
            return [_gate(first['code'], '; '.join(item.get('detail', item['code']) for item in parallel['blocking']),
                          'bind unique child workspaces and complete the integration task', current)]
        if state.get('inflight'):
            return [_gate('EXECUTION_UNKNOWN', 'an inflight command has not been recovered', 'recover-run and revalidate', current)]
        from .workflow_support import (evidence_for)
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
        from .core import (digest)
        from .workflow_support import (fingerprint, contract_hash)
        if (review.get('sha256') != digest(project.paths(state['id'])[1] / 'review.md') or
                review.get('fingerprint') != fingerprint(project.verification_root(state)) or
                review.get('contract') != contract_hash(project, state, project.paths(state['id'])[1])):
            return [_gate('REVIEW_BLOCKED', 'review is stale', 'revalidate and review current inputs', current)]
    elif current == 'checking':
        if state.get('inflight'):
            return [_gate('EXECUTION_UNKNOWN', 'verification has an inflight command', 'recover-run and revalidate', current)]
        from .workflow_support import (validate_parallel_bindings)
        parallel = validate_parallel_bindings(state)
        if not parallel['valid']:
            first = parallel['blocking'][0]
            return [_gate(first['code'], '; '.join(item.get('detail', item['code']) for item in parallel['blocking']),
                          'complete the child and integration topology checks', current)]
        document = project.paths(state['id'])[1]
        review = state.get('review') or {}
        if not review or not (document / 'review.md').is_file():
            return [_gate('REVIEW_MISSING', 'the review report is missing',
                          'write review.md and register a non-blocking review', current)]
        if review.get('blocking'):
            return [_gate('REVIEW_BLOCKED', f"{review.get('blocking')} blocking review findings",
                          'resolve the findings and register a clean review', current)]
        from .core import (digest)
        if review.get('sha256') != digest(document / 'review.md'):
            return [_gate('REVIEW_STALE', 'review.md changed after review registration',
                          'rerun review on the current report', current)]
        from .workflow_support import (validate_delivery)
        try:
            validate_delivery(project, state, project.paths(state['id'])[1])
        except Exception as exc:
            return [_gate('DELIVERY_BLOCKED', str(exc), 'resolve verification gates before ready', current)]
    return []


def _preflight_review_gate(project, state, stage):
    review = state.get('review') or {}
    try:
        report = project.paths(state['id'])[1] / 'review.md'
    except WorkflowError:
        report = None
    if not review or report is None or not report.is_file():
        return _gate('REVIEW_MISSING', 'the review report is missing',
                     'write review.md and register a non-blocking review', stage)
    if review.get('blocking'):
        return _gate('REVIEW_BLOCKED', f"{review.get('blocking')} blocking review findings",
                     'resolve the findings and register a clean review', stage)
    try:
        changed = review.get('sha256') != digest(report)
    except OSError:
        changed = True
    if changed:
        return _gate('REVIEW_STALE', 'review.md changed after review registration',
                     'rerun review on the current report', stage)
    return None


def _preflight_close(project, state, args, gate):
    current = state.get('status')
    if state.get('inflight'):
        return [gate('EXECUTION_UNKNOWN', 'an inflight execution is unresolved',
                     'recover its recorded request before closing or releasing resources', current)]
    if not state.get('work_uuid'):
        return [gate('CLOSE_REQUIRES_WORK_ARCHIVE',
                     'the task has no document archive to close',
                     'create or link a work archive before closing', current)]
    if current == 'closed':
        return []
    from .documents import WORK_STAGE_ALLOWED, work_record_snapshot
    record = work_record_snapshot(project.root, state['work_uuid'])
    abandoned = bool(getattr(args, 'abandoned', False))
    reconcile = bool(getattr(args, 'reconcile', False))
    if abandoned:
        if current != 'blocked':
            return [gate('CLOSE_ABANDON_REQUIRES_BLOCKED',
                         'close --abandoned is only valid for a blocked task',
                         'unblock the task or use a normal ready close', current)]
        if record and record.get('stage') == 'delivered':
            return [gate('CLOSE_ABANDON_NOT_ALLOWED',
                         'delivered work cannot be abandoned',
                         'close the delivered archive normally', current)]
        if (record and record.get('stage') != 'cancelled' and
                'cancelled' not in WORK_STAGE_ALLOWED.get(record.get('stage'), set())):
            return [gate('WORK_TRANSITION_INVALID',
                         f"archive stage {record.get('stage')} cannot be cancelled",
                         'inspect the work archive stage before abandoning', current)]
        return []
    if reconcile:
        if record and record.get('stage') != 'closed':
            return [gate('CLOSE_RECONCILE_REQUIRES_CLOSED_ARCHIVE',
                         'the document archive is not closed yet',
                         'close the work archive before reconciling', current)]
        binding = state.get('git_topology') or {}
        bound = binding.get('workspace_root')
        if bound and Path(bound).is_dir():
            return [gate('CLOSE_RECONCILE_REQUIRES_MISSING_WORKSPACE',
                         'the bound workspace still exists',
                         'remove or rebind the workspace before reconciling', current)]
        return []
    if record and record.get('stage') == 'closed':
        return []
    if current != 'ready':
        return [gate('CLOSE_REQUIRES_READY',
                     'close is only valid after ready or an explicitly closed archive',
                     'finish the delivery workflow before closing', current)]
    if record:
        if 'closed' not in WORK_STAGE_ALLOWED.get(record.get('stage'), set()):
            return [gate('WORK_TRANSITION_INVALID',
                         f"archive stage {record.get('stage')} cannot be closed",
                         'inspect the work archive stage before closing', current)]
        if not record.get('closure_condition'):
            return [gate('WORK_CLOSURE_CONDITION_REQUIRED',
                         'the work archive has no closure condition',
                         f"work-stage {record.get('uuid')} closed --reason \"<理由>\" "
                         '--closure-condition "<验收条件>"', current,
                         documents=[str(project.paths(state['id'])[1] / 'delivery.md')])]
    return []


def _preflight_ready(project, state, gate):
    current = state.get('status')
    if state.get('controller_required') and current != 'ready':
        return [gate('CONTROLLER_REQUIRED',
                     'run advance --to checking then --to ready before ready',
                     'advance through the controller stages', current)]
    if state.get('inflight'):
        return [gate('EXECUTION_UNKNOWN',
                     'an inflight command has not been recovered',
                     'recover-run and revalidate', current)]
    if state.get('revalidation'):
        return [gate('REVALIDATION_PENDING',
                     ', '.join(entry['task'] for entry in state['revalidation']),
                     'run revalidate before ready', current)]
    tasks = state.get('tasks') or []
    pending = [task['id'] for task in tasks if task.get('status') != 'done']
    if not tasks or pending:
        return [gate('TASKS_INCOMPLETE', ', '.join(pending) or 'no tasks',
                     'finish all registered tasks', current)]
    review_gate = _preflight_review_gate(project, state, current)
    return [review_gate] if review_gate else []


def _preflight_advance(project, state, target, gate):
    current = state.get('status')
    if target is None:
        return []
    from .handoff import TRANSITIONS
    if target == current and current in ('execute', 'review', 'checking', 'ready', 'ship'):
        return []
    expected = TRANSITIONS.get(current)
    if current == 'needs-verification' and target == 'execute':
        expected = 'execute'
    if current in ('checking', 'ready') and target == 'review':
        expected = 'review'
    if target != expected:
        return [gate('STAGE_ORDER_INVALID', f'{current} cannot advance directly to {target}',
                     f'advance to {expected}' if expected else 'resume the current task', current)]
    tasks = state.get('tasks') or []
    if target == 'execute':
        if not tasks:
            return [gate('TASKS_MISSING', 'no implementation tasks are registered',
                         'task-add the complete plan', current)]
        doc = project.paths(state['id'])[1]
        try:
            project.valid_design(state, doc)
        except (WorkflowError, OSError) as exc:
            return [gate('DESIGN_NOT_APPROVED', str(exc),
                         'approve the current design', current)]
        try:
            project.valid_plan(state, doc)
        except (WorkflowError, OSError) as exc:
            return [gate('PLAN_NOT_SEALED', str(exc),
                         'plan-check then seal-plan', current)]
        if topology_required(state) and not topology_selected(state):
            return [gate('GIT_TOPOLOGY_REQUIRED',
                         'the task has no effective Git topology selection',
                         'run topology and topology-select once before implementation', current)]
        if parallel_required(state):
            parallel = validate_parallel_bindings(state)
            ownership_codes = {'GIT_INTEGRATION_REQUIRED', 'GIT_INTEGRATION_OWNER_CONFLICT',
                               'GIT_INTEGRATION_DEPENDENCY_MISSING'}
            ownership = [item for item in parallel['blocking'] if item['code'] in ownership_codes]
            if ownership:
                first = ownership[0]
                detail = '; '.join(item.get('detail', item['code']) for item in ownership)
                return [gate(first['code'], detail,
                             '补齐唯一 integration 任务并声明全部子任务依赖后再进入 execute', current)]
    elif target == 'review':
        pending = [task['id'] for task in tasks if task.get('status') != 'done']
        if pending:
            return [gate('TASKS_INCOMPLETE', ', '.join(pending),
                         'finish all registered tasks', current)]
        if state.get('inflight'):
            return [gate('EXECUTION_UNKNOWN',
                         'an inflight command has not been recovered',
                         'recover-run and revalidate', current)]
    elif target == 'ready':
        if state.get('inflight'):
            return [gate('EXECUTION_UNKNOWN',
                         'an inflight command has not been recovered',
                         'recover-run and revalidate', current)]
        pending = [task['id'] for task in tasks if task.get('status') != 'done']
        if not tasks or pending:
            return [gate('TASKS_INCOMPLETE', ', '.join(pending) or 'no tasks',
                         'finish all registered tasks', current)]
        review_gate = _preflight_review_gate(project, state, current)
        return [review_gate] if review_gate else []
    return []


def preflight_command(project, state_snapshot, command, args):
    """Return only cheap, already-decidable blockers for one command.

    An empty list means the caller must continue through the authoritative
    validation path.  This function never writes state, launches Git, or scans
    source.  It can therefore reject a missing closure condition or review
    report without paying for a full delivery check.
    """
    if command in {'task-start', 'task-done'}:
        from .convergence import complete_guard, start_guard
        task = Project.item(state_snapshot, args.task)
        try:
            if command == 'task-start':
                start_guard(
                    task, now(),
                    convergence_schema=state_snapshot.get('convergence_schema'),
                )
            else:
                complete_guard(
                    task,
                    convergence_schema=state_snapshot.get('convergence_schema'),
                )
        except ValueError as exc:
            return [_gate(str(exc).split()[0], str(exc),
                          'current controller: split the task, accept a proven slice, or record a bounded convergence decision',
                          state_snapshot.get('status'))]
        return []
    if command not in ('close', 'ready', 'advance', 'handoff'):
        return []
    if command in ('close', 'ready', 'advance') and (
            (project.meta / 'document-transaction.json').exists() or
            (project.meta / 'document-migration.json').exists()):
        return [_gate('DOCUMENT_TRANSACTION_PENDING',
                      'a document operation needs recovery first',
                      'run docs-recover or docs-move-recover', state_snapshot.get('status'))]

    def gate(code, detail, action, stage, documents=None, evidence=None):
        return _gate(code, detail, action, stage, documents, evidence)

    if command == 'close':
        return _preflight_close(project, state_snapshot, args, gate)
    if command == 'ready':
        return _preflight_ready(project, state_snapshot, gate)
    return _preflight_advance(project, state_snapshot, getattr(args, 'to', None), gate)
