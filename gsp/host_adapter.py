"""Read-only Codex host capability and model-route diagnostics.

This module deliberately returns only non-sensitive metadata. It never exposes
provider URLs, bearer tokens, API keys, or other credential values.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

SAFE_ROUTE_KEYS = ("provider", "model_profile", "model", "reasoning_effort")
SUPPORTED_PROVIDER = {"inherit", "openai", "magpie", "opencode-go", "anthropic", "gemini", "custom"}


def _safe_text(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError(f"invalid {field}")
    if any(c.isspace() for c in value):
        raise ValueError(f"invalid {field}")
    return value


def normalize_route(route: Optional[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """Validate a route and drop all fields outside the public contract."""
    route = route or {}
    out: Dict[str, Optional[str]] = {}
    for key in SAFE_ROUTE_KEYS:
        out[key] = _safe_text(route.get(key), key)
    out["provider"] = out["provider"] or "inherit"
    if out["provider"] not in SUPPORTED_PROVIDER:
        raise ValueError(f"unsupported provider: {out['provider']}")
    return out


def _catalog_ids(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return set()
    items = data.get("models", []) if isinstance(data, dict) else data
    ids: set[str] = set()
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str):
                ids.add(item)
            elif isinstance(item, dict):
                # Only model identifiers are retained; arbitrary values are ignored.
                for key in ("id", "slug", "model"):
                    value = item.get(key)
                    if isinstance(value, str) and value:
                        ids.add(value)
    return ids


def _config_metadata(config_path: Path) -> Dict[str, Any]:
    """Extract known safe config metadata without returning secret values."""
    result: Dict[str, Any] = {"model": None, "model_catalog_json": None, "providers": []}
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return result
    section = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            if section.startswith("model_providers."):
                name = section.split(".", 1)[1].strip('"')
                if name and name not in result["providers"]:
                    result["providers"].append(name)
            continue
        if section:
            continue
        match = re.match(r'^(model|model_catalog_json)\s*=\s*(["\'])(.*?)\2\s*$', stripped)
        if match:
            result[match.group(1)] = match.group(3)
    return result


def inspect_codex_host(codex_home: Optional[os.PathLike[str] | str] = None,
                       cli_path: Optional[os.PathLike[str] | str] = None) -> Dict[str, Any]:
    """Return non-sensitive, read-only host capability metadata."""
    home = Path(codex_home or os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    config = home / "config.toml"
    meta = _config_metadata(config)
    catalog_name = meta.get("model_catalog_json")
    catalog_path = (home / catalog_name) if catalog_name else None
    cli = Path(cli_path) if cli_path else (Path(shutil.which("codex") or "") if shutil.which("codex") else None)
    cli_supports = {"profile": False, "model": False, "reasoning_effort": False}
    if cli and cli.exists():
        try:
            text = subprocess.run([str(cli), "--help"], capture_output=True, text=True, timeout=10, check=False).stdout
            cli_supports["profile"] = "--profile" in text
            cli_supports["model"] = "--model" in text
            cli_supports["reasoning_effort"] = "reasoning" in text.lower()
        except (OSError, subprocess.SubprocessError):
            pass
    return {
        "codex_home": str(home),
        "config_exists": config.is_file(),
        "configured_model": meta.get("model"),
        "model_catalog": str(catalog_path) if catalog_path else None,
        "catalog_exists": bool(catalog_path and catalog_path.is_file()),
        "catalog_models": sorted(_catalog_ids(catalog_path)) if catalog_path and catalog_path.is_file() else [],
        "providers": sorted(meta.get("providers", [])),
        "profiles": sorted(p.stem.removesuffix(".config") for p in home.glob("*.config.toml")),
        "cli_path": str(cli) if cli else None,
        "cli_supports": cli_supports,
    }


def route_health(route: Optional[Dict[str, Any]], host: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a route against host metadata without making a model request."""
    normalized = normalize_route(route)
    model = normalized.get("model")
    provider = normalized.get("provider")
    configured = provider == "inherit" or provider in host.get("providers", []) or provider == "magpie"
    resolvable = True
    reasons: list[str] = []
    if model:
        catalog = set(host.get("catalog_models", []))
        resolvable = model in catalog or normalized.get("provider") == "inherit"
        if not resolvable:
            reasons.append("model_not_in_catalog")
    if normalized.get("model_profile"):
        profile = normalized["model_profile"]
        if profile not in host.get("profiles", []):
            reasons.append("profile_not_found")
            resolvable = False
    desktop_visible = bool(model and model in set(host.get("catalog_models", [])))
    return {
        "route": normalized,
        "configured": bool(configured),
        "resolvable": bool(resolvable),
        "desktop_visible": desktop_visible,
        "request_verified": False,
        "status": "NOT_CHECKED",
        "reasons": reasons,
    }
