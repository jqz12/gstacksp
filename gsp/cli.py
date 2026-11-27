"""cli; shared business implementation, independent of CLI."""
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

from .command_policies import (parser)
from .core import (WorkflowError, read)
from .project import (Project)
from .workflow_locks import (inspect_lock, locked, recover_lock, suspend_writer_lock)
from .workflow_operations import (_github_user_upgrade, _require_cli_write_context, dispatch, health, hook)
from .workflow_support import (_brief_control_root, _format_brief_state, _ledger_state_at, _measure_quality_source, _quality_execution_window, slug)

def install_notice(scope, host, paths):
    # Human guidance goes to stderr; stdout stays a machine-readable JSON result.
    print('安装范围 / Installation scope:\n'
          '  --scope project: 项目级，仅当前目标项目 / this project only.\n'
          '  --scope user: 用户全局级，当前用户多个项目共用 / current user across projects.\n'
          f'当前选择 / Selected: {scope}; host={host}\n' +
          '\n'.join('  技能位置 / Skills: ' + str(path) for path in paths) + '\n'
          '用户全局安装不会新建目标项目技能目录 / User install does not create target-project skill directories.\n'
          '已有项目技能目录不会自动删除 / Existing project skill directories are not automatically removed.\n'
          '规格、计划和证据仍保存于各目标项目 / Task documents and evidence stay in each project.', file=sys.stderr)


def _emit_telemetry(project, args, result, success, event_name=None):
    """Write a small, sanitized lifecycle event without changing command results."""
    try:
        from .telemetry import append_event
        observed = {}
        if isinstance(result, dict):
            for key in ('status', 'event', 'next_skill', 'blocking', 'user_decision_required', 'decision_kind'):
                if key in result:
                    observed[key] = result[key]
            route = result.get('route') or result.get('git_health')
            if isinstance(route, dict):
                observed['route'] = {key: route.get(key) for key in ('route_id', 'status', 'next_skill', 'blocking', 'user_decision_required', 'decision_kind') if key in route}
            if result.get('id') and result.get('status'):
                observed['task_status'] = result.get('status')
        task_id = getattr(args, 'id', None)
        payload = {
            'project_id': None,
            'task_id': task_id,
            'host': 'codex' if os.environ.get('CODEX_HOME') or os.environ.get('CODEX_THREAD_ID') else 'unknown',
            'gsp_version': __version__,
            'event': event_name or getattr(args, 'command', 'unknown'),
            'skill': result.get('skill') if isinstance(result, dict) else None,
            'expected': result.get('expected') if isinstance(result, dict) else None,
            'observed': observed,
            'status': 'pass' if success else 'fail',
            'observation_status': 'observed',
            'source': 'gsp-cli',
            'decision_required': observed.get('user_decision_required', False),
            'deviation': result.get('deviation') if isinstance(result, dict) else None,
            'evidence': result.get('evidence', []) if isinstance(result, dict) else [],
        }
        if project.meta.joinpath('project.json').exists():
            try:
                payload['project_id'] = read(project.meta / 'project.json').get('id')
            except WorkflowError:
                pass
        append_event(project.root, payload)
    except Exception:
        # Observability must never turn a successful workflow command into a failure.
        pass


def main(argv=None):
    values = list(sys.argv[1:] if argv is None else argv)
    split = values.index('--') if '--' in values else len(values)
    args = parser().parse_args(values[:split])
    args.argv = values[split + 1:]
    project = None
    try:
        if args.command == 'codex-hook':
            from .codex_hooks import run
            print(json.dumps(run(),ensure_ascii=False));return 0
        if args.command == 'hooks-report':
            from .codex_hooks import report
            print(json.dumps(report(args.project),ensure_ascii=False,indent=2));return 0
        if args.command.startswith('hooks-'):
            from .codex_hook_install import HookInstall
            manager=HookInstall(args.profile)
            action=args.command.removeprefix('hooks-')
            result=manager.status() if action=='status' else manager.recover(args.dry_run) if action=='recover' else manager.apply(action,args.dry_run)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 1 if action=='status' and result.get('installed') and not result.get('integrity_ok') else 0
        if args.command in {'status','resume'} and getattr(args, 'brief', False):
            root = _brief_control_root(args.project)
            result = _format_brief_state(_ledger_state_at(root, args.id))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        project = Project(args.project)
        if args.command == 'orchestration-migrate':
            from .orchestration_migration import preview_migration
            from .orchestration_types import MigrationManifest
            from .migration_apply import (stage_migration, apply_migration, resume_migration,
                                           restore_migration, adopt_legacy_run)
            if args.action == 'preview':
                result = preview_migration(project.root).to_dict()
            elif not args.apply:
                result = {'dry_run': True, 'action': args.action, 'applied': False,
                          'pending_action': 'inspect the preview; use --apply for this explicit maintenance action'}
            elif args.action in {'stage', 'apply'}:
                if not args.manifest:
                    raise WorkflowError('MIGRATION_MANIFEST_REQUIRED use the original preview JSON')
                try:
                    manifest = MigrationManifest.from_dict(read(Path(args.manifest)))
                except (TypeError, ValueError, KeyError) as exc:
                    raise WorkflowError('MIGRATION_MANIFEST_CHANGED invalid manifest') from exc
                operation = stage_migration if args.action == 'stage' else apply_migration
                result = operation(project.root, manifest, request_uid=args.request_uid)
            elif args.action == 'adopt':
                if not args.legacy_run or not args.new_run or not args.request_uid:
                    raise WorkflowError('LEGACY_ADOPTION_REQUEST_REQUIRED --legacy-run, --new-run and --request-uid')
                result = adopt_legacy_run(project.root, args.legacy_run, args.new_run, args.request_uid)
            else:
                operation = resume_migration if args.action == 'resume' else restore_migration
                result = operation(project.root, request_uid=args.request_uid)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == 'session-bind':
            from .session_context import issue_cli_binding
            with locked(project.root):
                result = issue_cli_binding(project, args)
            _emit_telemetry(project, args, result, True, 'session-bind')
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == 'plan-check':
            from .plan_quality import inspect_plan
            result = inspect_plan(project, args.id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['structural_ok'] else 1
        if args.command.startswith('drive-'):
            from .driver import handle
            print(json.dumps(handle(project, args), ensure_ascii=False, indent=2))
            return 0
        if args.command == 'inspect-lock':
            result = inspect_lock(project.root); print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
        if args.command == 'recover-lock':
            result = recover_lock(project.root, args.evidence, args.reason); print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
        if args.command == 'hook':
            print(json.dumps(hook(project.root), ensure_ascii=False)); return 0
        if args.command == 'source-resolve':
            from .github_source import resolve_source
            from .version_store import VersionStore
            from .release_manifest import build_manifest
            result = resolve_source(args.source, args.version, args.target, args.timeout)
            if args.manifest:
                manifest = build_manifest(args.target, args.source, args.version, result['commit'])
                manifest_path = Path(args.manifest)
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                result['manifest'] = str(manifest_path.resolve())
            if args.store:
                VersionStore(args.store).switch(args.version, args.target)
                result['store'] = str(Path(args.store).resolve())
            print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
        if args.command == 'version-status':
            from .version_store import VersionStore
            store = VersionStore(args.store)
            current = store.current()
            print(json.dumps({'current': current, 'modified': store.modified()}, ensure_ascii=False, indent=2)); return 0
        if args.command == 'version-rollback':
            from .version_store import VersionStore
            store = VersionStore(args.store)
            store.rollback(args.version)
            print(json.dumps({'current': store.current(), 'modified': store.modified()}, ensure_ascii=False, indent=2)); return 0
        if args.command in ('docs-move','docs-move-recover'):
            from .document_migration import move,recover
            if args.command=='docs-move' and not args.apply:
                result=move(project.root,args.source,args.destination)
            else:
                with locked(project.root):
                    result=recover(project.root) if args.command=='docs-move-recover' else move(project.root,args.source,args.destination,True)
            print(json.dumps(result,ensure_ascii=False,indent=2));return 0
        if args.command == 'health':
            if args.report_dir and not (args.documents or args.documents_only): raise WorkflowError('DOCUMENT_AUDIT_REQUIRED --report-dir needs --documents')
            result = health(project.root, args.host, force_update=args.force_update_check) if not args.documents_only else {'static_ok':True,'checks':[],'live_verified':False}
            # Health is the workflow entry point. Attach the compact Git
            # handoff so telemetry records the route for the next skill.
            from .document_health import git_route
            result['route'] = git_route(project.invocation_root)
            if args.documents or args.report_dir:
                from .document_health import audit
                report=audit(project.root,args.report_dir)
                result['documents']={key:report[key] for key in ('report_markdown','report_json','inventory','integrity_ok','checks')}
                result['documents']['delta_counts']={key:len(report['delta'][key]) for key in ('new','resolved','persisting')}
                result['static_ok'] &= report['integrity_ok']
            _emit_telemetry(project, args, result, bool(result['static_ok']), 'health')
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result['static_ok']:
                return 1
            if args.strict and result.get('route', {}).get('blocking'):
                return 2
            return 0
        if args.command == 'observe':
            from .telemetry import build_report
            result = build_report(project.root, args.run_id)
            _emit_telemetry(project, args, result, True, 'observe_report')
            print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
        if args.command == 'quality':
            from .code_quality import run_quality
            slug(args.task)
            with locked(project.root):
                state_path = project.meta / 'runs' / args.task / 'state.json'
                if state_path.is_file():
                    from .quality_workflow import run_work_quality
                    args.id = args.task
                    snapshot = project.ledger_state(args.task)
                    args._gsp_binding=_require_cli_write_context(project, snapshot, 'quality', args)
                    result = run_work_quality(project, args, measure=_measure_quality_source,
                        unlocked=lambda state, uid: _quality_execution_window(project, state, uid))
                else:
                    with suspend_writer_lock(project.root):
                        result = run_quality(project.root, args.mode, args.task, args.run_id, args.config)
            _emit_telemetry(project, args, result, result['status'] in {'pass', 'warn'}, 'quality')
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['status'] in {'pass', 'warn'} else 1
        if args.command in ('install','upgrade','uninstall','install-recover'):
            if args.scope == 'user':
                from .user_install import UserInstall
                manager = UserInstall(args.host, args.profile)
                if args.command == 'install':
                    install_notice('user', args.host, [manager.root / 'skills'])
                if args.command == 'upgrade' and args.source == 'github':
                    result = _github_user_upgrade(manager, args.optional, args.dry_run)
                else:
                    result = manager.recover(args.dry_run) if args.command=='install-recover' else manager.apply(args.command,args.optional,args.dry_run)
                print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
            if args.command != 'install' or args.profile or args.dry_run:
                raise WorkflowError('USER_SCOPE_REQUIRED maintenance/dry-run/profile requires --scope user')
            from .install import install
            targets = ['claude', 'codex'] if args.host == 'both' else [args.host]
            install_notice('project', args.host, [project.root / ('.claude/skills' if host == 'claude' else '.zcode/skills' if host == 'zcode' else '.agents/skills') for host in targets])
            result = install(project.root, args.host, args.optional)
            print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
        if args.command == 'run-status':
            from .command_execution import progress_snapshot
            run, _ = project.paths(args.id)
            result = progress_snapshot(run, args.evidence, args.wait)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get('found') and not result.get('error') else 1
        if args.command == 'status' and getattr(args, 'brief', False):
            result, success = dispatch(project, args, enforce_context=True)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if success else 1
        if args.command in ('status', 'resume') and (project.meta / 'writer.lock').exists():
            lock_info = inspect_lock(project.root)
            if args.command == 'status':
                result, success = dispatch(project, args, enforce_context=True)
                result = {'state': result, 'writer_lock': lock_info}
                print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
            result = {'status': 'blocked', 'code': 'WORKFLOW_BUSY', 'writer_lock': lock_info,
                      'recovery': 'inspect-lock then recover-lock with an investigation report'}
            print(json.dumps(result, ensure_ascii=False, indent=2)); return 1
        from .command_policies import command_policy
        if command_policy(args.command,args).readonly:
            result,success=dispatch(project,args,enforce_context=True)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0 if success else 1
        with locked(project.root):
            result, success = dispatch(project, args, enforce_context=True)
        _emit_telemetry(project, args, result, success)
        if not success:
            # Keep the structured preview on stdout, while giving shell and
            # human callers the same actionable error channel as exceptions.
            blockers = result.get('blockers') if isinstance(result, dict) else None
            first = blockers[0] if blockers else {}
            code = first.get('code') if isinstance(first, dict) else None
            detail = first.get('detail') if isinstance(first, dict) else None
            message = ' '.join(str(part) for part in (code, detail) if part)
            print('GSP_ERROR: ' + (message or 'workflow command was blocked'), file=sys.stderr)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if success else 1
    except (WorkflowError, OSError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        from .command_policies import command_policy
        if project is not None and args.command != 'orchestration-migrate' and not command_policy(args.command,args).readonly:
            _emit_telemetry(project, args, {'status': 'failed', 'error_type': type(exc).__name__}, False, 'error')
        print(f'GSP_ERROR: {exc}', file=sys.stderr)
        return 1
