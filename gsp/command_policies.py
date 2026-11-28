"""command policies; shared business implementation, independent of CLI."""
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



def parser():
    p = argparse.ArgumentParser(description='GstackSP: local task records, checks and handoff. No model or remote mutation is executed implicitly.')
    p.add_argument('--project', default='.', help='absolute or relative target project directory')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    q=sub.add_parser('delivery',help='prepare, inspect, land or recover one verified Git candidate')
    q.add_argument('id')
    q.add_argument('action',choices=['prepare','land','recover','status','ship'])
    q.add_argument('--request-uid',required=True)
    q.add_argument('--target',help='fully qualified target ref, e.g. refs/heads/main')
    q.add_argument('--expected-target',help='expected target commit; changed targets require a new candidate')
    q.add_argument('--candidate',help='committed source SHA; defaults to bound workspace HEAD')
    q.add_argument('--push',action='store_true')
    q.add_argument('--remote',default='origin')
    q.add_argument('--cleanup',action='store_true')
    q=sub.add_parser('architecture-policy',help='bind a reviewed architecture baseline before sealing the plan')
    q.add_argument('id')
    q.add_argument('--baseline',required=True)
    q.add_argument('--scope',action='append',default=[])
    q.add_argument('--reason',required=True)
    q.add_argument('--quote',required=True,help='actual instruction authorizing this baseline adoption/reset')
    q=sub.add_parser('native-workspace',help='plan/confirm Codex-owned workspace actions from exact host receipts')
    q.add_argument('id')
    q.add_argument('action',choices=['create','archive','restore','confirm','status'])
    q.add_argument('--request-uid',required=True)
    q.add_argument('--payload',required=True,help='JSON action/receipt input; never a fabricated host success')
    q.add_argument('--task',default='all')
    for name in ('evidence-collect','evidence-verify'):
        q=sub.add_parser(name,help='collect or verify original evidence without asserting quality')
        q.add_argument('id')
        q.add_argument('--request-uid',required=True)
        q.add_argument('--payload',required=True)
        q.add_argument('--task',default='all')
        q.add_argument('--source',action='append',default=[])
        q.add_argument('--commit',action='append',default=[])
    q=sub.add_parser('schedule',help='readonly flat project queue and native usage')
    q.add_argument('action',choices=['preview','usage'])
    q.add_argument('--limit',type=int,default=3)
    q.add_argument('--id')
    q = sub.add_parser('plan-check', help='read-only bound plan structure check; semantic review is separate')
    q.add_argument('id')
    for name in ('handoff', 'advance'):
        q = sub.add_parser(name, help='inspect or advance one controlled workflow stage')
        q.add_argument('id')
        q.add_argument('--to', choices=['plan', 'execute', 'review', 'checking', 'ready', 'ship'])
    n = sub.add_parser('new'); n.add_argument('id'); n.add_argument('--title', required=True); n.add_argument('--mode', choices=['interactive', 'unattended'], default='interactive'); n.add_argument('--quote')
    n.add_argument('--intent', choices=['single-task', 'serial-multi-task', 'parallel-multi-task', 'cross-session', 'full-stack'], default='single-task', help='Git topology recommendation context')
    n.add_argument('--size', choices=['small', 'large'], default='small', help='use large for cross-module or substantial single tasks')
    n.add_argument('--independent-verification', action='store_true', help='request a separate worktree for independent verification')
    n.add_argument('--protect-existing', action='store_true', help='request a clean worktree to protect existing changes')
    n.add_argument('--legacy-layout',action='store_true',help='compatibility: separated spec/plan layout')
    n.add_argument('--work',help='continue a saved idea by UUID or number')
    for name in ('approve', 'seal-plan', 'task-add', 'task-start', 'task-done', 'task-reopen', 'revalidate', 'run', 'run-status', 'scope', 'check-path', 'review', 'ready', 'close', 'save', 'resume', 'status', 'decision', 'authorize', 'authority', 'block', 'unblock', 'observe-head', 'recover-run', 'documents-adopt', 'acceptance-enable', 'acceptance-record', 'topology', 'topology-select', 'external-link'):
        q = sub.add_parser(name); q.add_argument('id')
        if name == 'documents-adopt':
            q.add_argument('--spec', required=True); q.add_argument('--plan', required=True); q.add_argument('--reason', required=True)
        elif name == 'approve': q.add_argument('--quote', required=True)
        elif name == 'seal-plan': q.add_argument('--check-structure', action='store_true', help='reject structural gaps before sealing; does not prove semantic quality')
        elif name == 'task-add':
            q.add_argument('task'); q.add_argument('--title', required=True); q.add_argument('--acceptance', required=True); q.add_argument('--depends', default=''); q.add_argument('--no-tests-reason')
            q.add_argument('--role', choices=['implementation', 'frontend', 'backend', 'integration'], default='implementation')
            q.add_argument('--owned-by', help='the unique integration task that owns this frontend/backend task')
            q.add_argument('--agent'); q.add_argument('--session'); q.add_argument('--parent-task')
        elif name in ('task-start', 'task-done'):
            q.add_argument('task')
            if name == 'task-done':
                q.add_argument('--no-tests-reason')
        elif name == 'task-reopen': q.add_argument('task'); q.add_argument('--reason', required=True)
        elif name in {'status','resume'}:
            q.add_argument('--brief', action='store_true',
                           help='read the recorded ledger only; validation is NOT_CHECKED')
        elif name == 'run':
            q.add_argument('--task', required=True); q.add_argument('--kind', choices=['tests', 'check'], default='tests'); q.add_argument('--timeout', type=int, default=300); q.add_argument('--scan-timeout', type=int, default=120)
        elif name == 'run-status':
            q.add_argument('--evidence', required=True, help='evidence id to inspect')
            q.add_argument('--wait', type=float, default=0, help='seconds to wait, clamped to 0..30')
        elif name == 'scope':
            group = q.add_mutually_exclusive_group(required=True); group.add_argument('--path'); group.add_argument('--off', action='store_true'); q.add_argument('--quote', required=True)
        elif name == 'check-path': q.add_argument('path')
        elif name == 'review': q.add_argument('--reviewer', choices=['self', 'independent', 'fresh-context'], required=True); q.add_argument('--blocking', type=int, required=True)
        elif name == 'decision': q.add_argument('--text', required=True); q.add_argument('--source', choices=['user', 'agent'], required=True)
        elif name in ('authorize', 'authority'):
            q.add_argument('action', choices=['commit', 'push', 'create-pr', 'merge', 'delete-branch', 'publish', 'spend', 'send']); q.add_argument('--scope', required=True)
            if name == 'authorize': q.add_argument('--quote', required=True)
        elif name == 'block': q.add_argument('--reason', required=True)
        elif name == 'unblock': q.add_argument('--reason', required=True)
        elif name == 'observe-head':
            q.add_argument('--reason', required=True)
            q.add_argument('--task', dest='task_id', help='observe a child branch commit after inspection')
        elif name == 'close':
            q.add_argument('--reason', required=True)
            q.add_argument('--abandoned', action='store_true', help='close a blocked task explicitly as abandoned')
            q.add_argument('--allow-dirty', action='store_true', help='accept uncommitted workspace changes with an explicit reason')
            q.add_argument('--cleanup', action='store_true', help='after closing, remove merged clean worktrees owned by this run')
            q.add_argument('--reconcile', action='store_true', help='normalize an already closed archive whose workspace is gone')
        elif name == 'recover-run': q.add_argument('--evidence', required=True); q.add_argument('--outcome', choices=['not-run', 'failed'], required=True)
        elif name == 'acceptance-record': q.add_argument('task'); q.add_argument('--card', required=True)
        elif name == 'topology-select':
            q.add_argument('--mode', choices=['in-place', 'branch', 'worktree'], required=True)
            q.add_argument('--quote', required=True)
            q.add_argument('--branch')
            q.add_argument('--path')
            q.add_argument('--base', help='explicit starting branch or commit (for dependent branches)')
            q.add_argument('--rebind', action='store_true', help='explicitly rebind after investigation; invalidates old verification')
            q.add_argument('--task', dest='task_id', help='bind this topology to a plan task instead of the parent run')
            q.add_argument('--legacy-worktree',action='store_true',help='explicit compatibility: create a GSP-owned Git worktree')
            q.add_argument('--native-owner',default='local',help='actual Codex host id for a native workspace action')
        elif name == 'external-link':
            q.add_argument('--kind', choices=['issue', 'pr'], required=True)
            q.add_argument('--value', required=True)
    q = sub.add_parser('inspect-lock', help='inspect writer lock without mutating it'); q.add_argument('id', nargs='?')
    q = sub.add_parser('topology-clean', help='dry-run or remove merged, clean, inactive worktrees')
    q.add_argument('id')
    q.add_argument('task', nargs='?')
    q.add_argument('--apply', action='store_true', help='perform removals; default is read-only')
    q = sub.add_parser('recover-lock', help='recover a proven-dead writer lock'); q.add_argument('--evidence', required=True); q.add_argument('--reason', required=True)
    q = sub.add_parser('session-bind', help='issue a task-scoped context handle for a real host actor')
    q.add_argument('work_uid')
    q.add_argument(
        '--kind',
        choices=['main_chat', 'independent_worker', 'native_executor', 'helper', 'reviewer'],
        required=True,
    )
    q.add_argument('--host', required=True)
    q.add_argument('--thread')
    q.add_argument('--context-out', required=True)
    q.add_argument('--recover-legacy-controller', action='store_true')
    q.add_argument('--reason')
    q = sub.add_parser('session-register', help='register a task-scoped main or child session')
    q.add_argument('id')
    q.add_argument('--session')
    q.add_argument('--role', choices=['controller', 'worker', 'integration', 'observer'], required=True)
    q.add_argument('--host', default='unknown')
    q.add_argument('--thread')
    q.add_argument('--task')
    q.add_argument('--branch')
    q.add_argument('--worktree')
    q.add_argument('--write-mode', choices=['write', 'read-only'], default='write')
    q.add_argument('--path', action='append', default=[])
    q.add_argument('--context', help='absolute SessionBinding context JSON created for this work')
    q.add_argument('--workspace-group', choices=['parent'], help='join the sequential parent workspace group')
    q.add_argument('--takeover', action='store_true')
    q.add_argument('--reason', default='')
    q = sub.add_parser('session-status', help='show the task session registry')
    q.add_argument('id')
    q = sub.add_parser('session-claim', help='claim additional writable paths for a session')
    q.add_argument('id')
    q.add_argument('--session', required=True)
    q.add_argument('--path', action='append', required=True)
    q.add_argument('--replace', action='store_true')
    q = sub.add_parser('session-brief', help='write a stable child-task brief')
    q.add_argument('id')
    q.add_argument('task')
    q.add_argument('--session', required=True)
    q.add_argument('--owner')
    q.add_argument('--title', required=True)
    q.add_argument('--objective', required=True)
    q.add_argument('--path', action='append', default=[])
    q.add_argument('--interface', action='append', default=[])
    q.add_argument('--test', action='append', default=[])
    q.add_argument('--depends', default='')
    q.add_argument('--forbid', default='')
    q.add_argument('--replace', action='store_true')
    q = sub.add_parser('session-report', help='write the latest child-task report')
    q.add_argument('id')
    q.add_argument('task')
    q.add_argument('--session', required=True)
    q.add_argument('--status', choices=['DONE', 'BLOCKED', 'CHANGE_REQUEST'], required=True)
    q.add_argument('--summary', required=True)
    q.add_argument('--files', action='append', default=[])
    q.add_argument('--tests', action='append', default=[])
    q.add_argument('--interfaces', action='append', default=[])
    q.add_argument('--evidence', action='append', default=[])
    q.add_argument('--need-from-main', default='')
    q.add_argument('--ruling', default='')
    q = sub.add_parser('session-accept', help='accept a completed child task and trigger lifecycle cleanup')
    q.add_argument('id')
    q.add_argument('task')
    q.add_argument('--session', required=True, help='current controller session, such as S01')
    q.add_argument('--reason', required=True)
    q = sub.add_parser('session-handoff', help='transfer the controller lease')
    q.add_argument('id')
    q.add_argument('--from', dest='from_session', required=True)
    q.add_argument('--to', dest='to_session', required=True)
    q.add_argument('--reason', required=True)
    q = sub.add_parser('session-dispatch', help='dispatch ready child tasks as one independent batch')
    q.add_argument('id')
    q.add_argument('--workspace-group', choices=['parent'], help='reuse the authorized parent root for one sequential task')
    q.add_argument('--session', required=True, help='current controller session, such as S01')
    q.add_argument('--task', action='append', dest='tasks', default=[], help='task ID, repeat for a batch')
    q.add_argument('--target', action='append', default=[], metavar='TASK=PLATFORM')
    q.add_argument('--platform', choices=['codex', 'claude', 'zcode', 'manual', 'other'], default='manual')
    q.add_argument('--dispatch-mode', choices=['auto', 'adapter', 'manual'])
    q.add_argument('--path', action='append', default=[], metavar='TASK=PATH')
    q.add_argument('--worktree', action='append', default=[], metavar='TASK=PATH')
    q.add_argument('--worktree-mode', choices=['auto', 'bind'], default='auto',
                   help='create GSP-managed worktrees or bind explicit paths')
    q.add_argument('--worker-timeout', type=int, default=900,
                   help='bounded timeout for one Codex auto worker')
    q.add_argument('--max-parallel', type=int, default=3)
    q.add_argument('--retry', action='store_true', help='archive prior prompt and allow a tracked retry')
    sub.add_parser('session-progress', help='show controller progress and the next ready batch').add_argument('id')
    q = sub.add_parser('session-watch', help='consume durable child report events')
    q.add_argument('id')
    q.add_argument('--adapter', choices=['manual', 'codex', 'claude', 'zcode', 'other'], default='manual')
    q.add_argument('--limit', type=int, default=20)
    q.add_argument('--event-id')
    q = sub.add_parser('session-clean', help='record accepted child worktree cleanup')
    q.add_argument('id')
    q.add_argument('--task', required=True)
    q.add_argument('--result', choices=['archived', 'released', 'removed'], required=True)
    q.add_argument('--reason', required=True)
    q.add_argument('--retry', action='store_true', help='retry lifecycle cleanup for an accepted managed worktree')
    sub.add_parser('codex-hook', help='native Codex hook protocol (stdin JSON)')
    sub.add_parser('hooks-report', help='bounded offline Codex hook audit summary for --project')
    for name in ('hooks-install','hooks-upgrade','hooks-uninstall','hooks-recover','hooks-status'):
        q = sub.add_parser(name, help='maintain GSP Codex user-level hooks only')
        q.add_argument('--profile', help='Codex profile root (default: ~/.codex)')
        q.add_argument('--dry-run', action='store_true')
    q = sub.add_parser('health'); q.add_argument('--host', choices=['claude', 'codex', 'zcode', 'both'], default='both'); q.add_argument('--strict', action='store_true', help='return non-zero for a blocking Git route')
    q.add_argument('--force-update-check', action='store_true', help='bypass the 24-hour GitHub update-check cache')
    q.add_argument('--documents',action='store_true',help='include document governance audit')
    q.add_argument('--documents-only',action='store_true',help='read-only target; no installation checks')
    q.add_argument('--report-dir',help='explicit report destination (default: target docs/gsp/reports/document-governance)')
    q = sub.add_parser('observe', help='build a local privacy-preserving GSP observability report')
    q.add_argument('--run-id', help='stable report directory name; defaults to a unique UTC id')
    q = sub.add_parser('orchestration-migrate', help='preview or explicitly apply recoverable identity migration')
    q.add_argument('--action', choices=['preview', 'stage', 'apply', 'resume', 'restore', 'adopt'], default='preview')
    q.add_argument('--manifest', help='previous preview JSON; keep it outside the source checkout')
    q.add_argument('--request-uid')
    q.add_argument('--legacy-run')
    q.add_argument('--new-run')
    q.add_argument('--apply', action='store_true', help='perform the explicitly selected maintenance action')
    q = sub.add_parser('quality', help='run the project-declared, bounded quality stack')
    q.add_argument('--mode', choices=['change', 'baseline', 'release'], default='change')
    q.add_argument('--task', default='quality')
    q.add_argument('--run-id')
    q.add_argument('--config', help='project-relative quality stack JSON (default .gsp/quality.json)')
    q = sub.add_parser('source-resolve', help='resolve a GitHub stable tag into an isolated source directory')
    q.add_argument('--source', required=True)
    q.add_argument('--version', required=True)
    q.add_argument('--target', required=True)
    q.add_argument('--store')
    q.add_argument('--manifest')
    q.add_argument('--timeout', type=int, default=120)
    q = sub.add_parser('version-status', help='show the current resolved source version')
    q.add_argument('--store', required=True)
    q = sub.add_parser('version-rollback', help='switch the source store to a previous version')
    q.add_argument('--store', required=True)
    q.add_argument('--version')
    for name in ('install','upgrade','uninstall','install-recover'):
        q = sub.add_parser(name, description='Scopes: project = this project; user = global for the current user.')
        q.add_argument('--host', choices=['claude','codex','zcode','both'], required=True)
        q.add_argument('--scope', choices=['project','user'], default='project' if name=='install' else 'user', help='project: target project skills; user: current user profile skills (install defaults to project)')
        q.add_argument('--profile', help='explicit user profile root; defaults to the selected host profile')
        q.add_argument('--optional', nargs='*', default=[])
        q.add_argument('--dry-run', action='store_true')
        if name == 'upgrade':
            q.add_argument('--source', choices=['github', 'local'], default='github',
                           help='published stable GitHub tag by default; local is an explicit developer-only override')
    q = sub.add_parser('preference'); q.add_argument('key'); q.add_argument('--value', choices=['ask', 'recommended']); q.add_argument('--quote')
    q = sub.add_parser('todo-list'); q.add_argument('--origin')
    q = sub.add_parser('todo-add')
    for field in ('title', 'what', 'why', 'context', 'source'):
        q.add_argument('--' + field, required=True)
    q.add_argument('--priority', choices=['P0','P1','P2','P3','P4'], default='P2')
    q.add_argument('--effort', choices=['S','M','L','XL'], default='S')
    q.add_argument('--origin', default=''); q.add_argument('--blocking', action='store_true')
    q.add_argument('--component', default='项目'); q.add_argument('--depends-on', default=''); q.add_argument('--blocked-by', default='')
    q = sub.add_parser('todo-done'); q.add_argument('todo'); q.add_argument('--run', required=True); q.add_argument('--task', required=True); q.add_argument('--reason', required=True); q.add_argument('--version')
    q = sub.add_parser('todo-reopen'); q.add_argument('todo'); q.add_argument('--reason', required=True)
    sub.add_parser('todo-migrate')
    sub.add_parser('todo-format')
    q = sub.add_parser('todo-recover'); q.add_argument('--reason', required=True)
    q = sub.add_parser('todo-link'); q.add_argument('todo'); q.add_argument('--run', required=True); q.add_argument('--task', required=True)
    sub.add_parser('hook')
    from .driver import configure
    configure(sub)
    from .documents import add_parsers
    add_parsers(sub)
    return p


SESSION_CONTEXT_READ_COMMANDS = frozenset({
    'authority', 'check-path', 'handoff', 'inspect-lock',
    'plan-check', 'resume', 'run-status', 'session-progress',
    'session-status', 'status', 'topology', 'work-show',
})


SESSION_CONTEXT_BOUND_READ_COMMANDS = frozenset({
    'session-progress', 'session-status',
})


SESSION_CONTEXT_WRITE_COMMANDS = frozenset({
    'native-workspace','evidence-collect',
    'architecture-policy',
    'delivery',
    'acceptance-enable', 'acceptance-record', 'advance', 'approve', 'authorize',
    'block', 'close', 'decision', 'documents-adopt', 'external-link',
    'observe-head', 'quality', 'ready', 'recover-run', 'reopen', 'revalidate', 'review', 'run', 'save',
    'seal-plan', 'task-add', 'task-done', 'task-reopen', 'task-start',
    'topology-clean', 'topology-select', 'unblock',
})


SESSION_CONTEXT_PLAN_COMMANDS = frozenset({
    'native-workspace','evidence-collect',
    'architecture-policy',
    'acceptance-enable', 'advance', 'approve', 'documents-adopt', 'external-link',
    'decision', 'review', 'seal-plan', 'task-add', 'task-reopen',
    'topology-clean', 'topology-select',
})


SESSION_CONTEXT_REPORT_COMMANDS = frozenset({
    'session-brief', 'session-clean', 'session-dispatch', 'session-handoff',
    'session-report', 'session-watch',
})


SESSION_CONTEXT_ACCEPT_COMMANDS = frozenset({
    'delivery',
    'acceptance-record', 'close', 'ready', 'session-accept', 'task-done',
})


SESSION_CONTEXT_DOCUMENT_WRITE_COMMANDS = frozenset({
    'work-update', 'work-stage', 'work-link', 'module-set',
})


SESSION_CONTEXT_PROJECT_COMMANDS = frozenset({
    'init', 'new', 'work-new', 'docs-move', 'docs-move-recover',
})


SESSION_CONTEXT_TODO_WRITE_COMMANDS = frozenset({
    'todo-add', 'todo-done', 'todo-link', 'todo-reopen', 'todo-migrate',
    'todo-format', 'todo-recover',
})


from dataclasses import dataclass


@dataclass(frozen=True)
class CommandPolicy:
    capabilities: tuple
    readonly: bool
    recovery: bool
    lock: str


def command_policy(command,args=None):
    """One static classification for CLI, hooks and business adapters.

    A strict legacy status/resume may validate topology. Only explicit brief
    ledger views and the listed object queries promise zero effects.
    """
    action=getattr(args,'action',None)
    readonly=(command in {'run-status','session-status','session-progress'}
              or command in {'status','resume'} and bool(getattr(args,'brief',False))
              or command in {'delivery','native-workspace'} and action=='status'
              or command=='evidence-verify'
              or command=='schedule')
    capabilities=[]
    writes=(SESSION_CONTEXT_WRITE_COMMANDS|SESSION_CONTEXT_REPORT_COMMANDS|
            SESSION_CONTEXT_ACCEPT_COMMANDS|SESSION_CONTEXT_DOCUMENT_WRITE_COMMANDS|
            SESSION_CONTEXT_TODO_WRITE_COMMANDS)
    if command in writes and not readonly:
        capabilities.append('write')
        if command in SESSION_CONTEXT_PLAN_COMMANDS:
            capabilities.append('plan')
        if command in SESSION_CONTEXT_ACCEPT_COMMANDS:
            capabilities.append('accept')
        if command in SESSION_CONTEXT_REPORT_COMMANDS:
            capabilities.append('report')
    recovery=command in {'recover-run','topology-clean','close'} or (
        command in {'delivery','native-workspace'} and action in {'recover','restore','confirm'})
    return CommandPolicy(tuple(capabilities),readonly,recovery,'none' if readonly else 'writer')
