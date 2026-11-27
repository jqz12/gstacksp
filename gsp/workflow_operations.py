"""workflow operations; shared business implementation, independent of CLI."""
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
from .gate_rules import (_preflight_advance, _preflight_close, _preflight_ready, _preflight_review_gate, preflight_command)
from .workflow_support import (_delivery_git_state, _hygiene_release, _independent_review_proof, _measure_quality_source, _sequential_parent_group, archive_delivery, commit_number_warnings, contract_hash, evidence_for, fingerprint, head_observation_change, integration_dependencies, parallel_required, request_action_uid, revalidation_action_uid, run_check, slug, stable_action_uid, topology_required, topology_selected, validate_delivery, validate_parallel_bindings)

def _user_update_check(target, profile, manifest_path, force=False):
    """Return a read-only GitHub release row for one managed user install."""
    prefix = target + '-user-update'
    if not manifest_path.is_file():
        return {'check': prefix, 'scope': 'user', 'status': 'NOT_CHECKED',
                'detail': 'no managed user installation manifest'}
    try:
        manifest = read(manifest_path)
        if manifest.get('scope') != target + '-user' or manifest.get('host') != target:
            return {'check': prefix, 'scope': 'user', 'status': 'NOT_CHECKED',
                    'detail': 'manifest is not a managed GSP user installation'}
        current = manifest.get('source_version') or manifest.get('version') or __version__
        from .update_check import check, source_for_manifest
        source = source_for_manifest(manifest)
        result = check(profile / 'gstacksp' / 'update-check.json', source,
                       current_version=current, force=force)
        detail = {
            'current': current,
            'latest': result.get('latest'),
            'source': result.get('source', source),
            'checked_at': result.get('checked_at'),
            'cached': bool(result.get('cached')),
            'comparison': result.get('comparison', 'unknown'),
        }
        if result.get('commit'):
            detail['commit'] = result['commit']
        if result.get('error'):
            detail['error'] = result['error']
        status = 'UPDATE_AVAILABLE' if result.get('available') is True else (
            'PASS' if result.get('status') == 'ok' else 'NOT_CHECKED')
        return {'check': prefix, 'scope': 'user', 'status': status, 'detail': detail}
    except (WorkflowError, OSError, ValueError) as exc:
        return {'check': prefix, 'scope': 'user', 'status': 'NOT_CHECKED',
                'detail': {'error': type(exc).__name__, 'message': str(exc)}}


def _github_user_upgrade(manager, optional, dry_run=False):
    """Stage a user upgrade from the latest published stable GitHub tag."""
    from .github_source import resolve_source
    from .update_check import check, source_for_manifest
    from .user_install import UserInstall
    if not manager.manifest.is_file():
        raise WorkflowError('USER_INSTALL_MISSING')
    previous = read(manager.manifest)
    source = source_for_manifest(previous)
    current = previous.get('source_version') or previous.get('version') or __version__
    update = check(manager.root / 'gstacksp' / 'update-check.json', source,
                   current_version=current, force=True)
    if update.get('status') != 'ok' or not update.get('latest'):
        raise WorkflowError('UPGRADE_SOURCE_UNAVAILABLE GitHub stable tag check failed')
    with tempfile.TemporaryDirectory(prefix='gsp-upgrade-source-') as folder:
        resolved = resolve_source(source, update['latest'], Path(folder) / 'source')
        staged = UserInstall(manager.host, manager.root,
                             source_root=resolved['path'],
                             source_version=resolved['version'],
                             source_commit=resolved['commit'],
                             source_url=resolved['source'])
        result = staged.apply('upgrade', optional, dry_run)
    result['update_check'] = {
        'source': source, 'current': current,
        'latest': update['latest'], 'commit': update.get('commit'),
        'resolved_commit': resolved['commit'],
    }
    return result


def health(root, host, force_update=False):
    rows = []
    source = Path(__file__).resolve().parents[1]
    rows.append({'check': 'python', 'status': 'PASS' if sys.version_info >= (3, 10) else 'FAIL', 'detail': sys.version.split()[0]})
    catalog_path = source / 'catalog.json'
    if catalog_path.exists() and (source / 'skills').is_dir():
        catalog = read(catalog_path)
        missing = [s['name'] for s in catalog['skills'] if not (source / 'skills' / s['name'] / 'SKILL.md').is_file()]
        rows.append({'check': 'source-skills', 'status': 'FAIL' if missing else 'PASS', 'detail': missing or len(catalog['skills'])})
    if host not in ('claude', 'codex', 'zcode', 'both'):
        raise WorkflowError('HEALTH_HOST_REQUIRED choose claude, codex, zcode, or both')
    for target in (['claude', 'codex'] if host == 'both' else [host]):
        profile = (Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') if target == 'codex'
                   else Path.home() / ('.claude' if target == 'claude' else '.zcode'))
        project_skills = (root / '.claude/skills' if target == 'claude' else
                          root / '.zcode/skills' if target == 'zcode' else root / '.agents/skills')
        locations = [('project', root, project_skills, root / '.gsp' / f'install-{target}.json'),
                     ('user', profile, profile / 'skills', profile / 'gstacksp/install.json')]
        detected = False
        user_manifest = locations[1][3]
        for scope, owner, base, manifest in locations:
            installed = sorted(p.parent.name for p in base.glob('gsp-*/SKILL.md'))
            present = bool(installed) or manifest.exists()
            prefix = target if scope == 'project' else target + '-user'
            if not present:
                rows.append({'check': prefix + '-skill-files', 'scope': scope, 'path': str(base), 'status': 'NOT_CHECKED', 'detail': 'no GstackSP installation at this scope'})
                continue
            detected = True
            rows.append({'check': prefix + '-skill-files', 'scope': scope, 'path': str(base), 'status': 'PASS' if 'gsp-workflow' in installed else 'FAIL', 'detail': installed})
            if manifest.is_file():
                try:
                    if not inside(owner, manifest) or not inside(owner, base):
                        raise WorkflowError('INSTALL_ESCAPE installation storage resolves outside scope')
                    expected = read(manifest).get('files', {})
                    if not isinstance(expected, dict) or not expected:
                        raise WorkflowError('INSTALL_MANIFEST_INVALID missing file inventory')
                    issues = []
                    for rel, expected_hash in expected.items():
                        file = owner / rel
                        if not inside(owner, file):
                            issues.append({'path': rel, 'problem': 'outside-scope'})
                        elif not file.is_file():
                            issues.append({'path': rel, 'problem': 'missing'})
                        elif digest(file) != expected_hash:
                            issues.append({'path': rel, 'problem': 'modified'})
                    rows.append({'check': prefix + '-installed-integrity', 'scope': scope, 'status': 'FAIL' if issues else 'PASS', 'detail': issues or {'verified_files': len(expected)}})
                except (WorkflowError, OSError) as exc:
                    rows.append({'check': prefix + '-installed-integrity', 'scope': scope, 'status': 'FAIL', 'detail': str(exc)})
            else:
                rows.append({'check': prefix + '-installed-integrity', 'scope': scope, 'status': 'NOT_CHECKED', 'detail': 'no installer manifest; plugin/manual files not verified'})
        if not detected:
            rows.append({'check': target + '-installation', 'status': 'FAIL', 'detail': 'no project or user GstackSP installation detected'})
        rows.append({'check': target + '-cli', 'status': 'PASS' if shutil.which(target) else 'NOT_CHECKED', 'detail': 'on PATH' if shutil.which(target) else 'not found; app host may still be available'})
        rows.append({'check': target + '-live-discovery', 'status': 'NOT_CHECKED', 'detail': 'ask the actual host to invoke gsp-health and cite this installation; file existence is not discovery proof'})
        rows.append({'check': target + '-workspace-ownership', 'status': 'NOT_CHECKED', 'detail': 'Claude hook requires plugin/hooks activation; Codex checks supported direct edits against workspace ownership; no shell sandbox'})
        rows.append(_user_update_check(target, profile, user_manifest, force=force_update))
    from .build_policy import build_policy_report
    policy = build_policy_report(root)
    rows.append({'check': 'build-policy',
                 'status': {'ok': 'PASS', 'missing': 'NOT_CHECKED', 'invalid': 'FAIL'}[policy['status']],
                 'detail': policy})
    rows.append({'check': 'browser-and-agents', 'status': 'NOT_CHECKED', 'detail': 'discover tools in the running host; no paid calls made'})
    return {'version': __version__, 'checks': rows, 'static_ok': not any(r['status'] == 'FAIL' for r in rows), 'live_verified': False}


def hook(root):
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise WorkflowError('HOOK_PAYLOAD_INVALID')
        payload.setdefault('cwd', str(Path(root).resolve()))
        # The compatibility CLI predates native event names and represents a
        # pre-edit check. Native hook payloads keep their explicit event.
        payload.setdefault('hook_event_name', 'PreToolUse')
        from .codex_hooks import handle
        return handle(payload)
    except (WorkflowError, ValueError, TypeError, KeyError, OSError) as exc:
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                      'permissionDecisionReason': str(exc)}}


def _active_dispatch_worktrees(project, state):
    from .dispatch import ACTIVE_DISPATCH
    active = set()
    file = project.meta / 'runs' / state['id'] / 'sessions' / 'dispatch.json'
    if file.is_file():
        data = read(file)
        for row in data.get('dispatches', []):
            if row.get('status') in ACTIVE_DISPATCH and row.get('worktree'):
                active.add(os.path.normcase(str(Path(row['worktree']).resolve())))
    return active


def topology_clean(project, state, task_id=None, apply=False):
    """Compatibility facade for the shared recoverable disposal operation."""
    from .topology_cleanup import clean_topology
    document = clean_topology(
        project, state, task_id, apply,
        git_run=_git_run, git_identity=git_identity,
        active_worktrees=_active_dispatch_worktrees, clock=now,
    )
    if apply and state.get('work_uuid'):
        from .resource_lifecycle import cleanup_work_resources
        # A branch/prune failure must retain the whole request's ownership.
        document['resources'] = cleanup_work_resources(
            project, state, dry_run=bool(document['pending']))
    return document












def _preflight_command_result(project, state, command, gates):
    return {'task': state.get('id'), 'status': state.get('status'), 'command': command,
            'preflight': 'blocked', 'blockers': gates,
            'next_skill': 'gsp-workflow',
            'documents': [str(project.document(state, kind)) for kind in ('design', 'plan')],
            'scope': (state.get('scope') or {}).get('mode')}


def _session_context_path():
    return str(os.environ.get('GSP_SESSION_CONTEXT') or '').strip() or None


def _require_binding_capabilities(binding, command, work_uid):
    from .session_context import require_capability
    require_capability(binding, 'write', work_uid)
    if command in SESSION_CONTEXT_REPORT_COMMANDS:
        require_capability(binding, 'report', work_uid)
    elif command in SESSION_CONTEXT_PLAN_COMMANDS:
        require_capability(binding, 'plan', work_uid)
        if binding.kind not in {'main', 'main_chat', 'integration'}:
            raise WorkflowError('SESSION_CONTROLLER_REQUIRED')
    return binding


def _require_controller_acceptance(project, state, binding):
    """Require an accepting owner to be the current authoritative controller."""
    if binding is None:
        return None
    from .identity_store import IdentityStore
    from .session_context import require_capability, require_decision_controller
    work_uid = state.get('work_uuid')
    if not isinstance(work_uid, str) or not work_uid:
        raise WorkflowError('WORK_UID_REQUIRED')
    # Bindings created before the accept capability was introduced retain the
    # single-session main path. Modern work and every non-main binding must
    # carry the explicit capability; the authoritative controller fence below
    # applies to both paths.
    legacy_main = (
        not state.get('controller_required')
        and binding.kind == 'main'
    )
    if not legacy_main:
        require_capability(binding, 'accept', work_uid)
    require_decision_controller(IdentityStore(project.root), binding, work_uid)
    return binding


def _require_cli_write_context(project, state, command, args, work_uid=None):
    """Gate an existing-work CLI mutation with durable session authority."""
    if command in SESSION_CONTEXT_READ_COMMANDS:
        return None
    if command in SESSION_CONTEXT_PROJECT_COMMANDS:
        return None
    if command == 'session-register':
        # Registration validates an explicit handle and deliberately permits a
        # legacy bare registration only in read-only mode.
        return None
    if command in {'codex-hook', 'hooks-report'} or command.startswith('hooks-'):
        return None
    expected_work = work_uid if work_uid is not None else (state or {}).get('work_uuid')
    if not isinstance(expected_work, str) or not expected_work:
        raise WorkflowError('WORK_UID_REQUIRED command targets existing work without a work UUID')
    known = (SESSION_CONTEXT_WRITE_COMMANDS |
             SESSION_CONTEXT_DOCUMENT_WRITE_COMMANDS |
             SESSION_CONTEXT_TODO_WRITE_COMMANDS |
             SESSION_CONTEXT_REPORT_COMMANDS |
             SESSION_CONTEXT_ACCEPT_COMMANDS)
    if command not in known:
        raise WorkflowError('SESSION_CONTEXT_UNSUPPORTED_COMMAND ' + command)
    return _load_cli_binding(project, state, command, args, expected_work)


def _load_cli_binding(project, snapshot, command, args, work_uid):
    """Authorize current work and actor before handle recovery can write."""
    context_path = _session_context_path()
    if not context_path:
        raise WorkflowError('SESSION_CONTEXT_REQUIRED')
    from .session_context import load_binding

    def authorize(binding, store):
        if store.root.resolve() != project.root.resolve():
            raise WorkflowError('SESSION_PROJECT_MISMATCH')
        _require_binding_capabilities(binding, command, work_uid)
        if snapshot is None:
            return
        if command in SESSION_CONTEXT_ACCEPT_COMMANDS:
            _require_controller_acceptance(project, snapshot, binding)
        if command == 'session-handoff':
            from .sessions import replay_controller_handoff
            if replay_controller_handoff(project, snapshot, args, binding) is not None:
                return
        if command in SESSION_CONTEXT_PLAN_COMMANDS:
            from .sessions import require_controller_binding
            require_controller_binding(project, snapshot, binding)
        _preflight_session_actor(project, snapshot, command, args, binding)

    return load_binding(context_path, before_recovery=authorize)


def _require_document_context(project, command, args, enforce_context):
    if not enforce_context or command not in SESSION_CONTEXT_DOCUMENT_WRITE_COMMANDS:
        return None
    if command == 'module-set':
        active = project.meta / 'active.json'
        if not active.is_file():
            raise WorkflowError('SESSION_CONTEXT_REQUIRED module-set requires an active work controller')
        state, _, _ = project.active_state()
        return _require_cli_write_context(project, state, command, args)
    from .documents import Governance
    record = Governance(project.root).get(args.work)
    return _require_cli_write_context(project, None, command, args, work_uid=record['uuid'])


def _require_todo_context(project, command, args, enforce_context):
    if not enforce_context or command not in SESSION_CONTEXT_TODO_WRITE_COMMANDS:
        return None
    # Todo authority is checked from the recorded ledger. Calling state() here
    # would refresh and invalidate a stale ready run before context rejection.
    if command == 'todo-add' and getattr(args, 'origin', None):
        state = project.ledger_state(args.origin)
        return _require_cli_write_context(project, state, command, args)
    if command in {'todo-done', 'todo-link'}:
        state = project.ledger_state(args.run)
        return _require_cli_write_context(project, state, command, args)
    # Project backlog maintenance has no work target. It remains an explicit
    # project-level operation; it cannot mutate a run's task or evidence state.
    return None


def _preflight_session_actor(project, snapshot, command, args, caller_binding):
    """Reject a foreign session actor from a read-only ledger snapshot."""
    if caller_binding is None or command not in {
        'session-dispatch', 'session-accept', 'session-clean', 'session-watch',
        'session-claim', 'session-report', 'session-brief', 'session-handoff',
    }:
        return None
    from .sessions import (
        _find,
        _preflight_report_request,
        load_registry,
        require_actor_binding,
        require_controller_binding,
        require_row_capability,
    )
    registry = load_registry(project, snapshot, create=False)
    work_uid = snapshot.get('work_uuid')
    if command in {'session-dispatch', 'session-accept', 'session-clean', 'session-watch'}:
        controller_id = registry.get('controller_session')
        controller = next(
            (row for row in registry.get('sessions', [])
             if row.get('id') == controller_id),
            None,
        )
        if controller is None:
            if command == 'session-watch':
                from .sessions import resume_pending_controller
                if resume_pending_controller(project, snapshot, caller_binding, preflight=True) is not None:
                    return caller_binding
            raise WorkflowError('CONTROLLER_LEASE_REQUIRED')
        # Establish the claimed actor before checking controller-specific
        # capabilities, so a foreign caller is diagnosed as an actor mismatch.
        require_actor_binding(caller_binding, controller, 'write', work_uid)
        if command in {'session-dispatch', 'session-accept'}:
            requested = _find(registry, args.session)
            if requested['id'] != controller_id:
                raise WorkflowError('CONTROLLER_LEASE_REQUIRED')
        action = 'accept' if command in {'session-accept', 'session-clean'} else (
            'dispatch' if command == 'session-dispatch' else 'report'
        )
        require_row_capability(project, controller, action, work_uid)
        require_controller_binding(project, snapshot, caller_binding)
        return require_actor_binding(caller_binding, controller, action, work_uid)
    if command == 'session-claim':
        row = _find(registry, args.session)
        return require_actor_binding(caller_binding, row, 'claim', work_uid)
    if command == 'session-report':
        return _preflight_report_request(
            project, snapshot, args, caller_binding, create=False,
        )
    if command == 'session-brief':
        row = _find(registry, args.session)
        return require_actor_binding(caller_binding, row, 'brief', work_uid)
    if command == 'session-handoff':
        row = _find(registry, args.from_session)
        if registry.get('controller_session') != row.get('id'):
            raise WorkflowError('CONTROLLER_SOURCE_MISMATCH')
        return require_actor_binding(caller_binding, row, 'handoff', work_uid)
    return None


def dispatch(project, args, context=None, enforce_context=False):
    if args.command in {'status','resume'} and getattr(args, 'brief', False):
        return project.brief_state(args.id), True
    if context is None:
        context = ValidationContext(project, run_id=getattr(args, 'id', None))
    previous = activate(context)
    try:
        return _dispatch(project, args, context, enforce_context)
    finally:
        deactivate(previous)


def _dispatch(project, args, context, enforce_context=False):
    command = args.command
    if command=='evidence-verify' or command=='native-workspace' and args.action=='status':
        from .resource_operations import handle_resource
        from .session_context import load_binding
        path=_session_context_path()
        if not path:
            raise WorkflowError('SESSION_CONTEXT_REQUIRED')
        actor=load_binding(path,_recover_once=True)
        return handle_resource(project,project.ledger_state(args.id),vars(args),binding=actor)
    if command=='schedule':
        from .project_scheduler import preview_schedule, usage_for_work
        if args.action=='usage':
            if not args.id:
                raise WorkflowError('WORK_ID_REQUIRED schedule usage requires --id')
            return usage_for_work(project,project.ledger_state(args.id)),True
        return preview_schedule(project,limit=args.limit),True
    if command=='delivery' and args.action=='status':
        from .delivery_operations import handle_delivery
        return handle_delivery(project,project.ledger_state(args.id),args)
    if command == 'scope':
        project.ledger_state(args.id)
        return {
            'task': args.id,
            'deprecated': True,
            'no_op': True,
            'write_grant': False,
            'message': 'directory scope is retired; workspace ownership and resource claims are checked at edit time',
        }, True
    # Authorize existing-work writes from the recorded ledger before any
    # handler can refresh and invalidate a stale ready run. A rejected context
    # must be byte-zero, including advance/ready/close and report/accept paths.
    caller_binding = None
    snapshot = None
    if enforce_context and command == 'session-register':
        snapshot = project.ledger_state(args.id)
        from .sessions import register_session
        register_session(project, snapshot, args, preflight=True)
    if (
        enforce_context
        and command in (
            SESSION_CONTEXT_WRITE_COMMANDS
            | SESSION_CONTEXT_REPORT_COMMANDS
            | SESSION_CONTEXT_ACCEPT_COMMANDS
        )
        and hasattr(args, 'id')
    ):
        snapshot = project.ledger_state(args.id)
        caller_binding = _require_cli_write_context(
            project, snapshot, command, args,
        )
    if (
        enforce_context
        and command.startswith('session-')
        and command != 'session-register'
        and command not in SESSION_CONTEXT_BOUND_READ_COMMANDS
    ):
        if snapshot is None:
            snapshot = project.ledger_state(args.id)
        if caller_binding is None:
            caller_binding = _load_cli_binding(
                project, snapshot, command, args, snapshot.get('work_uuid'),
            )
    if caller_binding is not None and snapshot is not None:
        if command == 'session-handoff':
            from .sessions import replay_controller_handoff
            replay = replay_controller_handoff(
                project, snapshot, args, caller_binding, complete=True,
            )
            if replay is not None:
                return replay, True
        from .sessions import resume_pending_controller
        caller_binding = resume_pending_controller(
            project, snapshot, caller_binding,
        )
        if command in SESSION_CONTEXT_ACCEPT_COMMANDS:
            _require_controller_acceptance(project, snapshot, caller_binding)
        elif command in SESSION_CONTEXT_PLAN_COMMANDS:
            from .sessions import require_controller_binding
            require_controller_binding(project, snapshot, caller_binding)
        _preflight_session_actor(
            project, snapshot, command, args, caller_binding,
        )
    args._gsp_binding=caller_binding
    if command in {'native-workspace','evidence-collect'}:
        from .resource_operations import handle_resource
        state=project.ledger_state(args.id)
        supplied=vars(args).copy()
        archive_proof=None
        if command=='native-workspace':
            from .native_workflow import prepare_archive,adopt_confirmed
            payload=read(Path(args.payload))
            if args.action=='confirm':
                saved=(state.get('native_requests') or {}).get(args.request_uid) or {}
                if saved.get('create_intent'):
                    payload.setdefault('create_intent',saved['create_intent'])
                    payload.setdefault('intent_key',args.request_uid)
            if args.action=='archive':
                archive_proof=prepare_archive(project,state,args,caller_binding,payload)
            supplied['payload']=payload
        if command=='evidence-collect' or args.action=='create':
            from .gate_rules import guard_inputs
            guard_inputs(project,state,binding=caller_binding,phase='evidence')
        result,ok=handle_resource(project,state,supplied,binding=caller_binding)
        if ok and command=='native-workspace':
            if args.action=='confirm':
                adopt_confirmed(project,state,args.request_uid,result)
            elif archive_proof is not None:
                state['native_archive_proof']=archive_proof
                project.write(state,'native-archive-proof',args.request_uid,
                              action_uid='native-archive-proof:'+args.request_uid)
        return result,ok
    if command=='architecture-policy':
        from .architecture_inputs import freeze_architecture_inputs
        state=project.ledger_state(args.id)
        if state.get('inflight') or state.get('status') not in {'plan','design','needs-verification'}:
            raise WorkflowError('ARCHITECTURE_POLICY_REQUIRES_PLAN return to planning before changing the baseline')
        policy=freeze_architecture_inputs(project.root,{
            'required':True,'baseline':args.baseline,'scope':args.scope,'reason':args.reason,
            'authorization_quote':args.quote})
        previous=state.get('architecture_policy')
        if previous==policy:
            return {'policy':policy,'replayed':True},True
        state.setdefault('architecture_policy_history',[]).append({'previous':previous,'reason':args.reason})
        state.update(architecture_policy=policy,plan_seal=None,task_contracts={},review=None,status='plan')
        project.write(state,command,args.reason,action_uid=request_action_uid(command,args))
        return {'policy':policy,'plan_sealed':False},True
    if command=='delivery':
        from .delivery_operations import handle_delivery
        return handle_delivery(project,project.ledger_state(args.id),args,binding=caller_binding)
    if (command=='topology-select' and args.mode=='worktree' and enforce_context
            and not getattr(args,'legacy_worktree',False)):
        state=project.ledger_state(args.id)
        if state.get('operation_schema')==2:
            from .native_workflow import plan_topology
            return plan_topology(project,state,args,caller_binding)
    if command in {'task-start', 'task-done'}:
        ledger = project.ledger_state(args.id)
        gates = preflight_command(project, ledger, command, args)
        if gates:
            return _preflight_command_result(
                project, ledger, command, gates,
            ), False
    if command.startswith(('work-','module-')) or command in ('docs-search','docs-index','docs-recover'):
        _require_document_context(project, command, args, enforce_context)
        from .documents import handle
        return handle(project,args),True
    if command == 'run-status':
        from .command_execution import progress_snapshot
        run, _ = project.paths(args.id)
        result = progress_snapshot(run, args.evidence, args.wait)
        return result, not result.get('error')
    if command == 'init':
        project.initialize(); return {'project': str(project.root)}, True
    if command in ('handoff', 'advance'):
        from .handoff import preview, advance, blocked_preview
        snapshot = project.ledger_state(args.id)
        gates = preflight_command(project, snapshot, command, args)
        if gates:
            result = blocked_preview(project, snapshot, getattr(args, 'to', None), gates)
            return result, command == 'handoff'
        state, run, _ = project.state(args.id, execution=False)
        if command == 'handoff':
            return preview(project, state, args.to), True
        marker = state.get('_command_commit') or {}
        detail = json.dumps({'to': args.to}, ensure_ascii=False)
        if (marker.get('command') == 'advance'
                and marker.get('event') == 'advance'
                and marker.get('schema') == 1
                and marker.get('detail') == detail
                and state.get('status') == args.to
                and marker.get('request_digest', request_action_uid('advance', args))
                    == request_action_uid('advance', args)):
            from .operation_journal import recover_current_project_write
            recover_current_project_write(project, state)
            project.write(state, 'advance', marker['detail'],
                          action_uid=marker['action_uid'])
            return {'task': state['id'], 'to_stage': state['status'],
                    'allowed': True, 'replayed': True}, True
        transition_state = copy.deepcopy(state)
        context.begin_phase('final')
        result, success = advance(project, state, run, args.to)
        if success:
            context.assert_unchanged()
            action_uid = request_action_uid('advance', args, transition_state)
            state['_command_commit'] = {
                'schema': 1, 'command': 'advance', 'event': 'advance',
                'request_digest': request_action_uid('advance', args),
                'action_uid': action_uid, 'detail': detail,
                'effects': {'documents_root': project.paths(state['id'])[1].relative_to(project.root).as_posix()},
            }
            project.write(state, 'advance', detail, action_uid=action_uid)
        return result, success
    if command == 'new':
        result = project.new(args)
        from .git_topology import residue
        result['residue'] = residue(project.root)
        return result, True
    if command.startswith('todo-'):
        _require_todo_context(project, command, args, enforce_context)
        project.identity()
        from .todos import Backlog
        backlog = Backlog(project.root)
        if command == 'todo-list':
            return backlog.summary(args.origin), True
        if command == 'todo-add':
            if args.origin:
                project.state(args.origin)
            return backlog.add(args), True
        if command == 'todo-reopen':
            return backlog.reopen(args.todo, args.reason), True
        if command == 'todo-migrate':
            return backlog.migrate(), True
        if command == 'todo-format':
            return backlog.format(), True
        if command == 'todo-recover':
            return backlog.recover(args.reason), True
        state, _, _ = project.state(args.run, execution=True)
        if command == 'todo-link':
            project.item(state, args.task)
            return backlog.link(args.todo, args.run, args.task), True
        if project.item(state, args.task)['status'] != 'done':
            raise WorkflowError('TASK_NOT_DONE backlog completion requires completed work and valid evidence')
        receipt = evidence_for(project, state, args.task)
        path = next(p for p in state['evidence'] if p.endswith(receipt['id'] + '/receipt.json'))
        return backlog.done(args.todo, path, args.run, args.task, args.reason, args.version), True
    if command == 'preference':
        project.identity()
        config = read(project.meta / 'config.json')
        if args.value:
            if not args.quote:
                raise WorkflowError('USER_QUOTE_REQUIRED preferences need actual user instruction')
            config['preferences'][slug(args.key)] = {'value': args.value, 'quote': args.quote, 'at': now()}
            atomic(project.meta / 'config.json', config)
        return config['preferences'].get(args.key, {'value': 'ask'}), True
    if command in ('close', 'ready'):
        snapshot = project.ledger_state(args.id)
        gates = preflight_command(project, snapshot, command, args)
        if gates:
            return _preflight_command_result(project, snapshot, command, gates), False
    if command in REPLAYABLE_STATE_COMMANDS | {'run', 'revalidate'}:
        recorded = project.ledger_state(args.id)
        from .identity_store import IdentityStoreError
        from .operation_journal import recover_current_project_write
        try:
            recover_current_project_write(project, recorded)
        except IdentityStoreError as exc:
            raise WorkflowError(str(exc)) from exc
        marker = recorded.get('_command_commit') or {}
        if (command in REPLAYABLE_STATE_COMMANDS
                and marker.get('command') == command
                and marker.get('request_digest') == request_action_uid(command, args)):
            if marker.get('schema') != 1 or not isinstance(marker.get('action_uid'), str):
                raise WorkflowError('COMMAND_REQUEST_INVALID')
            # Project.write rechecks the exact immutable request/candidate and
            # current published state before rebuilding auxiliary projections.
            project.write(recorded, command, marker['detail'], action_uid=marker['action_uid'])
            return {'task': recorded['id'], 'status': recorded['status'],
                    'event': command, 'replayed': True}, True
    native_close=False
    if command=='close':
        from .native_workflow import missing_workspace_close
        recorded=project.ledger_state(args.id)
        native_close=missing_workspace_close(project,recorded)
    try:
        state, run, doc = ((recorded,*project.paths(args.id)) if native_close else project.state(
            args.id,
            execution=command in ('task-start', 'task-done', 'task-reopen',
                                  'revalidate', 'run', 'review', 'ready'),
            allow_head_change=command == 'observe-head',
            release_scope=command == 'scope' and not args.path,
            allow_closed=command in ('close', 'topology-clean'),
            recovery=command in ('recover-run', 'topology-clean') or
            (command == 'topology-select' and getattr(args, 'rebind', False)) or
            (command == 'close' and (getattr(args, 'abandoned', False) or
                                     getattr(args, 'reconcile', False))),
            context=context,
            defer_delivery=command == 'ready'))
    except WorkflowError as exc:
        if command not in ('status', 'resume') or 'GIT_WORKSPACE_MISSING' not in str(exc):
            raise
        run, doc = project.paths(args.id)
        state = read(run / 'state.json')
        state['workspace_missing'] = True
        state['workspace_recovery'] = [
            f'topology-select {state["id"]} --mode worktree --rebind --quote "<恢复依据>"',
            f'close {state["id"]} --reconcile --reason "<关闭依据>"',
        ]
        if command == 'status':
            return state, True
        return {'state': state, 'workspace_missing': True,
                'read_next': [str(project.document(state, kind)) for kind in ('design', 'plan')],
                'next_skill': 'gsp-workflow',
                'pending_decisions': [{'kind': 'workspace_missing',
                                       'reason': '绑定的 worktree 已不存在，不能在做指纹与 Git 校验',
                                       'command': state['workspace_recovery'][1]}],
                'live_host': 'not checked'}, True
    action_state = copy.deepcopy(state)
    if command == 'topology-clean':
        return topology_clean(project, state, getattr(args, 'task', None),
                              bool(getattr(args, 'apply', False))), True
    if command == 'topology':
        return project.topology_report(state), True
    if command == 'topology-select':
        result = project.select_topology(state, args)
        project.write(state, command, args.quote,
                      action_uid=request_action_uid(command, args, action_state))
        return result, True
    if command.startswith('session-'):
        if (state.get('git_topology') or {}).get('intent') != 'cross-session':
            raise WorkflowError('SESSION_MODE_REQUIRED use --intent cross-session')
        from .sessions import (register_session, session_status, claim_session,
                               write_brief, write_report, handoff_controller)
        if command == 'session-register':
            result = register_session(project, state, args)
        elif command == 'session-status':
            return session_status(project, state), True
        elif command == 'session-claim':
            result = claim_session(project, state, args.session, args.path, args.replace, caller_binding=caller_binding)
        elif command == 'session-brief':
            result = write_brief(project, state, args, caller_binding=caller_binding)
        elif command == 'session-report':
            result = write_report(
                project, state, args, caller_binding=caller_binding,
            )
            return result, True
        elif command == 'session-accept':
            from .dispatch import accept_session_task
            from .sessions import _find, load_registry
            args.session = _find(
                load_registry(project, state, create=False), args.session,
            )['id']
            result = accept_session_task(project, state, args, caller_binding=caller_binding)
        elif command == 'session-handoff':
            result = handoff_controller(project, state, args, caller_binding=caller_binding)
        elif command in {'session-dispatch', 'session-progress', 'session-watch', 'session-clean'}:
            from .dispatch import (dispatch_tasks, session_progress, watch_session_events,
                                   clean_session_worktree)
            if command == 'session-dispatch':
                from .sessions import _find, load_registry
                args.session = _find(
                    load_registry(project, state, create=False), args.session,
                )['id']
                result = dispatch_tasks(project, state, args, caller_binding=caller_binding)
                if (result.get('workspace_group') or {}).get('mode') == 'sequential':
                    state['workspace_group'] = {
                        **{key: result['workspace_group'][key] for key in
                           ('group_uid', 'workspace_root', 'branch', 'mode')},
                        'schema': 1, 'authority': 'controller-layout-selection',
                        'project_uid': state['project_id'], 'work_uid': state['work_uuid'],
                    }
            elif command == 'session-progress':
                return session_progress(project, state), True
            elif command == 'session-watch':
                result = watch_session_events(project, state, args, caller_binding=caller_binding)
            else:
                result = clean_session_worktree(project, state, args, caller_binding=caller_binding)
        else:
            raise WorkflowError('SESSION_COMMAND_UNKNOWN')
        project.write(state, command, json.dumps(result, ensure_ascii=False),
                      action_uid=request_action_uid(command, args, action_state))
        return result, True
    if command == 'external-link':
        value = str(args.value or '').strip()
        if not re.fullmatch(r'#[0-9]+|[0-9]+', value):
            raise WorkflowError('EXTERNAL_LINK_INVALID use a concrete numeric Issue or PR ID such as #123')
        links = state.setdefault('external_links', [])
        link = {'kind': args.kind, 'value': value}
        if link not in links:
            links.append(link)
        project.write(state, command, json.dumps(link, ensure_ascii=False),
                      action_uid=stable_action_uid(command, state['id'], link['kind'], link['value']))
        return {'task': state['id'], 'external_links': links}, True
    if command == 'documents-adopt':
        candidate = {**state, 'documents': {'design': args.spec, 'plan': args.plan}}
        for kind in ('design', 'plan'):
            file = project.document(candidate, kind)
            if not file.is_file():
                raise WorkflowError('DOCUMENT_MISSING ' + str(file))
            for other_file in project.meta.glob('runs/*/state.json'):
                other = read(other_file)
                if other['id'] != state['id'] and project.document(other, kind).resolve() == file.resolve():
                    raise WorkflowError('DOCUMENT_OWNED_BY_ANOTHER_TASK')
        if state.get('inflight') or (run / 'driver/state.json').exists():
            raise WorkflowError('DOCUMENT_MIGRATION_BUSY reconcile execution/driver before migration')
        old = {kind: str(project.document(state, kind).relative_to(project.root)).replace('\\', '/') for kind in ('design', 'plan')}
        state.setdefault('document_history', []).append({'at': now(), 'documents': old,
            'hashes': {kind: digest(project.document(state, kind)) for kind in ('design', 'plan')},
            'design_approval': state.get('design_approval'), 'plan_seal': state.get('plan_seal'), 'reason': redact(args.reason)})
        for task in state['tasks']:
            if task['status'] == 'done':
                task.setdefault('history', []).append({'at': now(), 'status': 'done',
                    'evidence': task.get('evidence'), 'reason': 'Document bindings explicitly changed'})
                task['status'] = 'needs-verification'
                task.pop('evidence', None)
                task.pop('completed_at', None)
        state['documents'] = candidate['documents']
        state['design_approval'] = None
        state['plan_seal'] = None
        state['review'] = None
        state['status'] = 'design'
    elif command == 'approve':
        if state.get('status') != 'design':
            raise WorkflowError('APPROVAL_STAGE_INVALID approve is only valid during design; use decision/authorize or resume the current stage')
        design = project.document(state, 'design').read_text(encoding='utf-8')
        if len(design.strip()) < 15 or '状态：草稿；填写目标' in design or 'Draft: discuss goals, constraints' in design:
            raise WorkflowError('DESIGN_EMPTY write the actual design first')
        state['design_approval'] = {'sha256': digest(project.document(state, 'design')), 'quote': args.quote, 'at': now(), 'source': 'caller-attributed-user'}
        state['status'] = 'plan'
        if not project.document(state,'plan').exists():
            atomic(project.document(state,'plan'),f'# {state["title"]} — 实施计划\n\n规格：[spec]({project.document_link(state,"design",project.document(state,"plan").parent)})\n\n计划尚未编写。\n')
    elif command == 'close':
        if not state.get('work_uuid'):
            raise WorkflowError('CLOSE_REQUIRES_WORK_ARCHIVE task has no document archive to close')
        from .documents import Governance
        book = Governance(project.root)
        record = book.get(state['work_uuid'])
        abandoned = bool(getattr(args, 'abandoned', False) or
                         (state.get('status') == 'closed' and state.get('close_kind') == 'abandoned'))
        allow_dirty = bool(getattr(args, 'allow_dirty', False))
        cleanup = bool(getattr(args, 'cleanup', False))
        reconcile = bool(getattr(args, 'reconcile', False))
        binding = state.get('git_topology') or {}
        bound = binding.get('workspace_root')
        workspace_missing = bool(bound) and bool(binding.get('enforce')) and not Path(bound).is_dir()
        dirty_files = []
        if (not workspace_missing and bound and binding.get('effective_mode') == 'worktree'
                and Path(bound).is_dir()):
            gate = _git_run(Path(bound), 'status', '--porcelain', '-uall')
            if gate.returncode == 0 and gate.stdout.strip():
                dirty_files = [line[3:].strip() for line in gate.stdout.splitlines() if line.strip()]
                if not allow_dirty:
                    raise WorkflowError('CLOSE_WORKSPACE_DIRTY ' + ', '.join(dirty_files[:10]))
        if reconcile and record.get('stage') != 'closed':
            raise WorkflowError('CLOSE_RECONCILE_REQUIRES_CLOSED_ARCHIVE archive is not closed yet')
        if reconcile and not workspace_missing:
            raise WorkflowError('CLOSE_RECONCILE_REQUIRES_MISSING_WORKSPACE workspace still exists')
        # Validate the completion stage before performing any disposal. Closing
        # is published only after the owned resources have actually converged.
        if abandoned:
            if state.get('status') not in {'blocked', 'closed'}:
                raise WorkflowError('CLOSE_ABANDON_REQUIRES_BLOCKED close --abandoned is only valid for a blocked task')
            if record.get('stage') == 'delivered':
                raise WorkflowError('CLOSE_ABANDON_NOT_ALLOWED delivered work cannot be abandoned')
        elif state.get('status') != 'ready' and record.get('stage') != 'closed':
            raise WorkflowError('CLOSE_REQUIRES_READY close is only valid after ready or an explicitly closed archive')
        from .resource_lifecycle import cleanup_work_resources
        resources = cleanup_work_resources(project, state, dry_run=True)
        if (state.get('status') == 'closed' and resources['complete']
                and (not bound or not Path(bound).exists()
                     or binding.get('effective_mode') != 'worktree')):
            return {'task': state['id'], 'status': 'closed', 'event': 'close',
                    'replayed': True, 'resources': resources, 'cleanup': None}, True
        if cleanup and caller_binding is not None and state.get('status')!='closed' and not native_close:
            from .resource_operations import ensure_work_evidence
            ensure_work_evidence(project,state,binding=caller_binding)
        cleanup_result = topology_clean(project, state, None, apply=True) if cleanup else None
        if cleanup_result and cleanup_result.get('resources') is not None:
            resources = cleanup_result['resources']
        if (not resources['complete'] or (cleanup_result and
                (cleanup_result.get('pending') or cleanup_result.get('prune_error')
                 or any(row.get('reason') not in {'ALREADY_DISPOSED', 'EXTERNAL_OWNER'}
                        for row in cleanup_result['refused'])))):
            result = {'task': state['id'], 'status': state.get('status'),
                      'event': 'close-pending', 'code': 'RESOURCE_CLEANUP_PENDING',
                      'resources': resources, 'cleanup': cleanup_result,
                      'recovery': 'close --cleanup, or complete the listed owner/host action'}
            if cleanup:
                atomic(run / 'close-pending.json', result)
            return result, False
        if state.get('status') == 'closed':
            return {'task': state['id'], 'status': 'closed', 'event': 'close',
                    'replayed': True, 'resources': resources, 'cleanup': cleanup_result}, True
        if abandoned:
            if state.get('status') not in {'blocked', 'closed'}:
                raise WorkflowError('CLOSE_ABANDON_REQUIRES_BLOCKED close --abandoned is only valid for a blocked task')
            if record.get('stage') == 'delivered':
                raise WorkflowError('CLOSE_ABANDON_NOT_ALLOWED delivered work cannot be abandoned')
            if record.get('stage') not in {'cancelled', 'closed'}:
                book.stage(record['uuid'], 'cancelled', args.reason, run_state=state, context=context)
            state['close_kind'] = 'abandoned'
        else:
            if state.get('status') != 'ready' and record.get('stage') != 'closed':
                raise WorkflowError('CLOSE_REQUIRES_READY close is only valid after ready or an explicitly closed archive')
            if record.get('stage') != 'closed':
                book.stage(record['uuid'], 'closed', args.reason, run_state=state, context=context)
            state['close_kind'] = 'normal'
        active = project.meta / 'active.json'
        if active.exists() and read(active).get('task') == state['id']:
            active.unlink()
        state['scope'] = {'mode': 'off', 'path': None, 'quote': args.reason, 'at': now()}
        state['closed_at'] = now()
        state['close_reason'] = redact(args.reason)
        state['close_dirty_files'] = dirty_files
        state['workspace_missing'] = workspace_missing
        state['status'] = 'closed'
        project.write(state, command, args.reason,
                      action_uid=request_action_uid(command, args, action_state))
        return {'task': state['id'], 'status': 'closed', 'event': 'close',
                'active_released': True, 'workspace_missing': workspace_missing,
                'dirty_files': dirty_files, 'cleanup': cleanup_result,
                'resources': resources}, True
    elif command == 'task-add':
        slug(args.task)
        if args.task == 'all': raise WorkflowError('RESERVED_TASK_ID all is the final regression lane')
        def same_task_id(existing):
            return (existing == args.task or
                    (re.fullmatch(r'T\d+', existing) and re.fullmatch(r'T\d+', args.task)
                     and int(existing[1:]) == int(args.task[1:])))
        if any(same_task_id(t['id']) for t in state['tasks']):
            raise WorkflowError('TASK_EXISTS ' + args.task)
        depends = [d for d in args.depends.split(',') if d]
        existing = {t['id'] for t in state['tasks']}
        if any(d not in existing for d in depends):
            raise WorkflowError('DEPENDENCY_INVALID dependencies must name prior existing tasks')
        parent_task = getattr(args, 'parent_task', None)
        if parent_task and parent_task not in existing:
            raise WorkflowError('PARENT_TASK_INVALID parent task must name an existing task')
        role = getattr(args, 'role', 'implementation')
        owned_by = getattr(args, 'owned_by', None)
        integrations = [task for task in state['tasks'] if task.get('role') == 'integration']
        if len(integrations) > 1:
            raise WorkflowError('GIT_INTEGRATION_OWNER_CONFLICT only one integration task may own the parent merge point')
        if role == 'integration' and integrations:
            raise WorkflowError('GIT_INTEGRATION_OWNER_CONFLICT only one integration task may own the parent merge point')
        if owned_by and role not in {'frontend', 'backend'}:
            raise WorkflowError('INTEGRATION_OWNER_INVALID --owned-by is only valid for frontend or backend tasks')
        owner = integrations[0] if integrations else None
        if role in {'frontend', 'backend'}:
            if not owner and state.get('plan_seal'):
                raise WorkflowError('INTEGRATION_REQUIRED_FOR_LATE_TASK add --role integration with --depends covering current tasks first')
            if owner:
                if not owned_by:
                    raise WorkflowError('INTEGRATION_OWNER_REQUIRED use --owned-by ' + owner['id'])
                if owned_by != owner['id']:
                    raise WorkflowError('INTEGRATION_OWNER_INVALID --owned-by must name the unique integration task ' + owner['id'])
                if owner.get('status') == 'done':
                    raise WorkflowError('INTEGRATION_REOPEN_REQUIRED task-reopen ' + state['id'] + ' ' + owner['id'] + ' --reason "new dependent task"')
        row = {'id': args.task, 'title': args.title, 'acceptance': args.acceptance, 'depends': depends,
               'status': 'pending', 'no_tests_reason': args.no_tests_reason, 'role': role,
               'agent': getattr(args, 'agent', None), 'session': getattr(args, 'session', None),
               'parent_task': parent_task, 'topology': {}}
        if owned_by:
            row['owned_by'] = owned_by
        if role == 'integration':
            covered = integration_dependencies({'tasks': state['tasks'] + [row]}, row)
            required = {task['id'] for task in state['tasks']
                        if task.get('role') not in {'observer', 'integration'}}
            missing = sorted(required - covered)
            if missing:
                raise WorkflowError('INTEGRATION_COVERAGE_REQUIRED missing: ' + ', '.join(missing))
        state['tasks'].append(row)
        if owned_by:
            owner['depends'] = list(dict.fromkeys(list(owner.get('depends') or []) + [args.task]))
        state['plan_seal'] = None
        if state.get('status') != 'design' or state.get('design_approval'):
            state['status'] = 'plan'
    elif command == 'acceptance-enable':
        state['acceptance_protocol'] = 'dual-track'
        for task in state['tasks']:
            task.setdefault('acceptance_cards', [])
            task.setdefault('acceptance_track', {'status': 'acceptance_pending'})
        state['status'] = 'execute'
    elif command == 'acceptance-record':
        from .acceptance_reporting import record_acceptance_card
        task = project.item(state, args.task)
        card = read(project.root / args.card)
        card_id = card.get('id')
        if not card_id:
            raise WorkflowError('ACCEPTANCE_CARD_ID_REQUIRED')
        cards = task.setdefault('acceptance_cards', [])
        cards[:] = [existing for existing in cards if existing.get('id') != card_id]
        cards.append(card)
        task.setdefault('acceptance_track', {})['status'] = 'acceptance_pending'
        record_acceptance_card(project.root, state['id'], args.task, card)
        state['acceptance_protocol'] = 'dual-track'
        state['status'] = 'execute'
    elif command == 'seal-plan':
        project.valid_design(state, doc)
        if not state['tasks']:
            raise WorkflowError('TASKS_MISSING register the complete implementation plan, not only review findings')
        from .plan_quality import read_plan_source, write_plan_gate
        plan_path = project.document(state, 'plan')
        plan_text, plan_revision = read_plan_source(plan_path)
        writes = write_plan_gate(state, plan_text)
        if writes['blockers']:
            raise WorkflowError(writes['blockers'][0]['code'] + ' ' + writes['blockers'][0]['detail'])
        if getattr(args, 'check_structure', False):
            quality = writes['analysis']
            if not quality['structural_ok']:
                raise WorkflowError('PLAN_STRUCTURE_INVALID ' + json.dumps(quality['findings'], ensure_ascii=False))
        elif '计划尚未编写' in plan_text:
            raise WorkflowError('PLAN_EMPTY write the actual task plan first')
        from .convergence import seal_policies
        try:
            convergence = seal_policies(
                state,
                plan_text,
            )
        except ValueError as exc:
            raise WorkflowError(str(exc)) from exc
        if read_plan_source(plan_path)[1] != plan_revision:
            raise WorkflowError('PLAN_CHANGED_DURING_SEAL retry against the current plan')
        state['plan_seal'] = {
            'sha256': plan_revision,
            'tasks_sha256': project.task_definition_hash(state),
            'convergence': convergence,
            'write_coverage': writes['coverage'],
            'at': now(),
        }
        from .contract_store import publish_contracts
        publish_contracts(project,state)
        if not state.get('controller_required'):
            state['status'] = 'execute'
    elif command == 'check-path':
        target = project.scope_path(state, args.path)
        return {
            'preview': True,
            'write_grant': False,
            'path': args.path,
            'resolved_path': str(target),
            'workspace_root': str(project.workspace_root(state, validate=False)),
            'owner_task': state['id'],
            'conflicts': [],
            'legacy_scope_mode': (state.get('scope') or {}).get('mode'),
        }, True
    elif command == 'task-start':
        if topology_required(state) and not topology_selected(state):
            raise WorkflowError('GIT_TOPOLOGY_REQUIRED choose and record in-place, branch or worktree before implementation')
        if state.get('controller_required') and state.get('status') != 'execute':
            raise WorkflowError('CONTROLLER_REQUIRED run advance <id> --to execute before task-start')
        task = project.item(state, args.task)
        group = _sequential_parent_group(state)
        if group:
            from .sessions import load_registry, _write_session
            registry = load_registry(project, state, create=False)
            owner = (registry.get('tasks') or {}).get(task['id'], {}).get('owner_session')
            if not owner:
                raise WorkflowError('WORKSPACE_GROUP_SOURCE_WRITER_REQUIRED register the task worker first')
            registry, writer = _write_session(project, state, owner)
            controller = next((row for row in registry.get('sessions', [])
                               if row.get('id') == registry.get('controller_session')), {})
            if (writer.get('task') != task['id']
                    or (writer.get('workspace_group') or {}).get('group_uid') != group['group_uid']
                    or Path(writer['worktree']).resolve() != Path(group['workspace_root']).resolve()
                    or writer.get('branch') != group['branch']
                    or any(writer.get(key) != controller.get(key)
                           for key in ('controller_generation', 'cancel_generation'))):
                raise WorkflowError('WORKSPACE_GROUP_SOURCE_WRITER_REQUIRED')
            if caller_binding is not None:
                if caller_binding.kind in {'main', 'main_chat'}:
                    from .sessions import require_controller_binding
                    require_controller_binding(project, state, caller_binding)
                elif caller_binding.binding_uid != writer.get('binding_uid'):
                    raise WorkflowError('SESSION_ACTOR_MISMATCH ' + owner)
        from .convergence import start_guard
        try:
            start_guard(
                task, now(),
                convergence_schema=state.get('convergence_schema'),
            )
        except ValueError as exc:
            raise WorkflowError(str(exc)) from exc
        role = str(task.get('role') or 'implementation')
        intent = str((state.get('git_topology') or {}).get('intent') or 'single-task')
        if parallel_required(state) and role in {'implementation', 'frontend', 'backend', 'integration'} and not topology_selected({'git_topology': task.get('topology') or {}}):
            raise WorkflowError('GIT_TASK_TOPOLOGY_REQUIRED bind this writable task to its own branch or worktree before starting')
        if task.get('topology', {}).get('effective_mode'):
            project.workspace_root(state, task_id=args.task)
        if task['status'] == 'done': raise WorkflowError('TASK_ALREADY_DONE do not rerun completed tasks blindly')
        if any(project.item(state, d)['status'] != 'done' for d in task['depends']): raise WorkflowError('DEPENDENCIES_PENDING')
        task.setdefault('original_started_at', now())
        task['status'] = 'running'
        if 'acceptance_cards' in task:
            task.setdefault('implementation', {})['status'] = 'implementation_pending'
            task.setdefault('acceptance_track', {})['status'] = 'acceptance_pending'
        state['status'] = 'execute'
    elif command == 'run':
        return run_check(project, state, run, args)
    elif command == 'task-done':
        task = project.item(state, args.task)
        if task['status'] != 'running': raise WorkflowError('TASK_NOT_RUNNING')
        from .convergence import complete_guard
        try:
            complete_guard(
                task,
                convergence_schema=state.get('convergence_schema'),
            )
        except ValueError as exc:
            raise WorkflowError(str(exc)) from exc
        context.begin_phase('final')
        latest = evidence_for(project, state, args.task, defer_no_tests=True)
        supplied = getattr(args, 'no_tests_reason', None)
        if latest['kind'] == 'tests':
            if supplied is not None:
                raise WorkflowError('NO_TESTS_REASON_CONFLICT tests evidence is already recorded')
        else:
            reason = supplied if supplied is not None else task.get('no_tests_reason')
            reason = (reason or '').strip()
            if supplied is not None and not reason:
                raise WorkflowError('NO_TESTS_REASON_INVALID reason must be 8..300 characters')
            if not reason:
                raise WorkflowError('TESTS_REQUIRED supply an explicit --no-tests-reason for a check-only task')
            if not 8 <= len(reason) <= 300:
                raise WorkflowError('NO_TESTS_REASON_INVALID reason must be 8..300 characters')
            task['no_tests_reason'] = redact(reason)
            task.setdefault('history', []).append(
                {'status': 'done', 'source': 'task-done',
                 'no_tests_reason': task['no_tests_reason'], 'at': now()})
        context.assert_unchanged()
        task['evidence'] = latest['id']
        task['status'] = 'done'
        if 'acceptance_cards' in task:
            task.setdefault('implementation', {})['status'] = 'implementation_done'
            task.setdefault('acceptance_track', {})['status'] = 'acceptance_pending'
        task['completed_at'] = now()
        state['revalidation'] = [entry for entry in state.get('revalidation') or []
                                 if entry['task'] != args.task]
    elif command == 'task-reopen':
        if state.get('inflight'): raise WorkflowError('EXECUTION_UNKNOWN reconcile the running command before rework')
        task = project.item(state, args.task)
        if task['status'] != 'done': raise WorkflowError('TASK_NOT_DONE only completed tasks need explicit reopening')
        if not args.reason.strip(): raise WorkflowError('REWORK_REASON_REQUIRED')
        affected = {task['id']}
        while True:
            additional = {t['id'] for t in state['tasks'] if set(t['depends']) & affected}
            if additional <= affected: break
            affected |= additional
        for item in state['tasks']:
            if item['id'] not in affected: continue
            if item['status'] == 'done':
                queue = state.setdefault('revalidation', [])
                if item['id'] not in {entry['task'] for entry in queue}:
                    queue.append({'task': item['id'], 'reason': args.reason, 'event': 'task-reopen', 'at': now()})
            item.setdefault('history', []).append({'at': now(), 'status': item['status'],
                'evidence': item.get('evidence'), 'reason': args.reason, 'reopened_from': task['id']})
            item['status'] = 'pending' if item['id'] == task['id'] else 'needs-verification'
            item.pop('evidence', None)
            item.pop('completed_at', None)
        state['review'] = None
        state['status'] = 'execute'
    elif command == 'review':
        if state.get('controller_required') and state.get('status') != 'review':
            raise WorkflowError('CONTROLLER_REQUIRED run advance <id> --to review before review')
        if args.blocking < 0: raise WorkflowError('INVALID_BLOCKING_COUNT')
        text = (doc / 'review.md').read_text(encoding='utf-8')
        if '尚未审查' in text or len(text.strip()) < 20: raise WorkflowError('REVIEW_MISSING write the actual review first')
        candidate = fingerprint(project.verification_root(state))
        contract = contract_hash(project, state, doc)
        independent_proof = None
        if state.get('convergence_schema') == 1:
            from .review_evidence import evaluate_review
            try:
                evaluated = evaluate_review(text, candidate, contract, state.get('issues') or {},
                                            work_uid=state.get('work_uuid') or state['id'])
            except ValueError as exc:
                raise WorkflowError(str(exc)) from exc
            if args.blocking != evaluated['blocking']:
                raise WorkflowError('REVIEW_BLOCKING_COUNT_MISMATCH use the classified open issue count')
            if args.reviewer in {'independent', 'fresh-context'}:
                independent_proof = _independent_review_proof(project, state, text, candidate, contract, caller_binding)
            state['issues'] = evaluated['issues']
        state['review'] = {'reviewer': args.reviewer, 'blocking': args.blocking, 'sha256': digest(doc / 'review.md'), 'fingerprint': candidate, 'contract': contract, 'independent_proof': independent_proof, 'at': now()}
        state['status'] = 'review'
    elif command == 'ready':
        if state.get('controller_required') and state.get('status') != 'ready':
            raise WorkflowError('CONTROLLER_REQUIRED run advance <id> --to checking then --to ready before ready')
        context.begin_phase('final')
        fp, checks, backlog = validate_delivery(project, state, doc)
        hygiene = _hygiene_release(project, state)
        if hygiene.get('accepted'):
            state['hygiene_release'] = {**hygiene['binding'], 'reason': hygiene['reason'], 'detail': hygiene['detail'],
                                        'at': now()}
        delivered_git = _delivery_git_state(project, state)
        state['git_clean'] = delivered_git['git_clean']
        state['branch_merged'] = delivered_git['branch_merged']
        state['delivery_git'] = delivered_git
        context.assert_unchanged()
        review = state['review']
        state['status'] = 'ready'
        state['delivery_fingerprint'] = fp
        text = f'# {state["title"]} — 交付准备\n\nREADY：本地交付检查通过。外部发布动作由用户授权另行执行。\n\n'
        text += f'- 项目：`{project.root}`\n- 审查：{review["reviewer"]}（self 不等于独立审查）\n- 任务：{len(state["tasks"])} 个完成\n'
        text += f"- 工作区：{'干净' if delivered_git['git_clean'] else '有未提交改动（未阻塞）: ' + ', '.join(delivered_git['dirty_files'][:8])}\n"
        text += f"- 分支：{'已并入 ' + delivered_git['base_branch'] if delivered_git['branch_merged'] else '未合并到 ' + delivered_git['base_branch']}\n"
        if hygiene.get('accepted'):
            text += f"- 卫生残留：已接受（{hygiene['reason']}）\n"
        text += '- 未覆盖：浏览器、线上服务、独立审查和多宿主的实际覆盖以 [review.md](review.md) 和日志为准。\n\n'
        text += '| 任务 | 检查 | 实际测试数 | 证据 |\n|---|---|---|---|\n'
        for task, row in zip(state['tasks'], checks):
            receipt = project.root / next(p for p in state['evidence'] if p.endswith(row['id'] + '/receipt.json'))
            link = os.path.relpath(receipt, doc).replace('\\', '/')
            count = row['tests_ran'] if row['tests_ran'] is not None else '-'
            text += f'| {task["id"]} | {row["kind"]}: {row["status"]} (lane: {row["task"]}) | {count} | [receipt]({link}) |\n'
        for task in state['tasks']:
            if task.get('no_tests_reason'):
                text += f'\n- {task["id"]} 无自动化测试：{task["no_tests_reason"]}'
        text += f"\n自主决定：[decisions.md](decisions.md)。规格：[design]({project.document_link(state, 'design', doc)})。计划：[plan]({project.document_link(state, 'plan', doc)})。\n"
        if not (doc/'decisions.md').exists(): atomic(doc/'decisions.md','# 决策记录\n\n本任务无额外决定记录。\n')
        backlog_link = os.path.relpath(backlog['file'], doc).replace('\\', '/')
        text += f'\n项目待办：[{Path(backlog["file"]).name}]({backlog_link})；结构化未完成 {backlog["open"]} 项。\n'
        for item in backlog['items']:
            if item['status'] == 'open':
                  text += f'- {item["id"]}（{item["priority"]}）：{item["title"]}\n'
        if backlog['orphaned_records']:
            text += '- 有执行记录对应的原待办已从正文移除；请核对记录与备份，不视为已解决。\n'
        if backlog['unmanaged_content']:
            text += '- 文件含人工或历史内容，需人工核对；上述数量不代表全部待办。\n'
        archive_delivery(doc)
        atomic(doc / 'delivery.md', text)
    elif command == 'decision':
        try:
            structured = json.loads(args.text)
        except ValueError:
            structured = None
        if isinstance(structured, dict) and isinstance(structured.get('issue'), dict):
            from .review_evidence import record_issue_decision
            value = dict(structured['issue'])
            if value.get('action') == 'sol_takeover':
                from .session_context import load_binding, require_decision_controller
                from .identity_store import IdentityStore
                actor = caller_binding
                if actor is None:
                    context_path = _session_context_path()
                    if not context_path:
                        raise WorkflowError('ISSUE_CONTROLLER_BINDING_REQUIRED')
                    actor = load_binding(context_path, _recover_once=True)
                require_decision_controller(IdentityStore(project.root), actor, state['work_uuid'])
                value['controller_binding_uid'] = actor.binding_uid
            try:
                updated_issues = record_issue_decision(state.get('issues') or {}, value)
            except (ValueError, TypeError) as exc:
                raise WorkflowError(str(exc)) from exc
            if value.get('action') == 'resolve':
                candidate = fingerprint(project.verification_root(state))
                for proof in value.get('evidence') or []:
                    path = project.root / proof.get('path', '')
                    if (proof.get('path') not in state.get('evidence', [])
                            or not inside(project.root, path) or path.is_symlink()
                            or not path.is_file() or digest(path) != proof.get('sha256')
                            or candidate != proof.get('candidate_digest')):
                        raise WorkflowError('RESOLUTION_EVIDENCE_REQUIRED use current registered evidence')
                    receipt = read(path)
                    evidence_for(project, state, receipt.get('task'), current_fingerprint=candidate,
                                 current_contract=contract_hash(project, state, doc))
            state['issues'] = updated_issues
            state['review'] = None
        elif isinstance(structured, dict) and isinstance(structured.get('convergence'), dict):
            from .convergence import apply_decision
            value = structured['convergence']
            task = project.item(state, value.get('task'))
            candidate, evidence = None, None
            if value.get('action') == 'accept_slice':
                from .review_evidence import evaluate_review
                candidate = fingerprint(project.workspace_root(state, task_id=task['id']))
                receipt = evidence_for(project, state, task['id'])
                evidence = receipt['id']
                contract = contract_hash(project, state, doc)
                for key in ('spec_review', 'quality_review'):
                    path = Path(value.get(key, ''))
                    path = (path if path.is_absolute() else project.root / path).resolve()
                    if not inside(project.root, path) or not path.is_file() or path.stat().st_size < 20:
                        raise WorkflowError('SLICE_REVIEW_REQUIRED ' + key)
                    text = path.read_text(encoding='utf-8')
                    proof = _independent_review_proof(project, state, text, candidate, contract, caller_binding, task['id'])
                    try:
                        findings = evaluate_review(text, candidate, contract, state.get('issues') or {},
                            work_uid=state['work_uuid'], task_uid=task['id'])
                    except ValueError as exc:
                        raise WorkflowError(str(exc)) from exc
                    if findings['blocking']:
                        raise WorkflowError('SLICE_REVIEW_BLOCKING unresolved required findings')
                    state['issues'] = findings['issues']
                    value[key] = {'path': str(path.relative_to(project.root)),
                                  'sha256': digest(path), **proof}
            try:
                updated = apply_decision(task, value, now(), candidate_digest=candidate, evidence=evidence)
            except ValueError as exc:
                raise WorkflowError(str(exc)) from exc
            task.clear()
            task.update(updated)
        with (doc / 'decisions.md').open('a', encoding='utf-8') as stream:
            stream.write(f'\n- {now()} [{args.source}] {redact(args.text)}\n')
    elif command == 'authorize':
        state['authorizations'].append({'action': args.action, 'scope': args.scope, 'quote': args.quote, 'at': now(), 'source': 'caller-attributed-user'})
    elif command == 'authority':
        match = next((a for a in reversed(state['authorizations']) if a['action'] == args.action and a['scope'] == args.scope), None)
        if not match: raise WorkflowError('AUTHORIZATION_MISSING action and scope must match actual user instruction')
        return match, True
    elif command == 'block':
        state['blocked_from'] = state.get('status')
        state['status'] = 'blocked'; state['blocked_reason'] = args.reason
    elif command == 'unblock':
        if state.get('status') != 'blocked':
            raise WorkflowError('UNBLOCK_REQUIRES_BLOCKED')
        target = state.get('blocked_from')
        if target not in {'design', 'plan', 'execute', 'review', 'needs-verification'}:
            target = 'plan'
        state['status'] = target
        state['unblocked_at'] = now()
        state['unblock_reason'] = redact(args.reason)
    elif command == 'observe-head':
        context.begin_phase('final')
        task_id = getattr(args, 'task_id', None)
        if task_id:
            task = project.item(state, task_id)
            binding = task.get('topology') or {}
            if not binding.get('workspace_root'):
                raise WorkflowError('GIT_TASK_TOPOLOGY_REQUIRED child is not bound')
            workspace = project.workspace_root(state, validate=False, task_id=task_id)
            actual = git_identity(workspace)
            if actual.get('branch') != binding.get('branch') or actual.get('root') != str(workspace):
                raise WorkflowError('GIT_WORKSPACE_MISMATCH inspect child workspace identity')
            base_check = is_ancestor(workspace, binding['base_head'], actual['head'])
            if base_check.returncode:
                raise WorkflowError('GIT_BASE_CHANGED child no longer descends from recorded base')
            previous = {'root': binding.get('workspace_root'), 'branch': binding.get('branch'),
                        'base_head': binding.get('base_head'), 'head': binding.get('head')}
            current = {'root': actual.get('root'), 'branch': actual.get('branch'),
                       'base_head': binding.get('base_head'), 'head': actual.get('head')}
            checks = {'workspace': actual.get('root') == str(workspace),
                      'branch': actual.get('branch') == binding.get('branch'),
                      'base': base_check.returncode == 0}
            if task.get('status') == 'done':
                details = {}
                try:
                    evidence_for(project, state, task_id,
                                 current_fingerprint=fingerprint(workspace),
                                 allow_head_change=True)
                    checks['evidence'] = True
                except (WorkflowError, OSError) as exc:
                    checks['evidence'] = False
                    details['evidence'] = str(exc)
            else:
                details = {}
            if state.get('status') == 'ready':
                try:
                    project.validate_integration(state, allow_head_change=True)
                    checks['integration'] = True
                except WorkflowError:
                    checks['integration'] = False
            observation = head_observation_change(previous, current,
                                                  {'checks': checks, 'details': details})
            if not observation['identity_changed'] and not observation['verification_changed']:
                return {'task': state['id'], 'plan_task': task_id, 'head': actual.get('head'),
                        'changed': False, **observation}, True
            binding.setdefault('head_history', []).append({'previous': binding.get('head'), 'head': actual['head'], 'reason': args.reason, 'at': now()})
            binding['head'] = actual['head']
            if observation['verification_changed']:
                state['review'] = None
                if state['status'] == 'ready':
                    state['status'] = 'needs-verification'
            context.assert_unchanged()
            project.write(state, command, args.reason,
                          action_uid=request_action_uid(
                              command, args, action_state,
                              context={
                                  'task': task_id,
                                  'previous': previous,
                                  'current': current,
                              },
                          ))
            return {'task': state['id'], 'plan_task': task_id, 'head': actual['head'],
                    'changed': True, **observation}, True
        before = dict(state.get('git') or {})
        workspace = project.workspace_root(state)
        actual = git_identity(workspace)
        previous = {'root': before.get('root'), 'branch': before.get('branch'),
                    'head': before.get('head')}
        current = {'root': actual.get('root'), 'branch': actual.get('branch'),
                   'head': actual.get('head')}
        if previous == current:
            return {'task': state['id'], 'head': actual.get('head'), 'changed': False,
                    'identity_changed': False, 'verification_changed': False,
                    'preserve_content_evidence': True, 'reason': 'no-change'}, True
        preserve = state.get('status') == 'ready' and not state.get('inflight')
        checks = {'workspace': actual.get('root') == before.get('root'),
                  'branch': actual.get('branch') == before.get('branch'),
                  'delivery': preserve}
        observation = head_observation_change(previous, current, {'checks': checks})
        state['git'] = actual
        if state.get('git_topology'):
            state['git_topology']['head'] = state['git']['head']
        state.setdefault('head_history', []).append({
            'previous': previous, 'current': current,
            'reason': args.reason, 'at': now(),
            'observation': observation,
        })
        state['head_reconciliation'] = {
            'previous': before, 'current': state['git'], 'reason': args.reason, 'at': now(),
            'changed': True, 'observation': observation,
            'commit_warnings': commit_number_warnings(project.root, before.get('head'), state['git'].get('head'), state.get('number')),
        }
        if observation['verification_changed']:
            state['review'] = None
            state['status'] = 'needs-verification'
        context.assert_unchanged()
        project.write(state, command, args.reason,
                      action_uid=request_action_uid(
                          command, args, action_state,
                          context={'previous': before, 'current': actual},
                      ))
        return {'task': state['id'], 'head': actual.get('head'), 'changed': True,
                **observation}, True
    elif command == 'recover-run':
        if not state.get('inflight'): raise WorkflowError('NO_UNKNOWN_EXECUTION')
        inflight = state['inflight']
        if inflight.get('kind') == 'quality':
            from .quality_workflow import recover_work_quality
            return recover_work_quality(project, state, measure=_measure_quality_source)
        receipt_path = project.root / f".gsp/runs/{state['id']}/evidence/{inflight['id']}/receipt.json"
        if receipt_path.is_file():
            receipt = read(receipt_path)
            if receipt.get('id') == inflight['id'] and receipt.get('task') == inflight.get('task') and receipt.get('status') in ('pass', 'fail', 'failed-output', 'zero-tests', 'test-count-unknown', 'inputs-changed', 'scope-violation', 'log-truncated'):
                if receipt_path.relative_to(project.root).as_posix() not in state.get('evidence', []):
                    state.setdefault('evidence', []).append(receipt_path.relative_to(project.root).as_posix())
                state['last_recovery'] = {'execution': state.pop('inflight'), 'outcome': 'claimed-receipt', 'receipt': receipt_path.relative_to(project.root).as_posix(), 'at': now()}
                if receipt.get('status') != 'pass':
                    state['status'] = 'needs-verification'
                project.write(state, 'recover-receipt', receipt.get('status', 'unknown'),
                              action_uid=stable_action_uid('recover-receipt', receipt_path.as_posix()))
                return {'task': state['id'], 'status': receipt.get('status'), 'recovered': 'claimed-receipt', 'receipt': receipt_path.relative_to(project.root).as_posix()}, receipt.get('status') == 'pass'
        file = (project.root / args.evidence).resolve()
        if not inside(project.root, file) or not file.is_file(): raise WorkflowError('RECOVERY_EVIDENCE_REQUIRED use a local investigation report')
        state['last_recovery'] = {'execution': state.pop('inflight'), 'outcome': args.outcome, 'report': str(file), 'sha256': digest(file), 'at': now()}
        state['status'] = 'needs-verification'
        state['verification_epoch'] = state.get('verification_epoch', 0) + 1
        queue = state.setdefault('revalidation', [])
        queued = {entry['task'] for entry in queue}
        for item in state['tasks']:
            if item['status'] == 'done' and item['id'] not in queued:
                queue.append({'task': item['id'], 'reason': 'unknown execution quarantined', 'event': 'recover-run', 'at': now()})
        state['review'] = None
    elif command == 'revalidate':
        if state.get('inflight'): raise WorkflowError('EXECUTION_UNKNOWN reconcile the running command before revalidating')
        if state.get('controller_required') and state['status'] != 'execute':
            state['status'] = 'execute'
            project.write(state, 'revalidate', 'resume execution for pending revalidation',
                          action_uid=revalidation_action_uid(project, state, None, 'resume', state.get('revalidation')))
        results = []
        while True:
            state = read(run / 'state.json')
            queue = list(state.get('revalidation') or [])
            if not queue: break
            done_ids = {item['id'] for item in state['tasks'] if item['status'] == 'done'}
            ready_entries = [entry for entry in queue
                             if all(dep in done_ids for dep in project.item(state, entry['task'])['depends'])]
            if not ready_entries:
                raise WorkflowError('REVALIDATION_DEPENDENCY_BLOCKED ' + ', '.join(entry['task'] for entry in queue))
            entry = ready_entries[0]
            task_id = entry['task']
            task = project.item(state, task_id)
            if task['status'] == 'done':
                try:
                    evidence_for(project, state, task_id)
                except WorkflowError:
                    task['status'] = 'needs-verification'
                    project.write(state, 'revalidation', task_id + ' evidence stale',
                                  action_uid=revalidation_action_uid(project, state, task_id, 'evidence-stale', entry))
                    continue
            else:
                if task['status'] != 'running':
                    dispatch(project, argparse.Namespace(command='task-start', id=state['id'],
                                                         task=task_id), context=context)
                receipts = [read(project.root / path) for path in state.get('evidence', [])]
                rows = [row for row in receipts if row.get('task') == task_id]
                if not rows:
                    raise WorkflowError('REVALIDATION_COMMAND_MISSING ' + task_id)
                receipt = rows[-1]
                run_args = argparse.Namespace(command='run', id=state['id'], task=task_id,
                                              kind=receipt.get('kind', 'tests'), timeout=300, scan_timeout=120,
                                              argv=list(receipt['argv']))
                result, ok = dispatch(project, run_args, context=context)
                results.append({'task': task_id, 'status': result.get('status') if isinstance(result, dict) else None})
                if not ok:
                    state = read(run / 'state.json')
                    return {'task': state['id'], 'revalidation': 'stopped', 'failed': task_id,
                            'remaining': [item['task'] for item in state.get('revalidation') or []]}, False
                dispatch(project, argparse.Namespace(command='task-done', id=state['id'],
                                                     task=task_id), context=context)
            state = read(run / 'state.json')
            state['revalidation'] = [item for item in state.get('revalidation') or [] if item['task'] != task_id]
            project.write(state, 'revalidation', task_id + ' revalidated',
                          action_uid=revalidation_action_uid(project, state, task_id, 'revalidated', entry))
        state = read(run / 'state.json')
        return {'task': state['id'], 'revalidation': 'empty', 'results': results}, True
    elif command == 'save':
        checkpoint = {'schema': 1, 'at': now(), 'project_id': state['project_id'], 'root': str(project.root), 'git': git_identity(project.workspace_root(state)),
                      'fingerprint': fingerprint(project.workspace_root(state)), 'design': digest(project.document(state, 'design')), 'plan': digest(project.document(state, 'plan')) if project.document(state,'plan').exists() else None}
        atomic(run / 'checkpoint.json', checkpoint)
    elif command == 'resume':
        if state.get('inflight'): raise WorkflowError('EXECUTION_UNKNOWN inspect the recorded argv and evidence folder; do not redispatch blindly')
        for task in state.get('tasks', []):
            if task.get('topology'):
                project.workspace_root(state, task_id=task['id'])
        check = read(run / 'checkpoint.json') if (run / 'checkpoint.json').exists() else None
        if check:
            if check['git'] != git_identity(project.workspace_root(state)) or check['fingerprint'] != fingerprint(project.workspace_root(state)):
                raise WorkflowError('RESTORE_DIVERGED inspect changed files, branch or HEAD; saved state is not permission to overwrite')
            if check['design'] != digest(project.document(state, 'design')) or check['plan'] != (digest(project.document(state, 'plan')) if project.document(state,'plan').exists() else None):
                raise WorkflowError('RESTORE_PLAN_CHANGED')
        elif state['git'].get('head') != git_identity(project.workspace_root(state)).get('head'):
            raise WorkflowError('HEAD_CHANGED inspect before continuation')
        from .todos import Backlog
        backlog = Backlog(project.root).summary()
        pending = []
        if not state.get('design_approval'):
            pending.append({'kind': 'approval', 'reason': '当前设计没有匹配的用户批准', 'command': f"approve {state['id']} --quote \"用户原话\""})
        if topology_required(state) and not topology_selected(state):
            pending.append({'kind': 'git_topology', 'reason': '当前任务尚未选择原地、分支或 worktree', 'command': f"topology {state['id']} && topology-select {state['id']} --mode <in-place|branch|worktree> --quote \"用户原话\""})
        if state.get('inflight'):
            pending.append({'kind': 'recovery', 'reason': '上次命令结果未知，先调查并恢复', 'command': f"recover-run {state['id']} --evidence <报告路径> --outcome failed"})
        if state.get('status') == 'blocked':
            pending.append({'kind': 'blocked', 'reason': state.get('blocked_reason') or '任务已被阻塞',
                            'command': (f'unblock {state["id"]} --reason "<恢复依据>" | '
                                        f'close {state["id"]} --abandoned --reason "<放弃依据>"')})
        next_skill = {'design': 'gsp-brainstorming', 'plan': 'gsp-writing-plans', 'execute': 'gsp-execute',
                      'checking': 'gsp-review', 'review': 'gsp-review', 'ready': 'gsp-ship',
                      'needs-verification': 'gsp-review', 'blocked': 'gsp-workflow'}.get(state.get('status'), 'gsp-workflow')
        return {'state': state, 'read_next': [str(project.document(state, kind)) for kind in ('design', 'plan')] + [str(doc / f) for f in ('decisions.md', 'review.md')] + [backlog['file']], 'backlog': backlog, 'next_skill': next_skill, 'pending_decisions': pending, 'live_host': 'not checked'}, True
    elif command == 'status':
        return state, True
    else:
        raise WorkflowError('UNKNOWN_COMMAND')
    action_uid = request_action_uid(command, args, action_state)
    if command in REPLAYABLE_STATE_COMMANDS:
        effects = {'documents_root': doc.relative_to(project.root).as_posix()}
        if command == 'task-reopen':
            effects['decision'] = {'kind': 'rework', 'task': args.task,
                                   'reason': redact(args.reason), 'affected': sorted(affected)}
        state['_command_commit'] = {
            'schema': 1, 'command': command, 'event': command,
            'request_digest': request_action_uid(command, args),
            'action_uid': action_uid, 'detail': getattr(args, 'quote', '') or '',
            'effects': effects,
        }
    project.write(state, command, getattr(args, 'quote', '') or '', action_uid=action_uid)
    return {'task': state['id'], 'status': state['status'], 'event': command}, True
