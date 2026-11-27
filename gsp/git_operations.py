"""git operations; shared business implementation, independent of CLI."""
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

from .core import (cached_value)
_GIT_MUTATIONS = {'add', 'am', 'branch', 'checkout', 'cherry-pick', 'clean',
                  'commit', 'fetch', 'merge', 'mv', 'pull', 'push', 'rebase',
                  'reset', 'restore', 'revert', 'rm', 'stash', 'switch', 'tag',
                  'worktree'}

def _discovery_ceilings(name):
    result = []
    for value in os.environ.get(name, '').split(os.pathsep):
        if value.strip():
            try:
                result.append(Path(value).resolve())
            except OSError:
                pass
    return result


def visible_git_marker(root):
    """Return whether a .git marker is visible above root within the ceiling."""
    root = Path(root).resolve()
    ceilings = _discovery_ceilings('GIT_CEILING_DIRECTORIES')
    for path in (root, *root.parents):
        if (path / '.git').exists():
            return True
        if path in ceilings:
            return False
    return False


def head_branch(root, gitdir):
    """Read the checked-out branch from HEAD; None when detached or unknown."""
    if not gitdir:
        return None
    path = Path(gitdir)
    if not path.is_absolute():
        path = Path(root) / path
    try:
        text = (path / 'HEAD').read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return None
    if text.startswith('ref: '):
        ref = text[5:].strip()
        if ref.startswith('refs/heads/'):
            return ref[len('refs/heads/'):]
        return None
    return None


def _git_identity_uncached(root):
    def call(*args):
        try:
            return subprocess.run(['git', *args], cwd=root, capture_output=True, text=True,
                                  encoding='utf-8', errors='replace', timeout=15,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except (OSError, subprocess.TimeoutExpired):
            return None
    probe = call('rev-parse', '--show-toplevel', '--git-common-dir', '--git-dir', '--verify', 'HEAD')
    if probe is None:
        return {'root': None, 'branch': None, 'head': None}
    lines = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
    top = lines[0] if lines else None
    if not top:
        return {'root': None, 'branch': None, 'head': None}
    gitdir = lines[2] if len(lines) >= 3 else None
    head = lines[3] if probe.returncode == 0 and len(lines) >= 4 else None
    branch = head_branch(top, gitdir)
    return {'root': str(Path(top).resolve()), 'branch': branch, 'head': head}


def git_identity(root):
    root = Path(root).resolve()
    return cached_value('git_identity', str(root),
                        lambda: _git_identity_uncached(root))


def _git_run(root, *args):
    """Run one bounded local Git command without invoking a shell."""
    result = subprocess.run(
        ['git', *args], cwd=Path(root), capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    if any(str(arg) in _GIT_MUTATIONS for arg in args):
        invalidate('git:' + ' '.join(str(arg) for arg in args[:2]))
    return result


def is_ancestor(root, ancestor, descendant):
    """Cache one read-only ancestry probe inside the active command phase."""
    if not ancestor or not descendant:
        return None
    key = (str(Path(root).resolve()), str(ancestor), str(descendant))
    return cached_value('ancestor', key,
                        lambda: _git_run(root, 'merge-base', '--is-ancestor',
                                         ancestor, descendant))
