"""project resolution; shared business implementation, independent of CLI."""
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

from .core import (WorkflowError, inside, read)
from .git_operations import (_discovery_ceilings, _git_run)

def find_project_root(cwd, *, include_linked=False):
    """Resolve the nearest GSP project without crossing the discovery ceiling."""
    cwd = Path(cwd).resolve()
    ceilings = _discovery_ceilings('GSP_CEILING_DIRECTORIES')
    for path in (cwd, *cwd.parents):
        if (path / '.gsp' / 'project.json').is_file():
            # The hook must not accept a local/split marker that the common-root
            # authority rejects. The default legacy lookup remains unchanged.
            return ProjectLocation(path).root if include_linked else path
        if path in ceilings:
            return None
        if include_linked and (path / '.git').is_file():
            # Discovery is not native adoption or a write grant. Delegate all
            # common-dir/registration/split-authority proof to the one existing
            # control-root resolver rather than trusting a copied Git marker.
            project = ProjectLocation(path)
            if project.root != path and (project.meta / 'project.json').is_file():
                return project.root
            return None
    return None


class ProjectLocation:
    def __init__(self, root):
        invocation_root = Path(root).resolve()
        self.invocation_root = invocation_root
        self.root = invocation_root
        self.meta = invocation_root / '.gsp'
        if not inside(self.root, self.meta) or not inside(self.root, self.root / 'docs/gsp'):
            raise WorkflowError('PROJECT_PATH_ESCAPE workflow storage resolves outside project')
        marker = invocation_root / '.git'

        def ancestor_marker():
            ceilings = _discovery_ceilings('GIT_CEILING_DIRECTORIES')
            if invocation_root in ceilings:
                return None, None
            for parent in invocation_root.parents:
                if parent in ceilings:
                    break
                candidate = parent / '.git'
                if candidate.exists():
                    return parent, candidate
            return None, None

        def linked_split_surface():
            meta = invocation_root / '.gsp'
            surfaces = (
                meta / 'project.json',
                meta / 'orchestration/registry.sqlite',
                meta / 'orchestration/registry.json',
            )
            found = next((path for path in surfaces if path.exists()), None)
            if found is not None:
                return found
            from .identity_store import IdentityStore
            legacy = IdentityStore(invocation_root).local_allocator_surfaces()
            return invocation_root / legacy[0] if legacy else None

        def verify_checkout():
            try:
                proof = _git_run(invocation_root, 'rev-parse',
                                 '--is-inside-work-tree', '--show-toplevel',
                                 '--show-superproject-working-tree')
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise WorkflowError('CONTROL_ROOT_TOPOLOGY_UNSUPPORTED cannot verify Git checkout') from exc
            facts = [line.strip() for line in proof.stdout.splitlines() if line.strip()]
            if proof.returncode or len(facts) < 2 or facts[0] != 'true' or Path(facts[1]).resolve() != invocation_root:
                raise WorkflowError('CONTROL_ROOT_TOPOLOGY_UNSUPPORTED damaged or unproven Git checkout')
            if len(facts) != 2:
                raise WorkflowError('CONTROL_ROOT_TOPOLOGY_UNSUPPORTED submodules are not supported')

        if marker.is_dir():
            verify_checkout()
            parent, _ = ancestor_marker()
            if parent is not None:
                raise WorkflowError(
                    'CONTROL_ROOT_TOPOLOGY_UNSUPPORTED nested Git repositories are not supported'
                )
            if not (marker / 'HEAD').is_file():
                raise WorkflowError(
                    'CONTROL_ROOT_TOPOLOGY_UNSUPPORTED damaged Git metadata is not supported'
                )
            return

        if not marker.exists():
            parent, _ = ancestor_marker()
            if parent is not None:
                raise WorkflowError(
                    'CONTROL_ROOT_TOPOLOGY_UNSUPPORTED repository subdirectories are not supported'
                )
            if (
                (invocation_root / 'HEAD').is_file()
                and (invocation_root / 'objects').is_dir()
                and (invocation_root / 'refs').is_dir()
            ):
                raise WorkflowError(
                    'CONTROL_ROOT_TOPOLOGY_UNSUPPORTED bare repositories are not supported'
                )
            return

        if not marker.is_file():
            raise WorkflowError(
                'CONTROL_ROOT_TOPOLOGY_UNSUPPORTED unrecognized Git marker'
            )
        try:
            marker_text = marker.read_text(encoding='utf-8').strip()
        except OSError as exc:
            raise WorkflowError(
                'CONTROL_ROOT_TOPOLOGY_UNSUPPORTED cannot read Git marker'
            ) from exc
        if not marker_text.startswith('gitdir: '):
            raise WorkflowError(
                'CONTROL_ROOT_TOPOLOGY_UNSUPPORTED invalid Git marker'
            )
        marker_path = Path(marker_text[8:].strip())
        if not marker_path.is_absolute():
            marker_path = invocation_root / marker_path
        try:
            marker_path = marker_path.resolve()
        except OSError as exc:
            raise WorkflowError(
                'CONTROL_ROOT_TOPOLOGY_UNSUPPORTED cannot resolve Git marker'
            ) from exc
        parts = tuple(part.lower() for part in marker_path.parts)
        if any(
            parts[index] == '.git' and parts[index + 1] == 'modules'
            for index in range(len(parts) - 1)
        ):
            raise WorkflowError(
                'CONTROL_ROOT_TOPOLOGY_UNSUPPORTED submodules are not supported'
            )

        standard_linked = (
            marker_path.parent.name.lower() == 'worktrees'
            and marker_path.parent.parent.name.lower() == '.git'
        )
        verify_checkout()
        if not standard_linked:
            parent, _ = ancestor_marker()
            if parent is not None:
                raise WorkflowError('CONTROL_ROOT_TOPOLOGY_UNSUPPORTED nested Git repositories are not supported')
            if (marker_path / 'commondir').exists():
                raise WorkflowError(
                    'CONTROL_ROOT_TOPOLOGY_UNSUPPORTED unproven linked worktree'
                )
            if not (marker_path / 'HEAD').is_file():
                raise WorkflowError(
                    'CONTROL_ROOT_TOPOLOGY_UNSUPPORTED damaged Git metadata is not supported'
                )
            return

        try:
            probe = _git_run(
                invocation_root,
                'rev-parse',
                '--is-bare-repository',
                '--show-toplevel',
                '--git-common-dir',
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkflowError(
                'CONTROL_ROOT_TOPOLOGY_UNSUPPORTED cannot verify linked worktree'
            ) from exc
        probe_lines = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
        if probe.returncode or len(probe_lines) != 3:
            raise WorkflowError(
                'CONTROL_ROOT_TOPOLOGY_UNSUPPORTED cannot verify linked worktree'
            )
        bare, top, common = probe_lines
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = invocation_root / common_path
        common_path = common_path.resolve()
        if (
            bare != 'false'
            or Path(top).resolve() != invocation_root
            or marker_path.parent.parent != common_path
        ):
            raise WorkflowError(
                'CONTROL_ROOT_TOPOLOGY_AMBIGUOUS linked worktree proof is inconsistent'
            )

        try:
            worktrees = _git_run(invocation_root, 'worktree', 'list', '--porcelain')
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkflowError(
                'CONTROL_ROOT_TOPOLOGY_UNSUPPORTED cannot enumerate linked worktrees'
            ) from exc
        if worktrees.returncode:
            raise WorkflowError(
                'CONTROL_ROOT_TOPOLOGY_UNSUPPORTED cannot enumerate linked worktrees'
            )
        rows = []
        row = {}
        for line in worktrees.stdout.splitlines() + ['']:
            if not line:
                if row:
                    rows.append(row)
                    row = {}
            elif line.startswith('worktree '):
                row['path'] = Path(line[9:]).resolve()
            elif line == 'bare':
                row['bare'] = True
        invocation_rows = [
            row for row in rows
            if row.get('path') == invocation_root and not row.get('bare')
        ]
        if len(invocation_rows) != 1:
            raise WorkflowError(
                'CONTROL_ROOT_TOPOLOGY_AMBIGUOUS invocation is not a registered worktree'
            )
        primary_rows = []
        for candidate in rows:
            candidate_git = candidate['path'] / '.git'
            if (
                not candidate.get('bare')
                and candidate_git.is_dir()
                and candidate_git.resolve() == common_path
            ):
                primary_rows.append(candidate['path'])
        if len(primary_rows) != 1:
            raise WorkflowError(
                'CONTROL_ROOT_TOPOLOGY_AMBIGUOUS registered primary is not unique'
            )
        primary = primary_rows[0]
        if primary == invocation_root:
            raise WorkflowError(
                'CONTROL_ROOT_TOPOLOGY_AMBIGUOUS ordinary checkout is not a linked worktree'
            )
        split_surface = linked_split_surface()
        if split_surface is not None:
            raise WorkflowError(
                'CONTROL_ROOT_SPLIT_AUTHORITY linked worktree has independent identity or allocator'
            )
        self.root = primary
        self.meta = primary / '.gsp'
        ident = primary / '.gsp/project.json'
        if not ident.is_file():
            self._control_root_uninitialized = True
        else:
            identity = read(ident)
            if identity.get('root') != str(primary):
                raise WorkflowError(
                    'CONTROL_ROOT_TOPOLOGY_AMBIGUOUS primary project identity points elsewhere'
                )
        if not inside(self.root, self.meta) or not inside(self.root, self.root / 'docs/gsp'):
            raise WorkflowError('PROJECT_PATH_ESCAPE workflow storage resolves outside project')
