"""Build a safe, explicit Codex worker invocation from a GSP route."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from .host_adapter import normalize_route, route_health


class HostAdapterError(ValueError):
    pass


def build_codex_worker_plan(route: Optional[Dict[str, Any]], host: Dict[str, Any],
                            worktree: os.PathLike[str] | str, prompt: str,
                            codex_executable: Optional[os.PathLike[str] | str] = None,
                            codex_home: Optional[os.PathLike[str] | str] = None) -> Dict[str, Any]:
    """Return an auditable argv/env plan without starting Codex."""
    if not isinstance(prompt, str) or not prompt.strip() or any(ord(c) < 32 and c not in "\r\n\t" for c in prompt):
        raise HostAdapterError("invalid worker prompt")
    route = normalize_route(route)
    health = route_health(route, host)
    if not health["configured"] or not health["resolvable"]:
        raise HostAdapterError("CHANGE_REQUEST: " + ",".join(health["reasons"] or ["route_not_supported"]))
    supports = host.get("cli_supports") or {}
    required = ["exec"]
    profile = route.get("model_profile")
    model = route.get("model")
    effort = route.get("reasoning_effort")
    if profile:
        required.append("profile")
    if model:
        required.append("model")
    if effort:
        required.append("reasoning_effort")
    missing = [name for name in required if not supports.get(name)]
    if missing:
        raise HostAdapterError(
            "CHANGE_REQUEST: " + ",".join("cli_does_not_support_" + name for name in missing))
    exe = str(codex_executable or host.get("cli_path") or "codex")
    argv = [exe, "exec"]
    if profile:
        argv += ["--profile", profile]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["-c", f'model_reasoning_effort="{effort}"']
    argv += ["-C", str(Path(worktree)), "-"]
    env = {}
    if codex_home:
        env["CODEX_HOME"] = str(Path(codex_home))
    return {
        "provider": route["provider"],
        "route": route,
        "argv": argv,
        "env": env,
        "stdin": prompt,
        "started": False,
        "request_verified": False,
        "status": "NOT_CHECKED",
    }
