"""Bind declared quality execution to one work without owning a process runtime.

The CLI supplies its existing writer-lock window and uncached source measurer.
The same execution, resource and evidence providers serve normal checks.
"""
from __future__ import annotations

import os
from pathlib import Path
import uuid

from .build_context import build_lease, command_environment, resolve_context
from .code_quality import (bound_quality_phase, evaluate_required_quality,
                           quality_contract, load_stack, run_quality)
from .core import WorkflowError, atomic, inside, read
from .evidence import EvidenceCache, QualityReportLocator
from .resource_lifecycle import register_build_resource
from .resource_registry import ResourceRegistry


def quality_build_context(project, state):
    source = project.verification_root(state)
    stack = load_stack(project.root, source_root=source)
    cargo = next((row['argv'] for row in stack['checks']
                  if Path(row['argv'][0]).name.lower() in {'cargo', 'cargo.exe'}), [])
    context = resolve_context(project, state, 'all', cargo, os.environ, source_root=source)
    return context, command_environment(os.environ, context)


def _known_terminal(row, control_root):
    raw = row.get('execution_result')
    if not isinstance(raw, str):
        return False
    path = Path(raw)
    if not path.is_absolute():
        path = Path(control_root) / path
    if not inside(control_root, path) or not path.is_file() or path.is_symlink():
        return False
    result = read(path)
    if result.get('process_alive') or result.get('interruption'):
        return False
    return (result.get('status') in {'completed', 'failed'}
            and type(result.get('returncode')) is int) or (
                result.get('status') == 'launch-failed' and not result.get('process_identity'))


def _complete_process_identity(value):
    return (
        isinstance(value, dict)
        and type(value.get('pid')) is int
        and value['pid'] > 0
        and isinstance(value.get('token'), str)
        and bool(value['token'])
    )


def _recover_quality_lease(project, pending):
    """Remove only a lease whose complete owner identities are proven dead."""
    context = pending.get('build_context') or {}
    if not isinstance(context, dict):
        raise WorkflowError(
            'QUALITY_EXECUTION_UNKNOWN original resource context is invalid'
        )
    raw = context.get('root')
    if not raw:
        return 'absent'
    lease_file = Path(raw) / '.gsp-build-lease.json'
    if not lease_file.exists():
        return 'absent'
    if lease_file.is_symlink() or not lease_file.is_file():
        raise WorkflowError(
            'QUALITY_EXECUTION_UNKNOWN original resource lease is invalid'
        )
    try:
        lease = read(lease_file)
    except (OSError, WorkflowError) as exc:
        raise WorkflowError(
            'QUALITY_EXECUTION_UNKNOWN original resource lease is unreadable'
        ) from exc
    if not isinstance(lease, dict):
        raise WorkflowError(
            'QUALITY_EXECUTION_UNKNOWN original resource lease is invalid'
        )
    if lease.get('evidence_id') != pending.get('id'):
        raise WorkflowError(
            'QUALITY_EXECUTION_UNKNOWN original resource lease changed'
        )
    controller = lease.get('controller')
    child = lease.get('child')
    if not _complete_process_identity(controller):
        raise WorkflowError(
            'QUALITY_EXECUTION_UNKNOWN original resource controller identity is unavailable'
        )
    if not _complete_process_identity(child):
        raise WorkflowError(
            'QUALITY_EXECUTION_UNKNOWN original resource child identity is unavailable'
        )
    from .hosts import is_alive
    if (is_alive(child)
            or (controller.get('pid') != os.getpid() and is_alive(controller))):
        raise WorkflowError(
            'QUALITY_EXECUTION_UNKNOWN original resource lease is still active or unknown'
        )
    try:
        lease_file.unlink()
    except FileNotFoundError:
        return 'absent'
    except OSError as exc:
        raise WorkflowError(
            'QUALITY_EXECUTION_UNKNOWN original resource lease cleanup failed'
        ) from exc
    return 'recovered'


def _canonical_empty_report(project, state, pending, folder):
    """Require the original report to prove a zero-command quality terminal."""
    expected = folder / 'report.json'
    raw = pending.get('report')
    if raw is None:
        path = expected
    elif not isinstance(raw, str) or not raw.strip():
        raise WorkflowError(
            'QUALITY_EXECUTION_UNKNOWN zero-execution report path is unavailable'
        )
    else:
        path = Path(raw)
    if not path.is_absolute():
        path = Path(project.root) / path
    if path.is_symlink():
        raise WorkflowError(
            'QUALITY_EXECUTION_UNKNOWN zero-execution report is unsafe'
        )
    try:
        path = path.resolve()
    except OSError as exc:
        raise WorkflowError('QUALITY_EXECUTION_UNKNOWN original report path is unavailable') from exc
    if path != expected.resolve() or not path.is_file():
        raise WorkflowError(
            'QUALITY_EXECUTION_UNKNOWN zero-execution report is unavailable'
        )
    try:
        report = read(path)
    except (OSError, WorkflowError) as exc:
        raise WorkflowError(
            'QUALITY_EXECUTION_UNKNOWN zero-execution report is unreadable'
        ) from exc
    if not isinstance(report, dict):
        raise WorkflowError(
            'QUALITY_EXECUTION_UNKNOWN zero-execution report is invalid'
        )
    binding = report.get('binding')
    if (report.get('quality_run_id') != pending.get('id')
            or report.get('task_id') != state.get('id')
            or report.get('status') != 'not_checked'
            or report.get('checks') != []
            or report.get('binding_status') != 'bound'
            or not isinstance(binding, dict)
            or binding.get('status') != 'bound'
            or report.get('source_fingerprint') != pending.get('source_fingerprint')
            or report.get('contract') != pending.get('contract')):
        raise WorkflowError(
            'QUALITY_EXECUTION_UNKNOWN zero-execution report does not match the original request'
        )
    return report


def run_work_quality(project, args, *, measure, unlocked):
    """Caller holds the short writer lock; ``unlocked`` fences its resumption."""
    state = project.ledger_state(args.task)
    from .gate_rules import guard_inputs
    guard_inputs(project,state,binding=getattr(args,'_gsp_binding',None),phase='execute')
    if state.get('inflight'):
        raise WorkflowError('EXECUTION_UNKNOWN recover the recorded execution before quality')
    source = project.verification_root(state)
    contract = quality_contract(project, state)
    if not contract:
        raise WorkflowError('QUALITY_CONTRACT_UNBOUND approve and seal the work contract')
    run_id = args.run_id or 'quality-' + uuid.uuid4().hex
    report_path = Path(project.root) / '.gsp/runs' / args.task / 'quality' / run_id / 'report.json'
    if (not inside(Path(project.root) / '.gsp/runs' / args.task / 'quality', report_path)
            or report_path.parent.exists()):
        raise WorkflowError('QUALITY_REQUEST_EXISTS_OR_INVALID inspect the existing result instead of relaunching')
    selected = [row['id'] for row in load_stack(project.root, args.config, source_root=source)['checks']
                if args.mode in row['modes']]
    context, environment = quality_build_context(project, state)
    resource = register_build_resource(project, state, 'all', context)
    before = measure(source)
    with build_lease(project, context, run_id) as lease:
        if resource is not None:
            ResourceRegistry(project.root).commit(resource['request_uid'], resource['owner'])
        state['inflight'] = {'id': run_id, 'task': 'all', 'kind': 'quality', 'phase': 'command',
                             'report': report_path.relative_to(project.root).as_posix(),
                             'checks': selected, 'source_fingerprint': before,
                             'contract': contract, 'mode': args.mode,
                             'config': args.config, 'build_context': context}
        project.write(state, 'quality-start', run_id, action_uid='quality-start:' + run_id)
        with unlocked(state, run_id):
            result = run_quality(project.root, args.mode, args.task, run_id, args.config,
                control_root=project.root, source_root=source, source_fingerprint=before,
                contract=contract, env=environment, verification_epoch=state.get('verification_epoch', 0),
                phase=bound_quality_phase(state, args.mode),
                on_started=lease.attach if lease is not None else None)
        result_checks = result.get('checks')
        if (not isinstance(result_checks, list)
                or not all(_known_terminal(row, project.root) for row in result_checks)):
            raise WorkflowError('QUALITY_EXECUTION_UNKNOWN retain inflight and inspect the original quality result')
        if (result.get('evidence_persisted') is not True
                or result.get('locator_published') is not True):
            raise WorkflowError('QUALITY_EVIDENCE_PERSISTENCE_UNKNOWN recover the original quality receipt')
    after = measure(source)
    if after != before or quality_contract(project, state) != contract:
        result.update(status='fail', binding_status='invalidated', error='QUALITY_INPUTS_CHANGED',
                      observed_source_fingerprint=after)
        result['binding']['status'] = 'invalidated'
        atomic(report_path, result)
        state['review'] = None
        state['status'] = 'needs-verification'
    state.pop('inflight', None)
    project.write(state, 'quality-finished', run_id + ' ' + result['status'],
                  action_uid='quality-finished:' + run_id)
    return result


def recover_work_quality(project, state, *, measure):
    """Reconcile the original persisted execution; never spawn a replacement."""
    pending = state.get('inflight') or {}
    if pending.get('kind') != 'quality':
        raise WorkflowError('QUALITY_RECOVERY_REQUEST_REQUIRED')
    folder = Path(project.root) / '.gsp/runs' / state['id'] / 'quality' / pending['id']
    checks = pending.get('checks')
    if not isinstance(checks, list):
        raise WorkflowError('QUALITY_EXECUTION_UNKNOWN original command set is unavailable')
    _recover_quality_lease(project, pending)
    empty_report = None
    if checks:
        for ident in checks:
            path = folder / 'evidence' / ident / 'command-result.json'
            if not _known_terminal({'execution_result': str(path)}, project.root):
                raise WorkflowError('QUALITY_EXECUTION_UNKNOWN original command has no verified terminal result')
        evidence_class = 'verified-process-results'
    else:
        empty_report = _canonical_empty_report(project, state, pending, folder)
        evidence_class = 'zero-execution'
    report = folder / 'report.json'
    if checks:
        cache = EvidenceCache(project.root)
        for ident in checks:
            receipt_path = folder / 'evidence' / ident / 'receipt.json'
            receipt = read(receipt_path)
            identity = receipt.get('execution_identity')
            if not cache.publish(identity, receipt_path):
                raise WorkflowError('QUALITY_EVIDENCE_PERSISTENCE_UNKNOWN failure barrier is not durable')
        if report.is_file():
            original = read(report)
            if (original.get('quality_run_id') != pending['id']
                    or original.get('task_id') != state['id']):
                raise WorkflowError('QUALITY_EVIDENCE_PERSISTENCE_UNKNOWN original report identity changed')
            if not QualityReportLocator(project.root).publish(report,
                    original['work_uid'], original['mode'], original['phase'],
                    original['verification_epoch'], original['binding_status'],
                    original['source_fingerprint'], original['contract'], original['config_digest']):
                raise WorkflowError('QUALITY_EVIDENCE_PERSISTENCE_UNKNOWN report locator is not durable')
    source = project.verification_root(state)
    fp = measure(source)
    _, environment = quality_build_context(project, state)
    validation_state = {**state, 'inflight': None}  # Original terminal receipts were verified above.
    valid = (bool(checks) and report.is_file() and fp == pending.get('source_fingerprint')
             and quality_contract(project, state) == pending.get('contract')
             and not evaluate_required_quality(project, validation_state, fp, env=environment))
    if measure(source) != fp:
        raise WorkflowError('QUALITY_INPUTS_CHANGED during recovery')
    original_request = pending.copy()
    state.pop('inflight')
    state['last_recovery'] = {'kind': 'quality', 'id': pending['id'],
                              'original_request': original_request,
                              'evidence_class': evidence_class,
                              'quality_status': (
                                  empty_report.get('status') if empty_report is not None
                                  else 'pass' if valid else 'unverified'
                              ),
                              'outcome': (
                                  'zero-execution' if empty_report is not None
                                  else 'claimed-report' if valid
                                  else 'terminal-requires-verification'
                              )}
    if empty_report is None and not valid:
        state['status'], state['review'] = 'needs-verification', None
        state['verification_epoch'] = state.get('verification_epoch', 0) + 1
    project.write(state, 'quality-recovered', pending['id'],
                  action_uid='quality-recovered:' + pending['id'])
    return {'task': state['id'], 'recovered': 'quality',
            'quality_valid': bool(valid), 'quality_status': (
                empty_report.get('status') if empty_report is not None
                else 'pass' if valid else 'unverified'
            ),
            'evidence_class': evidence_class, 'accepted': False}, True
