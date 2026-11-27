"""project; shared business implementation, independent of CLI."""
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

from .core import (WorkflowError, atomic, cached_value, digest, inside, now, read, redact, sha)
from .git_operations import (_git_run, git_identity, is_ancestor)
from .project_resolution import (ProjectLocation)
from .workflow_support import (_format_brief_state, _identity_at, _ledger_state_at, evidence_for, explicit_work_number_from_run_id, fingerprint, integration_dependencies, invalidate_delivery, parallel_required, project_command_effects, slug, stable_action_uid, topology_required, topology_selected, validate_delivery)

class Project(ProjectLocation):


    def initialize(self):
        if getattr(self, '_control_root_uninitialized', False):
            raise WorkflowError('CONTROL_ROOT_UNINITIALIZED initialize the registered primary worktree first')
        if not self.root.is_dir():
            raise WorkflowError('PROJECT_MISSING create the target directory first')
        ident = self.meta / 'project.json'
        if not ident.exists():
            atomic(ident, {'schema': 1, 'id': uuid.uuid4().hex, 'root': str(self.root), 'created': now()})
        if not (self.meta / 'config.json').exists():
            atomic(self.meta / 'config.json', {'schema': 1, 'preferences': {}, 'max_fix_rounds': 3})
        ignore = self.root / '.gitignore'
        if not inside(self.root, ignore):
            raise WorkflowError('IGNORE_ESCAPE .gitignore resolves outside project')
        content = ignore.read_text(encoding='utf-8-sig') if ignore.exists() else ''
        if '.gsp/' not in content.splitlines() and '/.gsp/' not in content.splitlines():
            atomic(ignore, content.rstrip() + ('\n\n' if content.strip() else '') + '# GstackSP local workflow state\n.gsp/\n')
        self.identity()
        from .identity_store import IdentityStore, IdentityStoreError
        identity_store = IdentityStore(self.root)
        try:
            identity_store.ensure_authority()
        except IdentityStoreError as exc:
            raise WorkflowError(str(exc)) from exc
        from .todos import Backlog
        Backlog(self.root).initialize()
        self.dashboard()

    def identity(self):
        return _identity_at(self.root)

    def paths(self, task_id):
        slug(task_id)
        paths = (self.meta / 'runs' / task_id, self.root / 'docs/gsp/tasks' / task_id)
        state_file=paths[0]/'state.json'
        if state_file.exists():
            rel=read(state_file).get('doc_dir')
            if rel:
                if Path(rel).is_absolute() or '..' in Path(rel).parts or not rel.startswith('docs/work/'):
                    raise WorkflowError('TASK_PATH_ESCAPE')
                paths=(paths[0],self.root/rel)
        if not all(inside(self.root, p) for p in paths):
            raise WorkflowError('TASK_PATH_ESCAPE')
        return paths

    def document(self, state, kind):
        """One authoritative spec/plan per task; old states retain their old layout."""
        if kind not in ('design', 'plan'):
            raise WorkflowError('INVALID_DOCUMENT_KIND')
        if 'documents' not in state:
            return self.paths(state['id'])[1] / (kind + '.md')
        if not isinstance(state['documents'], dict):
            raise WorkflowError('DOCUMENT_BINDING_INVALID')
        rel = state['documents'].get(kind)
        if not isinstance(rel, str) or Path(rel).is_absolute():
            raise WorkflowError('DOCUMENT_PATH_INVALID')
        rel=Path(rel).as_posix()
        file = self.root / rel
        if '..' in Path(rel).parts or not inside(self.root, file) or file.suffix != '.md' or not rel.startswith('docs/'):
            raise WorkflowError('DOCUMENT_PATH_ESCAPE')
        return file

    def document_link(self, state, kind, folder):
        return os.path.relpath(self.document(state, kind), folder).replace('\\', '/')

    def active_state(self, context_path=None):
        from .git_topology import bound_task_for_path
        task_id = bound_task_for_path(self.root, context_path) if context_path is not None else None
        task_id = task_id or read(self.meta / 'active.json')['task']
        run, doc = self.paths(task_id)
        state = read(run / 'state.json')
        if state.get('project_id') != self.identity()['id'] or state.get('root') != str(self.root):
            raise WorkflowError('PROJECT_MISMATCH refusing cross-project continuation')
        return state, run, doc

    def workspace_root(self, state, validate=True, task_id=None, allow_head_change=False):
        """Return the source workspace bound to a task.

        The control root owns ``.gsp`` and task documents.  A worktree-mode
        task keeps those records there while commands and fingerprints run in
        the bound source workspace.  Older states simply use the project root.
        """
        topology = state.get('git_topology') or {}
        if task_id:
            task = self.item(state, task_id)
            task_topology = task.get('topology') or {}
            if task_topology.get('workspace_root'):
                topology = task_topology
        raw = topology.get('workspace_root') or str(self.root)
        path = Path(raw).resolve()
        if not inside(self.root, path):
            # Codex owns sibling worktrees outside the control checkout.
            # An explicit binding still needs this project's registered Git
            # ownership; merely naming an external directory grants nothing.
            from .worktree_adapters import WorktreeAdapter, WorktreeAdapterError
            if not topology.get('workspace_root') or not topology.get('effective_mode'):
                raise WorkflowError('GIT_WORKSPACE_ESCAPE external workspace is not explicitly bound')
            try:
                WorktreeAdapter(self.root, expected_project_uid=state.get('project_id')).inspect(path)
            except WorktreeAdapterError as exc:
                raise WorkflowError('GIT_WORKSPACE_ESCAPE ' + str(exc)) from exc
        # Legacy/non-Git projects keep the historical project-root execution
        # path.  A workspace must be a real Git worktree only after the task
        # has opted into the topology contract or a child topology is bound.
        topology_enforced = bool(topology.get('enforce') or topology.get('effective_mode') in {'in-place', 'branch', 'worktree'})
        if not (validate and topology_enforced):
            return path

        def load():
            if not path.is_dir():
                raise WorkflowError('GIT_WORKSPACE_MISSING bound source workspace does not exist')
            identity = git_identity(path)
            if not identity.get('root') or Path(identity['root']).resolve() != path:
                raise WorkflowError('GIT_WORKSPACE_INVALID bound source workspace is not a Git worktree')
            expected = topology.get('branch')
            if expected and identity.get('branch') != expected:
                raise WorkflowError('GIT_WORKSPACE_MISMATCH bound branch differs from the recorded topology')
            if task_id and not allow_head_change and topology.get('head') and topology['head'] != identity.get('head'):
                raise WorkflowError('HEAD_CHANGED inspect child commit, then observe-head --task ' + task_id)
            return path

        key = (str(path), topology.get('branch') or '', topology.get('head') or '',
               bool(allow_head_change), task_id or '')
        return cached_value('workspace_root', key, load)

    def validate_topology(self, state, allow_head_change=False):
        """Read-only validation shared by health, status and execution."""
        if not topology_selected(state):
            return None
        from .git_topology import probe
        inventory = probe(self.root, include_branches=False, include_status=False)
        if not inventory.get('worktrees'):
            raise WorkflowError('GIT_PROBE_FAILED cannot verify registered worktrees')
        self.validate_claims(state, inventory.get('active_tasks'))
        registered = {Path(row['path']).resolve(): row for row in inventory['worktrees']}
        bindings = [(None, state['git_topology'])]
        bindings += [(t['id'], t['topology']) for t in state.get('tasks', []) if t.get('topology')]
        attested_ids = set()
        if any(task_id for task_id, _ in bindings):
            from .dispatch import attested_removed_tasks
            attested_ids = attested_removed_tasks(self, state)
        for task_id, binding in bindings:
            if task_id in attested_ids:
                continue
            path = self.workspace_root(state, task_id=task_id, allow_head_change=allow_head_change,
                                       validate=False)
            if not path.is_dir():
                raise WorkflowError('GIT_WORKSPACE_MISSING bound source workspace does not exist')
            row = registered.get(path)
            if not row or row.get('prunable'):
                raise WorkflowError('GIT_WORKSPACE_UNREGISTERED ' + str(path))
            expected_branch = binding.get('branch')
            if expected_branch and row.get('branch') != expected_branch:
                raise WorkflowError('GIT_WORKSPACE_MISMATCH bound branch differs from the recorded topology')
            expected_head = binding.get('head') if task_id else state.get('git', {}).get('head')
            if not allow_head_change and expected_head and row.get('head') != expected_head:
                raise WorkflowError('HEAD_CHANGED inspect commit and observe-head' + (' --task ' + task_id if task_id else ''))
            if (binding.get('base_head') and
                    (allow_head_change or not expected_head)):
                ancestor = is_ancestor(path, binding['base_head'], 'HEAD')
                if ancestor.returncode:
                    raise WorkflowError('GIT_BASE_MISMATCH bound base is not an ancestor of the workspace HEAD')
        return inventory

    def verification_root(self, state, allow_head_change=False):
        integrations = [t for t in state.get('tasks', []) if t.get('role') == 'integration']
        return self.workspace_root(state, task_id=integrations[0]['id'] if len(integrations) == 1 else None,
                                   allow_head_change=allow_head_change)

    def validate_claims(self, state, active_tasks=None):
        if not topology_selected(state):
            return
        from .git_topology import _active_summaries
        if active_tasks is None:
            active_tasks = _active_summaries(self.root, None)
        claims = [r for r in active_tasks if r.get('claiming')]
        owned = [r for r in claims if r['id'] == state['id'] or r.get('parent') == state['id']]
        for left in owned:
            for right in claims:
                if left['id'] == right['id']:
                    continue
                if Path(left['workspace_root']).resolve() == Path(right['workspace_root']).resolve():
                    raise WorkflowError('GIT_WORKTREE_CONFLICT ' + left['id'] + ' / ' + right['id'])
                if left.get('branch') and left['branch'] == right.get('branch'):
                    raise WorkflowError('GIT_BRANCH_CONFLICT ' + left['id'] + ' / ' + right['id'])

    def validate_integration(self, state, allow_head_change=False, context=None,
                             verify_evidence=True):
        integrations = [t for t in state.get('tasks', []) if t.get('role') == 'integration']
        if not integrations:
            return
        if len(integrations) != 1:
            raise WorkflowError('GIT_INTEGRATION_OWNER_CONFLICT only one integration owner is allowed')
        integration = integrations[0]
        target = self.workspace_root(state, task_id=integration['id'],
                                     allow_head_change=allow_head_change)
        from .dispatch import attested_removed_tasks
        removed_ids = attested_removed_tasks(self, state)
        from .git_topology import probe
        for child in state['tasks']:
            if child['id'] == integration['id'] or child.get('role') == 'observer':
                continue
            if child['id'] not in integration_dependencies(state, integration) or child['status'] != 'done':
                raise WorkflowError('GIT_INTEGRATION_DEPENDENCY_MISSING ' + child['id'])
            if child['id'] in removed_ids:
                continue
            source = self.workspace_root(state, task_id=child['id'],
                                         allow_head_change=allow_head_change)
            inventory = probe(source, active_tasks=[], include_branches=False,
                              include_status=True, include_worktrees=False)
            if inventory.get('current', {}).get('source_changed_paths'):
                raise WorkflowError('GIT_CHILD_UNCOMMITTED ' + child['id'] + ': commit verified output before integration')
            if inventory.get('status') == 'blocked':
                raise WorkflowError('GIT_CHILD_INVALID ' + child['id'])
            if verify_evidence:
                evidence_for(self, state, child['id'],
                             allow_head_change=allow_head_change, context=context)
            child_head = (inventory.get('current', {}).get('head') or
                          git_identity(source).get('head'))
            included = is_ancestor(target, child_head, 'HEAD')
            if included.returncode:
                raise WorkflowError('GIT_INTEGRATION_MISSING_CHILD ' + child['id'] + ': integrate the verified child commit before checks')

    def _topology_context(self, state):
        topology = state.get('git_topology') or {}
        return {
            'number': state.get('number'),
            'title': state.get('title', ''),
            'intent': topology.get('intent', 'single-task'),
            'size': topology.get('size', 'small'),
            'independent_verification': topology.get('independent_verification', False),
            'protect_existing': topology.get('protect_existing', False),
        }

    def topology_report(self, state):
        from .git_topology import probe, recommend, residue
        inventory = probe(self.root)
        recommendation = recommend(inventory, self._topology_context(state))
        return {
            'schema': 1,
            'task': state['id'],
            'number': state.get('number'),
            'control_root': str(self.root),
            'current': inventory.get('current', {}),
            'worktrees': inventory.get('worktrees', []),
            'branches': inventory.get('branches', []),
            'active_tasks': inventory.get('active_tasks', []),
            'task_bindings': [
                {'id': task.get('id'), 'role': task.get('role', 'implementation'),
                 'status': task.get('status'), 'topology': task.get('topology') or {}}
                for task in state.get('tasks', [])
            ],
            'risks': inventory.get('risks', []),
            'inventory_status': inventory.get('status'),
            'recommendation': recommendation,
            'selected': state.get('git_topology') or {},
            'residue': residue(self.root),
        }

    def _state_topology(self, state):
        """Create a compatibility topology object for an old state in memory."""
        from .git_topology import probe, recommend
        existing = state.get('git_topology')
        if isinstance(existing, dict):
            return existing
        inventory = probe(self.root)
        recommendation = recommend(inventory, self._topology_context(state))
        return {
            'schema': 1,
            'status': 'legacy',
            'enforce': False,
            'intent': 'single-task',
            'recommended_mode': recommendation.get('mode'),
            'requested_mode': None,
            'effective_mode': None,
            'control_root': str(self.root),
            'workspace_root': str(self.root),
            'branch': (state.get('git') or {}).get('branch'),
            'base_branch': (state.get('git') or {}).get('branch'),
            'base_head': (state.get('git') or {}).get('head'),
            'initial_inventory': inventory,
            'decision_quote': None,
            'decision_source': None,
        }

    def bind_task_topology(self, state, task_id, mode, branch, workspace_root, source, quote=None):
        """Record one writable child's topology binding without creating Git resources."""
        task = self.item(state, task_id)
        if str(task.get('role') or 'implementation') not in {'implementation', 'frontend', 'backend', 'integration'}:
            raise WorkflowError('GIT_TASK_READ_ONLY_TOPOLOGY read-only observers cannot claim a writable workspace')
        if mode != 'worktree':
            raise WorkflowError('GIT_TASK_WORKTREE_REQUIRED writable children need independent worktrees')
        if not re.fullmatch(r'T\d+', task_id):
            raise WorkflowError('GIT_PLAN_TASK_INVALID child branches require a Tnn task identity')
        number = state.get('number')
        task_suffix = f'T{int(task_id[1:]):02d}'
        if branch and not re.match(r'^[^/]+/' + re.escape(number + '-' + task_suffix) + '-', branch):
            raise WorkflowError('GIT_BRANCH_TASK_MISMATCH child branch must contain its own GSP and Tnn identity')
        workspace = Path(workspace_root).resolve()
        identity = git_identity(workspace)
        parent = state.get('git_topology') or {}
        parent_git = state.get('git') or {}
        task['topology'] = {
            'schema': 1,
            'status': 'selected',
            'requested_mode': mode,
            'effective_mode': mode,
            'control_root': str(self.root),
            'workspace_root': str(workspace),
            'base_branch': parent.get('base_branch') or parent_git.get('branch'),
            'base_head': parent.get('base_head') or parent.get('head') or identity.get('head'),
            'branch': branch or identity.get('branch'),
            'base_workspace': str(self.root),
            'decision_quote': quote,
            'decision_source': source,
            'selected_at': now(),
            'created_at': now(),
            'head': identity.get('head'),
            'task': task_id,
            'parent_work': number,
        }
        return task['topology']

    def select_topology(self, state, args):
        if state.get('inflight'):
            raise WorkflowError('EXECUTION_UNKNOWN inspect and recover the interrupted operation first')
        task_id = getattr(args, 'task_id', None)
        if task_id:
            task = self.item(state, task_id)
            if str(task.get('role') or 'implementation') not in {'implementation', 'frontend', 'backend', 'integration'}:
                raise WorkflowError('GIT_TASK_READ_ONLY_TOPOLOGY read-only observers cannot claim a writable workspace')
            child = dict(state)
            child['id'] = f'{state["id"]}:{task_id}'
            child['title'] = task.get('title') or task_id
            child['git_topology'] = task.get('topology') or {'intent': (state.get('git_topology') or {}).get('intent', 'single-task')}
            if args.mode != 'worktree':
                raise WorkflowError('GIT_TASK_WORKTREE_REQUIRED writable children need independent worktrees')
            from .git_topology import branch_name
            if not re.fullmatch(r'T\d+', task_id):
                raise WorkflowError('GIT_PLAN_TASK_INVALID child branches require a Tnn task identity')
            task_suffix = f'T{int(task_id[1:]):02d}'
            if args.branch and not re.match(r'^[^/]+/' + re.escape(state['number'] + '-' + task_suffix) + '-', args.branch):
                raise WorkflowError('GIT_BRANCH_TASK_MISMATCH child branch must contain its own GSP and Tnn identity')
            import types
            requested_branch = args.branch or branch_name(state['number'], task_id, child['title'])
            child_args = types.SimpleNamespace(mode=args.mode, quote=args.quote,
                branch=requested_branch,
                path=args.path, task_id=None, base=getattr(args, 'base', None),
                rebind=getattr(args, 'rebind', False), owner_state=state)
            result = self.select_topology(child, child_args)
            task['topology'] = self.bind_task_topology(
                state, task_id, args.mode,
                result['git_topology'].get('branch') or requested_branch,
                result['git_topology'].get('workspace_root'),
                'caller-attributed-user', quote=args.quote,
            )
            return {'task': state['id'], 'plan_task': task_id, 'number': state.get('number'),
                    'topology': task['topology']}
        from .git_topology import branch_name, probe, validate_branch_name
        if not args.quote or not args.quote.strip():
            raise WorkflowError('GIT_TOPOLOGY_QUOTE_REQUIRED record the actual user choice')
        topology = dict(self._state_topology(state))
        owner_state = getattr(args, 'owner_state', state)
        rebind = getattr(args, 'rebind', False)
        requested_base = getattr(args, 'base', None)
        resolved_base = None
        if requested_base:
            resolved = _git_run(self.root, 'rev-parse', '--verify', str(requested_base) + '^{commit}')
            if resolved.returncode:
                raise WorkflowError('GIT_BASE_INVALID select a real commit or branch')
            resolved_base = resolved.stdout.strip()
        if topology_selected(state) and not rebind:
            same = args.mode == topology.get('effective_mode') and (not args.branch or args.branch == topology.get('branch'))
            same = same and (not args.path or (self.root / args.path).resolve() == Path(topology['workspace_root']).resolve())
            same = same and (not requested_base or resolved_base == topology.get('base_head'))
            if not same:
                raise WorkflowError('GIT_REBIND_REQUIRED explicit --rebind with a reason is required')
            self.workspace_root(state)
            return {'task': state['id'], 'number': state.get('number'), 'git_topology': topology, 'reused': True}
        number = state.get('number')
        if not number and state.get('work_uuid'):
            try:
                from .documents import Governance
                number = Governance(self.root).get(state['work_uuid']).get('number')
                state['number'] = number
            except (WorkflowError, OSError):
                number = None
        if not number:
            raise WorkflowError('GIT_TASK_NUMBER_REQUIRED topology selection needs a GSP work-item number')
        inventory = probe(self.root)
        if inventory.get('status') == 'not-a-repository':
            raise WorkflowError('GIT_NOT_REPOSITORY topology selection requires a Git repository')
        fatal = [r for r in inventory.get('risks', []) if r['code'] in {'GIT_PROBE_FAILED', 'GIT_UNAVAILABLE', 'GIT_HEAD_UNBORN'}]
        if fatal:
            raise WorkflowError(fatal[0]['code'] + ' ' + fatal[0]['detail'])
        topology['enforce'] = True
        current = inventory.get('current') or {}
        requested = args.mode
        branch = args.branch
        if requested in ('branch', 'worktree'):
            branch = branch or branch_name(number, None, state.get('title', '任务'))
            validate_branch_name(branch)
            if not re.match(r'^[^/]+/' + re.escape(number) + r'-', branch):
                raise WorkflowError('GIT_BRANCH_NUMBER_MISMATCH branch must contain the task GSP number')
        claims = [row for row in inventory.get('active_tasks', []) if row.get('claiming') and row.get('id') != state['id']]
        target = (self.root / args.path).resolve() if args.path else (self.root / '.gsp/worktrees' / (branch or '')).resolve()
        if requested != 'worktree':
            target = self.root
        for claim in claims:
            claimed = Path(claim['workspace_root']).resolve()
            # The primary root's .gsp worktrees are isolated source checkouts,
            # not descendants of its tracked input set.
            if target == claimed:
                raise WorkflowError('GIT_WORKTREE_CONFLICT workspace claimed by ' + str(claim['id']))
            if branch and branch == claim.get('branch'):
                raise WorkflowError('GIT_BRANCH_CONFLICT branch claimed by ' + str(claim['id']))
        base = resolved_base or current.get('head')
        base_branch = current.get('branch')
        if requested_base:
            symbolic = _git_run(self.root, 'rev-parse', '--symbolic-full-name', str(requested_base))
            ref = symbolic.stdout.strip() if symbolic.returncode == 0 else ''
            base_branch = next((ref[len(prefix):] for prefix in ('refs/heads/', 'refs/remotes/')
                                if ref.startswith(prefix)), None)
        if requested == 'in-place':
            if not current.get('branch'):
                raise WorkflowError('GIT_DETACHED_HEAD in-place implementation requires a named branch')
            if current.get('branch') in ('main', 'master'):
                # The explicit quote is the required risk override; do not
                # silently turn it into a branch or worktree choice.
                pass
            topology.update({
                'status': 'selected', 'requested_mode': requested, 'effective_mode': requested,
                'control_root': str(self.root), 'workspace_root': str(self.root),
                'base_branch': current.get('branch'), 'base_head': current.get('head'),
                'branch': current.get('branch'), 'decision_quote': args.quote,
                'decision_source': 'caller-attributed-user', 'initial_inventory': inventory,
                'selected_at': now(),
            })
        elif requested == 'branch':
            if current.get('source_changed_paths') or [p for p in (current.get('changed_paths') or []) if p not in (current.get('workflow_changed_paths') or [])]:
                raise WorkflowError('GIT_DIRTY branch creation refuses to carry unreviewed working-tree changes')
            show = _git_run(self.root, 'show-ref', '--verify', '--quiet', f'refs/heads/{branch}')
            if show.returncode == 0:
                active = [row for row in inventory.get('active_tasks', []) if row.get('claiming') and row.get('branch') == branch and row.get('id') != state['id']]
                if active:
                    raise WorkflowError('GIT_BRANCH_CONFLICT branch is already bound to another active task')
                if current.get('branch') != branch:
                    self._record_topology_inflight(owner_state, state['id'], requested, branch, self.root, base, args.quote)
                    switched = _git_run(self.root, 'switch', branch)
                    if switched.returncode != 0:
                        self._topology_failed(owner_state, 'GIT_BRANCH_SWITCH_FAILED', switched.stderr or 'git switch failed')
            else:
                self._record_topology_inflight(owner_state, state['id'], requested, branch, self.root, base, args.quote)
                created = _git_run(self.root, 'switch', '-c', branch, base)
                if created.returncode != 0:
                    self._topology_failed(owner_state, 'GIT_BRANCH_CREATE_FAILED', created.stderr)
            identity = git_identity(self.root)
            topology.update({
                'status': 'selected', 'requested_mode': requested, 'effective_mode': requested,
                'control_root': str(self.root), 'workspace_root': str(self.root),
                'base_branch': current.get('branch'), 'base_head': current.get('head'),
                'branch': identity.get('branch'), 'base_workspace': str(self.root),
                'decision_quote': args.quote, 'decision_source': 'caller-attributed-user',
                'initial_inventory': inventory, 'selected_at': now(),
            })
            state['git'] = identity
        elif requested == 'worktree':
            requested_path = Path(args.path) if args.path else None
            worktree = (requested_path if requested_path and requested_path.is_absolute() else self.root / requested_path) if requested_path else (self.root / '.gsp' / 'worktrees' / branch)
            worktree = worktree.resolve()
            if not inside(self.root, worktree) or worktree == self.root:
                raise WorkflowError('GIT_WORKTREE_PATH_INVALID choose a path inside the control root')
            if worktree.exists() and (not worktree.is_dir() or any(worktree.iterdir())) and not rebind:
                raise WorkflowError('GIT_WORKTREE_PATH_BUSY target path already contains files')
            if not inside(self.root / '.gsp/worktrees', worktree):
                ignored = _git_run(self.root, 'check-ignore', '--quiet', str(worktree))
                if ignored.returncode:
                    raise WorkflowError('GIT_WORKTREE_NOT_IGNORED explicitly ignore the target before creating it')
            show = _git_run(self.root, 'show-ref', '--verify', '--quiet', f'refs/heads/{branch}')
            if show.returncode == 0:
                active = [row for row in inventory.get('active_tasks', []) if row.get('claiming') and row.get('branch') == branch and row.get('id') != state['id']]
                if active:
                    raise WorkflowError('GIT_BRANCH_CONFLICT branch is already bound to another active task')
                if not rebind:
                    raise WorkflowError('GIT_BRANCH_EXISTS choose a new unbound branch name or explicitly rebind')
            worktree.parent.mkdir(parents=True, exist_ok=True)
            if rebind and (worktree / '.git').is_file():
                identity = git_identity(worktree)
                if identity.get('branch') != branch or not any(Path(w['path']) == worktree for w in inventory['worktrees']):
                    raise WorkflowError('GIT_WORKSPACE_MISMATCH cannot adopt unrelated worktree')
            else:
                self._record_topology_inflight(owner_state, state['id'], requested, branch, worktree, base, args.quote)
                argv = ('worktree', 'add', str(worktree), branch) if show.returncode == 0 else ('worktree', 'add', '-b', branch, str(worktree), base)
                created = _git_run(self.root, *argv)
                if created.returncode != 0:
                    self._topology_failed(owner_state, 'GIT_WORKTREE_CREATE_FAILED', created.stderr)
            identity = git_identity(worktree)
            topology.update({
                'status': 'selected', 'requested_mode': requested, 'effective_mode': requested,
                'control_root': str(self.root), 'workspace_root': str(worktree),
                'base_branch': current.get('branch'), 'base_head': current.get('head'),
                'branch': identity.get('branch') or branch, 'base_workspace': str(self.root),
                'decision_quote': args.quote, 'decision_source': 'caller-attributed-user',
                'initial_inventory': inventory, 'selected_at': now(),
            })
            state['git'] = identity
        else:
            raise WorkflowError('GIT_TOPOLOGY_MODE_INVALID choose in-place, branch or worktree')
        if requested in ('branch', 'worktree'):
            topology['base_head'] = base
            topology['base_branch'] = base_branch
            topology['head'] = identity.get('head')
        else:
            topology['head'] = current.get('head')
        topology['created_by'] = os.environ.get('GSP_HOST', 'unknown')
        topology['created_at'] = now()
        owner_state.pop('inflight', None)
        if rebind:
            owner_state['review'] = None
            for task in owner_state.get('tasks', []):
                if task['status'] == 'done':
                    task.setdefault('history', []).append({'status': 'done', 'evidence': task.get('evidence'), 'reason': 'topology rebind', 'at': now()})
                    task['status'] = 'needs-verification'
            owner_state['status'] = 'execute'
        state['git_topology'] = topology
        state.setdefault('external_links', [])
        return {'task': state['id'], 'number': state.get('number'), 'git_topology': topology}

    def _record_topology_inflight(self, state, binding, mode, branch, workspace, base, quote):
        state['inflight'] = {'id': uuid.uuid4().hex, 'kind': 'topology', 'task': binding,
            'mode': mode, 'branch': branch, 'workspace_root': str(workspace), 'base_head': base,
            'quote': quote, 'at': now(), 'previous_status': state['status']}
        self.write(state, 'topology-start', json.dumps(state['inflight'], ensure_ascii=False),
                   action_uid=stable_action_uid('topology-start', state['inflight']['id']))

    def _topology_failed(self, state, code, detail):
        failed = state.pop('inflight')
        state['topology_failure'] = {'operation': failed, 'code': code, 'detail': redact(detail), 'at': now()}
        self.write(state, 'topology-failed', code + ' ' + redact(detail),
                   action_uid=stable_action_uid('topology-failed', failed.get('id'), code))
        raise WorkflowError(code + ' ' + detail.strip())

    def state(self, task_id, execution=False, allow_head_change=False, release_scope=False,
              allow_closed=False, recovery=False, context=None, defer_delivery=False):
        if (self.meta/'document-transaction.json').exists() or (self.meta/'document-migration.json').exists():
            raise WorkflowError('DOCUMENT_TRANSACTION_PENDING recover document operations first')
        run, doc = self.paths(task_id)
        state = read(run / 'state.json')
        ident = self.identity()
        if state.get('project_id') != ident['id'] or state.get('root') != str(self.root):
            raise WorkflowError('PROJECT_MISMATCH refusing cross-project continuation')
        # Git identity follows the task's bound source workspace.  In
        # worktree mode the control root remains on its own branch while the
        # task is executed in the child worktree.
        inventory = None
        if not (release_scope or recovery) and topology_selected(state):
            inventory = self.validate_topology(state, allow_head_change=allow_head_change)
        workspace = self.workspace_root(state, validate=False)
        current = None
        if inventory:
            row = inventory.get('current') or {}
            if row.get('root') and Path(row['root']).resolve() == workspace:
                current = {'root': row.get('root'), 'branch': row.get('branch'), 'head': row.get('head')}
        if current is None:
            current = git_identity(workspace)
        before = state.get('git', {})
        first_repository = (allow_head_change and not any(before.get(k) for k in ('root','branch','head'))
                            and current.get('root') == str(self.root))
        if not recovery and not first_repository and (current.get('root') != before.get('root') or (not release_scope and current.get('branch') != before.get('branch'))):
            raise WorkflowError('BRANCH_MISMATCH reconcile before continuing')
        if not (allow_head_change or release_scope or recovery) and current.get('head') != before.get('head'):
            raise WorkflowError('HEAD_CHANGED inspect actual changes, then observe-head with evidence and reason')
        stale = False
        if state['status'] == 'ready' and not recovery and not defer_delivery:
            try:
                stale = state.get('delivery_fingerprint') != fingerprint(self.verification_root(state, allow_head_change=allow_head_change))
                self.valid_design(state, doc)
                self.valid_plan(state, doc)
                validate_delivery(self, state, doc, allow_head_change=allow_head_change,
                                  context=context)
            except (WorkflowError, OSError):
                stale = True
        if stale:
            state['status'] = 'needs-verification'
            self.write(state,'delivery-invalidated','项目文件、设计、计划或审查已变化，需要重新验证、审查并 ready。',
                       action_uid=stable_action_uid('delivery-invalidated', state['id'],
                                                    state.get('delivery_fingerprint'), state['status']))
        if execution:
            if topology_required(state) and not topology_selected(state):
                raise WorkflowError('GIT_TOPOLOGY_REQUIRED choose and record in-place, branch or worktree before implementation')
            if state.get('work_uuid'):
                from .documents import Governance
                if not allow_closed and Governance(self.root).get(state['work_uuid'])['stage'] in ('paused','cancelled','delivered','closed'):
                    raise WorkflowError('WORK_NOT_ACTIVE resume or create related work before execution')
            self.valid_design(state, doc)
            self.valid_plan(state, doc)
        return state, run, doc

    def ledger_state(self, task_id):
        """Read the recorded ledger after the cheap project identity check."""
        return _ledger_state_at(self.root, task_id)

    def brief_state(self, task_id):
        """Read the recorded ledger only; never claim current validation."""
        return _format_brief_state(self.ledger_state(task_id))

    def valid_design(self, state, doc):
        if not state.get('design_approval') or state['design_approval']['sha256'] != digest(self.document(state, 'design')):
            raise WorkflowError('DESIGN_NOT_APPROVED current design has no matching user approval')

    def valid_plan(self, state, doc):
        plan = state.get('plan_seal')
        if not plan or plan['sha256'] != digest(self.document(state, 'plan')) or plan['tasks_sha256'] != self.task_definition_hash(state):
            raise WorkflowError('PLAN_CHANGED review changes and seal the current plan')

    @staticmethod
    def task_definition_hash(state):
        fields = []
        for task in state['tasks']:
            field = {k: task[k] for k in ('id', 'title', 'acceptance', 'depends')}
            if 'role' in task:
                field['role'] = task.get('role')
            if 'parent_task' in task:
                field['parent_task'] = task.get('parent_task')
            fields.append(field)
        return sha(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode())

    def write(self, state, event, detail='', action_uid=None):
        run, doc = self.paths(state['id'])
        candidate_state = copy.deepcopy(state)
        stable_action_uid = action_uid or candidate_state.get('journal_action_uid') or event
        if not isinstance(stable_action_uid, str) or not stable_action_uid.strip():
            raise WorkflowError('JOURNAL_ACTION_UID_REQUIRED')
        marker = candidate_state.get('_command_commit')
        if marker is not None and not isinstance(marker, dict):
            raise WorkflowError('COMMAND_REQUEST_INVALID')
        if marker is not None and marker.get('action_uid') != stable_action_uid:
            candidate_state.pop('_command_commit')
        candidate_state['updated'] = now()
        state_file = run / 'state.json'
        from .identity_store import IdentityStore, IdentityStoreError
        from .operation_journal import (begin_intent, commit_state, emit_event,
                                        finish_previous_command_projection,
                                        mark_command_projection_complete,
                                        project_write_logical_key, project_write_marker,
                                        state_digest as journal_state_digest)
        from .orchestration_types import EventEnvelope, RequestRef, digest_value
        store = IdentityStore(self.root)
        try:
            candidate_state['_command_commit'] = project_write_marker(
                candidate_state, event, redact(detail), stable_action_uid,
                doc.relative_to(self.root).as_posix(),
            )
            store.ensure_authority()
            finish_previous_command_projection(self, store, state_file, stable_action_uid)
            semantic_state = {key: value for key, value in candidate_state.items() if key != 'updated'}
            request_input = {
                'state': semantic_state,
                'event': event,
                'detail': redact(detail),
            }
            input_digest = digest_value(request_input)
            controller_generation = int(candidate_state.get('controller_generation') or 1)
            cancel_generation = int(candidate_state.get('cancel_generation') or 1)
            contract = candidate_state.get('current_contract') if isinstance(candidate_state.get('current_contract'), dict) else {}
            contract_uid = contract.get('contract_uid')
            contract_version = contract.get('version')
            contract_digest = contract.get('content_digest')
            if not (isinstance(contract_digest, str) and len(contract_digest) == 64):
                contract_uid = contract_version = contract_digest = None
            attempt_uid = candidate_state.get('attempt_uid') if isinstance(candidate_state.get('attempt_uid'), str) else None
            work_uid = candidate_state.get('work_uuid') if isinstance(candidate_state.get('work_uuid'), str) else None
            logical_key = project_write_logical_key(candidate_state['id'], stable_action_uid)
            request = RequestRef(
                request_uid=digest_value({'project_uid': store.project_uid(),
                                          'logical_key': logical_key,
                                          'operation_kind': 'project-write'}),
                logical_key=logical_key,
                input_digest=input_digest,
                project_uid=store.project_uid(),
                work_uid=work_uid,
                attempt_uid=attempt_uid,
                operation_kind='project-write',
                controller_generation=controller_generation,
                cancel_generation=cancel_generation,
                contract_uid=contract_uid,
                contract_version=contract_version,
                contract_digest=contract_digest,
            )
            operation = begin_intent(store, request, journal_state_digest(state_file))
            committed = commit_state(
                store,
                operation.operation_uid,
                state_file,
                candidate_state,
                semantic_digest=input_digest,
            )
            candidate = store.get_operation_candidate(operation.operation_uid)
            frozen_state = (
                json.loads(candidate.candidate_text)
                if candidate is not None
                else read(state_file)
            )
            frozen_marker = frozen_state.get('_command_commit') or {}
            projection_root = (frozen_marker.get('effects') or {}).get('documents_root')
            if projection_root is not None:
                selected_doc = (self.root / projection_root).resolve()
                if not inside(self.root, selected_doc):
                    raise WorkflowError('COMMAND_EFFECT_INVALID')
                doc = selected_doc
            if frozen_state['status'] not in ('ready', 'closed'):
                invalidate_delivery(
                    doc,
                    f'任务进入 {frozen_state["status"]} 阶段；原交付报告仅保留为历史记录。',
                )
            project_command_effects(self, frozen_state, doc, committed.operation_uid,
                                    store.operation_details(committed.operation_uid)['created_at'])
            # Project.write has no trusted task/dispatch identity binding.  An
            # inflight label is a plan alias, not an authoritative task UID.
            task_uid = None
            dispatch_uid = None
            event_uid = digest_value({
                'operation_uid': committed.operation_uid,
                'kind': 'project-write',
                'event': event,
                'detail': redact(detail),
            })
            envelope = EventEnvelope(
                event_uid=event_uid,
                kind='project-write',
                project_uid=store.project_uid(),
                work_uid=work_uid,
                task_uid=task_uid,
                dispatch_uid=dispatch_uid,
                attempt_uid=attempt_uid,
                contract_uid=contract_uid,
                contract_version=contract_version,
                contract_digest=contract_digest,
                controller_generation=controller_generation,
                cancel_generation=cancel_generation,
                payload={'event': event, 'detail': redact(detail),
                         'action_uid': stable_action_uid,
                         'state_digest': committed.result_ref,
                         'state_path': state_file.relative_to(self.root).as_posix()},
            )
            emit_event(store, committed.operation_uid, envelope)
            event_file = run / 'events.jsonl'
            projected = {}
            for row in store.event_projection(state_file):
                payload = row['payload']
                projected[row['event_uid']] = json.dumps({
                    'at': row['created_at'],
                    'event': payload.get('event', row['kind']),
                    'detail': payload.get('detail', ''),
                    'event_uid': row['event_uid'],
                }, ensure_ascii=False)

            existing_lines = (event_file.read_text(encoding='utf-8').splitlines(keepends=True)
                              if event_file.exists() else [])
            rendered = []
            emitted = set()
            quarantined = []
            for raw in existing_lines:
                fragment = raw.strip()
                if not fragment:
                    continue
                try:
                    value = json.loads(fragment)
                except (TypeError, ValueError):
                    value = None
                if not isinstance(value, dict):
                    quarantined.append(raw if raw.endswith(('\n', '\r')) else raw + '\n')
                    continue
                event_uid = value.get('event_uid')
                if 'event_uid' in value and (
                    not isinstance(event_uid, str) or not event_uid
                ):
                    quarantined.append(raw if raw.endswith(('\n', '\r')) else raw + '\n')
                    continue
                if event_uid in projected:
                    if event_uid not in emitted:
                        rendered.append(projected[event_uid] + '\n')
                        emitted.add(event_uid)
                else:
                    rendered.append(fragment + '\n')
            for event_uid, line in projected.items():
                if event_uid not in emitted:
                    rendered.append(line + '\n')
            if quarantined:
                quarantine = run / 'events-quarantine'
                quarantine.mkdir(parents=True, exist_ok=True)
                invalid = ''.join(quarantined)
                atomic(quarantine / (sha(invalid.encode()) + '.jsonl'), invalid)
            atomic(event_file, ''.join(rendered))
        except IdentityStoreError as exc:
            raise WorkflowError(str(exc)) from exc
        if frozen_state.get('work_uuid'):
            from .documents import Governance
            book=Governance(self.root); record=book.get(frozen_state['work_uuid'])
            if record.get('run_id')!=frozen_state['id']: raise WorkflowError('WORK_ACTIVE_RUN_MISMATCH')
            before_record=json.dumps(record,sort_keys=True)
            stages={'design':'design','plan':'approved','execute':'implementing','checking':'implementing',
                    'review':'implementing','ready':'ready','needs-verification':'implementing','blocked':'implementing'}
            if record['stage'] not in ('closed','delivered','paused','cancelled'):
                next_stage=stages.get(frozen_state['status'],'implementing')
                if record['stage'] != next_stage:
                    record.setdefault('history', []).append({'at': now(), 'from': record['stage'], 'to': next_stage,
                                                             'event': event, 'reason': redact(detail)})
                    record['stage']=next_stage
            record['documents'].update({k:v for k,v in frozen_state.get('documents',{}).items() if (self.root/v).exists()})
            if json.dumps(record,sort_keys=True)!=before_record: book._save(record)
        self.finish_write_projection(state_file, frozen_state, committed)
        state.clear()
        state.update(frozen_state)

    def finish_write_projection(self, state_file, state, committed):
        """Finish required effects; optional dashboards/index text render on request."""
        from .operation_journal import mark_command_projection_complete
        mark_command_projection_complete(state_file, state, committed)

    def dashboard(self, updated=None):
        lines = ['# GstackSP 任务索引', '', '这是给人看的入口；AI 恢复时先运行 resume，再读本任务设计、计划与状态。', '',
                 '| 任务 | 状态 | 当前任务 | 设计 | 计划 | 交付 |', '|---|---|---|---|---|---|']
        cache=self.meta/'dashboard.json'
        states=read(cache) if cache.exists() else {}
        if updated: states[updated['id']]=updated
        states=dict(sorted(states.items(),key=lambda pair:pair[1].get('updated',''),reverse=True)[:50])
        atomic(cache,states)
        for state in states.values():
            tid = state['id']
            pending = ', '.join(t['id'] + ':' + t['status'] for t in state['tasks'] if t['status'] != 'done') or '无'
            title = state['title'].replace('|', '\\|').replace('\n', ' ')
            folder=self.paths(tid)[1]
            link=lambda name: os.path.relpath(folder/name,self.root/'docs/gsp').replace('\\','/')
            title_link = f"[{title}]({link('decisions.md')})" if (folder/'decisions.md').is_file() else title
            document_cell = lambda kind, label: (f"[{label}]({self.document_link(state, kind, self.root/'docs/gsp')})"
                                                  if self.document(state, kind).is_file() else '待生成')
            delivery_cell = f"[交付]({link('delivery.md')})" if (folder/'delivery.md').is_file() else '待生成'
            lines.append(f"| {title_link} | {state['status']} | {pending} | {document_cell('design', '规格')} | {document_cell('plan', '计划')} | {delivery_cell} |")
        lines += ['', '最近最多 50 个已更新执行记录；历史使用 docs-search / docs-index。']
        atomic(self.root / 'docs/gsp/README.md', '\n'.join(lines) + '\n')

    def new(self, args):
        self.identity()
        explicit_number = explicit_work_number_from_run_id(args.id)
        active = self.meta / 'active.json'
        if active.exists():
            active_id=read(active)['task']
            orphan=(active_id==args.id and bool(getattr(args,'work',None)) and not (self.meta/'runs'/args.id/'state.json').exists())
            if not orphan:
                self.active_state()
        run, doc = self.paths(args.id)
        resume_empty=bool(getattr(args,'work',None)) and run.exists() and not any(run.iterdir())
        if (run.exists() and not resume_empty) or doc.exists():
            raise WorkflowError('TASK_EXISTS choose another task id')
        if args.mode == 'unattended' and not args.quote:
            raise WorkflowError('AUTHORIZATION_REQUIRED quote the actual user instruction for unattended mode')
        date = datetime.now().astimezone().date().isoformat()
        documents = {'design': f'docs/superpowers/specs/{date}-{args.id}-design.md',
                     'plan': f'docs/superpowers/plans/{date}-{args.id}.md'}
        record=None
        if not getattr(args,'legacy_layout',False):
            from .documents import Governance
            book=Governance(self.root)
            if getattr(args,'work',None):
                record=book.get(args.work)
                if record.get('run_id') and record['run_id']!=args.id: raise WorkflowError('WORK_ALREADY_HAS_RUN')
                if record['stage'] not in ('idea','design','paused'): raise WorkflowError('WORK_STAGE_INVALID')
                record=book.stage(record['uuid'],'design','User requested execution workflow; approval still required')
                record['run_id']=args.id;book._save(record)
            else:
                record=book.create(args.title,args.title,args.quote or 'user requested new task',run_id=args.id, number=explicit_number)
                record=book.stage(record['uuid'],'design','Begin design discussion')
            doc=self.root/record['folder']
            documents={'design':record['documents']['design'],'plan':record['folder']+'/plan.md'}
        probe = {'id': args.id, 'documents': documents}
        for kind in documents:
            file = self.document(probe, kind)
            if file.exists() and not record:
                raise WorkflowError('DOCUMENT_EXISTS refusing to overwrite ' + str(file))
        run.mkdir(parents=True,exist_ok=resume_empty)
        doc.mkdir(parents=True,exist_ok=bool(record))
        from .git_topology import probe, recommend
        git_inventory = probe(self.root)
        git_context = {
            'number': record.get('number') if record else None,
            'title': args.title,
            'intent': getattr(args, 'intent', 'single-task'),
            'size': getattr(args, 'size', 'small'),
            'independent_verification': bool(getattr(args, 'independent_verification', False)),
            'protect_existing': bool(getattr(args, 'protect_existing', False)),
        }
        git_recommendation = recommend(git_inventory, git_context)
        # Git-backed modern work items opt into the hard topology gate.  A
        # project that has never been initialized as a Git repository keeps
        # the historical compatibility path until it explicitly selects a
        # topology after Git is initialized.
        topology_enforce = bool(record and git_inventory.get('status') != 'not-a-repository')
        git_topology = {
            'schema': 1,
            'status': 'pending' if topology_enforce else 'legacy-unavailable',
            'enforce': topology_enforce,
            'intent': git_context['intent'],
            'size': git_context['size'],
            'independent_verification': git_context['independent_verification'],
            'protect_existing': git_context['protect_existing'],
            'recommended_mode': git_recommendation.get('mode'),
            'recommended_reasons': git_recommendation.get('reasons', []),
            'requested_mode': None,
            'effective_mode': None,
            'control_root': str(self.root),
            'workspace_root': str(self.root),
            'base_branch': git_inventory.get('current', {}).get('branch'),
            'base_head': git_inventory.get('current', {}).get('head'),
            'branch': git_inventory.get('current', {}).get('branch'),
            'initial_inventory': git_inventory,
            'decision_quote': None,
            'decision_source': None,
        }
        state = {'schema': 1, 'id': args.id, 'number': record.get('number') if record else None,
                 'title': args.title, 'root': str(self.root),
                 'project_id': self.identity()['id'], 'created': now(), 'status': 'design',
                 'mode': args.mode, 'mode_quote': args.quote, 'documents': documents, 'design_approval': None, 'plan_seal': None,
                 'controller_required': not getattr(args, 'legacy_layout', False),
                 'operation_schema': 2,
                 'convergence_schema': 1 if not getattr(args, 'legacy_layout', False) else None,
                 'scope': {'mode': 'pending'}, 'tasks': [], 'evidence': [], 'review': None,
                 'authorizations': [], 'external_links': [], 'git': git_identity(self.root),
                 'git_topology': git_topology}
        if record:
            state.update({'doc_dir':record['folder'],'work_uuid':record['uuid']})
        templates = {
            'design.md': f'# {args.title} — 设计\n\n状态：草稿；填写目标、范围、方案、验收、非目标。用户确认前不实施。\n',
            'plan.md': f'# {args.title} — 实施计划\n\n规格：[design.md](design.md)\n\n计划尚未编写。具体任务应登记到运行状态，写清验收。\n',
            'review.md': '# 代码审查\n\n尚未审查。记录审查者、范围、需求符合性、质量问题和未覆盖项。\n',
            'delivery.md': '# 交付\n\nNOT_READY：任务尚未验证交付。\n',
            'decisions.md': '# 决策记录\n\n每项写明选择、依据、代价、来源；用户原话与 AI 建议分别标记。\n',
        }
        for name, text in templates.items():
            if record and name in ('plan.md','review.md','delivery.md','decisions.md'):
                continue
            if name in ('design.md', 'plan.md'):
                kind = name[:-3]
                if kind == 'plan':
                    text = text.replace('(design.md)', '(' + self.document_link(state, 'design', self.document(state, 'plan').parent) + ')')
                if not self.document(state,kind).exists(): atomic(self.document(state, kind), text)
            else:
                atomic(doc / name, text)
        atomic(self.meta / 'active.json', {'task': args.id})
        self.write(state, 'created', args.quote or '',
                   action_uid=stable_action_uid('created', state['id'], state.get('number')))
        return {'task': args.id, 'documents': str(doc), 'state': str(run / 'state.json'), 'document_paths': documents}

    def control_document_paths(self, state):
        """Identify the work's control documents without granting edit rights."""
        _, docs = self.paths(state['id'])
        return frozenset(
            [self.document(state, kind).resolve() for kind in ('design', 'plan')]
            + [(docs / name).resolve()
               for name in ('review.md', 'decisions.md', 'delivery.md')]
        )

    def scope_path(self, state, path):
        """Preview workspace ownership and conflicts; this is not a write grant."""
        candidate = Path(path)
        workspace = self.workspace_root(state, validate=True)
        target = (candidate if candidate.is_absolute() else workspace / candidate).resolve()
        if target in self.control_document_paths(state):
            return target
        allowed = [workspace]
        if parallel_required(state):
            allowed = [self.workspace_root(state, task_id=t['id']) for t in state['tasks']
                       if t['status'] == 'running' and t.get('topology')]
        # A registered child worktree may live under the control root's .gsp.
        # Its source remains a workspace; its own control metadata stays protected.
        workspace_in_control_metadata = any(
            base.resolve() != self.root.resolve() and inside(self.meta, base)
            and inside(base, target) for base in allowed)
        workspace_metadata = any(
            base.resolve() != self.root.resolve()
            and (inside(base / '.git', target) or inside(base / '.gsp', target))
            for base in allowed)
        if (workspace_metadata
                or (inside(self.meta, target) and not workspace_in_control_metadata)
                or inside(self.root / '.git', target)):
            raise WorkflowError('CONTROL_PATH_PROTECTED modify workflow control through validated commands')
        if topology_required(state):
            if not topology_selected(state):
                raise WorkflowError('GIT_TOPOLOGY_REQUIRED choose a workspace before source edits')
            source_allowed = any(inside(base, target) for base in allowed)
            if not source_allowed:
                if workspace.resolve() != self.root.resolve() and inside(self.root, target):
                    raise WorkflowError('CONTROL_PATH_PROTECTED source edits must use the selected workspace')
                raise WorkflowError('GIT_WORKSPACE_PATH edit must use the selected source workspace')
            from .git_topology import bound_task_for_path
            owner = bound_task_for_path(self.root, target)
            if owner and owner != state['id']:
                raise WorkflowError('GIT_WORKSPACE_CLAIMED path belongs to task ' + owner)
            return target
        if (workspace.resolve() != self.root.resolve()
                and inside(self.root, target)
                and not inside(workspace, target)):
            raise WorkflowError('CONTROL_PATH_PROTECTED source edits must use the selected workspace')
        if not inside(workspace, target):
            raise WorkflowError(f'PATH_PREVIEW_OUTSIDE_WORKSPACE {target} is outside {workspace}')
        return target

    @staticmethod
    def item(state, task_id):
        task = next((t for t in state['tasks'] if t['id'] == task_id), None)
        if not task:
            raise WorkflowError('TASK_NOT_FOUND ' + task_id)
        return task
