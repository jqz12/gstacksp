"""Read-only Git worktree inspection and ownership-aware disposition."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess


OWNERSHIP_MODES = frozenset({"bound", "native", "legacy-managed"})


class WorktreeAdapterError(Exception):
    """Stable worktree adapter failure with a machine-readable prefix."""


def _git(path, *argv):
    try:
        return subprocess.run(
            ["git", *argv],
            cwd=path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            ),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise WorktreeAdapterError("WORKTREE_GIT_UNAVAILABLE") from exc


def _resolved(raw):
    try:
        return Path(raw).resolve()
    except OSError as exc:
        raise WorktreeAdapterError("WORKTREE_PATH_INVALID") from exc


def _read_project_uid(path):
    identity = Path(path) / ".gsp" / "project.json"
    if not identity.is_file():
        return None
    try:
        value = json.loads(identity.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise WorktreeAdapterError("WORKTREE_PROJECT_IDENTITY_INVALID") from exc
    project_uid = value.get("id")
    if not isinstance(project_uid, str) or not project_uid.strip():
        raise WorktreeAdapterError("WORKTREE_PROJECT_IDENTITY_INVALID")
    return project_uid


class WorktreeAdapter:
    """Verify real Git worktrees without creating, deleting or archiving them."""

    def __init__(self, project_root, expected_project_uid=None):
        self.project_root = _resolved(project_root)
        self.expected_project_uid = expected_project_uid

    def _git_identity(self, path):
        result = _git(
            path,
            "rev-parse",
            "--show-toplevel",
            "--git-common-dir",
            "--verify",
            "HEAD",
        )
        if result.returncode:
            detail = (result.stderr or result.stdout or "").strip()
            raise WorktreeAdapterError("WORKTREE_GIT_IDENTITY_INVALID " + detail)
        lines = result.stdout.splitlines()
        if len(lines) < 3:
            raise WorktreeAdapterError("WORKTREE_GIT_IDENTITY_INVALID")
        top = _resolved(lines[0].strip())
        common_raw = Path(lines[1].strip())
        common = (
            common_raw.resolve()
            if common_raw.is_absolute()
            else (path / common_raw).resolve()
        )
        branch_result = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
        branch = (
            branch_result.stdout.strip()
            if branch_result.returncode == 0
            else None
        )
        return {
            "top": top,
            "common_dir": common,
            "head": lines[2].strip(),
            "branch": branch,
        }

    @staticmethod
    def _inventory(project_root):
        result = _git(project_root, "worktree", "list", "--porcelain", "-z")
        if result.returncode:
            detail = (result.stderr or result.stdout or "").strip()
            raise WorktreeAdapterError("WORKTREE_INVENTORY_FAILED " + detail)
        rows = []
        current = None
        for line in result.stdout.split("\0"):
            if not line:
                if current is not None:
                    rows.append(current)
                    current = None
                continue
            key, _, value = line.partition(" ")
            if key == "worktree":
                if current is not None:
                    rows.append(current)
                current = {"path": _resolved(value)}
            elif current is None:
                continue
            elif key == "HEAD":
                current["head"] = value
            elif key == "branch":
                current["branch"] = value.removeprefix("refs/heads/")
            elif key == "detached":
                current["detached"] = True
        if current is not None:
            rows.append(current)
        return rows

    @staticmethod
    def _same_path(left, right):
        return os.path.normcase(str(left)) == os.path.normcase(str(right))

    def inspect(self, path):
        candidate = _resolved(path)
        if not candidate.is_dir():
            raise WorktreeAdapterError(
                "WORKTREE_NOT_REGISTERED " + str(candidate)
            )
        project = self._git_identity(self.project_root)
        try:
            worktree = self._git_identity(candidate)
        except WorktreeAdapterError as exc:
            if str(exc).startswith("WORKTREE_GIT_IDENTITY_INVALID"):
                raise WorktreeAdapterError(
                    "WORKTREE_NOT_REGISTERED " + str(candidate)
                ) from exc
            raise
        if not self._same_path(worktree["top"], candidate):
            raise WorktreeAdapterError(
                "WORKTREE_NOT_REGISTERED " + str(candidate)
            )
        if not self._same_path(
            worktree["common_dir"], project["common_dir"]
        ):
            raise WorktreeAdapterError(
                "WORKTREE_COMMON_DIR_MISMATCH "
                + str(worktree["common_dir"])
            )
        inventory = self._inventory(self.project_root)
        registered = next(
            (
                row
                for row in inventory
                if self._same_path(row["path"], candidate)
            ),
            None,
        )
        if registered is None:
            raise WorktreeAdapterError(
                "WORKTREE_NOT_REGISTERED " + str(candidate)
            )

        expected_uid = self.expected_project_uid or _read_project_uid(
            self.project_root
        )
        child_uid = _read_project_uid(candidate)
        if expected_uid and child_uid and child_uid != expected_uid:
            raise WorktreeAdapterError(
                "WORKTREE_PROJECT_UID_MISMATCH "
                + child_uid
                + " expected "
                + expected_uid
            )
        return {
            "schema": 1,
            "path": str(candidate),
            "top_level": str(worktree["top"]),
            "branch": registered.get("branch") or worktree["branch"],
            "head": registered.get("head") or worktree["head"],
            "detached": bool(registered.get("detached")),
            "registered": True,
            "common_dir": str(worktree["common_dir"]),
            "project_uid": child_uid or expected_uid,
        }

    @staticmethod
    def _disposition(ownership, binding_uid, inspection):
        if ownership == "bound":
            return {
                "cleanup_action": "release",
                "deletable": False,
                "host_action": None,
            }
        if ownership == "legacy-managed":
            return {
                "cleanup_action": "remove",
                "deletable": True,
                "host_action": None,
            }
        if ownership == "native":
            return {
                "cleanup_action": "archive",
                "deletable": False,
                "host_action": {
                    "schema": 1,
                    "kind": "worktree-archive",
                    "state": "pending",
                    "binding_uid": binding_uid,
                    "path": inspection["path"],
                    "head": inspection["head"],
                },
            }
        raise WorktreeAdapterError("WORKTREE_OWNERSHIP_INVALID")

    def bind(self, path, ownership="bound", binding_uid=None):
        if ownership not in OWNERSHIP_MODES:
            raise WorktreeAdapterError("WORKTREE_OWNERSHIP_INVALID")
        inspection = self.inspect(path)
        if binding_uid is None:
            binding_uid = hashlib.sha256(
                "\0".join(
                    (
                        inspection.get("project_uid") or "",
                        ownership,
                        inspection["path"],
                        inspection.get("branch") or "",
                        inspection.get("head") or "",
                    )
                ).encode("utf-8")
            ).hexdigest()[:24]
        if not isinstance(binding_uid, str) or not binding_uid.strip():
            raise WorktreeAdapterError("WORKTREE_BINDING_UID_INVALID")
        disposition = self._disposition(
            ownership, binding_uid, inspection
        )
        return {
            **inspection,
            "binding_uid": binding_uid,
            "ownership": ownership,
            **disposition,
        }

    @staticmethod
    def release(binding):
        if not isinstance(binding, dict):
            raise WorktreeAdapterError("WORKTREE_BINDING_INVALID")
        if binding.get("cleanup_action") != "release":
            raise WorktreeAdapterError("WORKTREE_RELEASE_INVALID")
        return {
            "schema": 1,
            "binding_uid": binding.get("binding_uid"),
            "path": binding.get("path"),
            "state": "released",
            "action": "release",
        }


def inspect_worktree(project_root, path, expected_project_uid=None):
    """Convenience entry for read-only inspection."""
    return WorktreeAdapter(
        project_root, expected_project_uid=expected_project_uid
    ).inspect(path)
