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
ROUTE_PATTERNS = {
    "provider": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"),
    "model_profile": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"),
    "model": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$"),
    "reasoning_effort": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"),
}


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
    pattern = ROUTE_PATTERNS.get(field)
    if pattern and not pattern.fullmatch(value):
        raise ValueError(f"invalid {field}")
    return value


def normalize_route(route: Optional[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """Validate a route and drop all fields outside the public contract."""
    route = route or {}
    out: Dict[str, Optional[str]] = {}
    for key in SAFE_ROUTE_KEYS:
        out[key] = _safe_text(route.get(key), key)
    out["provider"] = out["provider"] or "inherit"
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


def _catalog_metadata(home: Path, catalog_name: Optional[str]) -> Dict[str, Any]:
    catalog_path = (home / catalog_name) if catalog_name else None
    return {
        "model_catalog": str(catalog_path) if catalog_path else None,
        "catalog_exists": bool(catalog_path and catalog_path.is_file()),
        "catalog_models": sorted(_catalog_ids(catalog_path)) if catalog_path and catalog_path.is_file() else [],
    }


def _toml_quoted_value(raw: str) -> Optional[str]:
    """Decode one simple TOML string and ignore a trailing comment."""
    value = raw.lstrip()
    if not value or value[0] not in {'"', "'"}:
        return None
    quote = value[0]
    if quote == "'":
        end = value.find("'", 1)
        if end < 0:
            return None
        decoded = value[1:end]
    else:
        escaped = False
        end = -1
        for index in range(1, len(value)):
            char = value[index]
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                end = index
                break
        if end < 0:
            return None
        try:
            decoded = json.loads(value[:end + 1])
        except (TypeError, ValueError):
            return None
    remainder = value[end + 1:].strip()
    if remainder and not remainder.startswith('#'):
        return None
    return decoded


def _config_assignment(line: str) -> tuple[Optional[str], Optional[str]]:
    match = re.match(r'^([A-Za-z0-9_.-]+)\s*=\s*(.*)$', line)
    if not match:
        return None, None
    return match.group(1), _toml_quoted_value(match.group(2))


def _provider_section_name(section: str) -> Optional[str]:
    prefix = 'model_providers.'
    if not section.startswith(prefix):
        return None
    raw = section[len(prefix):].strip()
    if not raw:
        return None
    if raw[0] in {'"', "'"}:
        return _toml_quoted_value(raw)
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', raw):
        return raw
    return None


def _config_metadata(config_path: Path) -> Dict[str, Any]:
    """Stream only approved metadata; provider bodies are never retained or parsed."""
    result: Dict[str, Any] = {
        "model": None,
        "model_provider": None,
        "model_catalog_json": None,
        "has_reasoning_effort": False,
        "providers": [],
    }
    try:
        stream = config_path.open('r', encoding='utf-8')
    except OSError:
        return result
    section = ''
    try:
        with stream:
            for line in stream:
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    continue
                section_match = re.match(r'^\[([^\[\]]+)\]\s*(?:#.*)?$', stripped)
                if section_match:
                    section = section_match.group(1).strip()
                    provider = _provider_section_name(section)
                    if provider and provider not in result["providers"]:
                        result["providers"].append(provider)
                    continue
                if section:
                    continue
                key, value = _config_assignment(stripped)
                if key == 'model_reasoning_effort':
                    result["has_reasoning_effort"] = True
                elif key in {'model', 'model_provider', 'model_catalog_json'} and value is not None:
                    result[key] = value
    except (OSError, UnicodeError):
        return result
    return result


def inspect_codex_host(codex_home: Optional[os.PathLike[str] | str] = None,
                       cli_path: Optional[os.PathLike[str] | str] = None) -> Dict[str, Any]:
    """Return non-sensitive, read-only host capability metadata."""
    home = Path(codex_home or os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    config = home / "config.toml"
    meta = _config_metadata(config)
    catalog_name = meta.get("model_catalog_json")
    base_catalog = _catalog_metadata(home, catalog_name)
    profile_paths = sorted(home.glob("*.config.toml"))
    profile_catalogs: Dict[str, Dict[str, Any]] = {}
    profile_providers: Dict[str, list[str]] = {}
    profile_model_providers: Dict[str, Optional[str]] = {}
    for profile_path in profile_paths:
        profile_name = profile_path.stem.removesuffix(".config")
        profile_meta = _config_metadata(profile_path)
        profile_catalog = _catalog_metadata(
            home, profile_meta.get("model_catalog_json") or catalog_name
        )
        profile_catalog["configured_model"] = profile_meta.get("model")
        profile_catalogs[profile_name] = profile_catalog
        profile_providers[profile_name] = sorted(profile_meta.get("providers", []))
        profile_model_providers[profile_name] = profile_meta.get("model_provider")
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
        "configured_model_provider": meta.get("model_provider"),
        **base_catalog,
        "providers": sorted(meta.get("providers", [])),
        "profiles": sorted(profile_catalogs),
        "profile_catalogs": profile_catalogs,
        "profile_providers": profile_providers,
        "profile_model_providers": profile_model_providers,
        "cli_path": str(cli) if cli else None,
        "cli_supports": cli_supports,
    }


def build_cli_report(codex_home: Optional[os.PathLike[str] | str],
                     cli_path: Optional[os.PathLike[str] | str],
                     route: Optional[Dict[str, Any]],
                     desktop_visible_models: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Build the JSON-safe payload for the read-only host diagnostics commands."""
    host = inspect_codex_host(codex_home, cli_path=cli_path)
    visible = sorted({
        value
        for value in (
            _safe_text(item, "model")
            for item in (desktop_visible_models or [])
        )
        if value
    })
    host["desktop_visible_models"] = visible
    return {
        "schema": 1,
        "host": host,
        "health": route_health(route, host),
    }


def route_health(route: Optional[Dict[str, Any]], host: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a route against host metadata without making a model request."""
    normalized = normalize_route(route)
    model = normalized.get("model")
    provider = normalized.get("provider")
    profile = normalized.get("model_profile")
    profile_providers = (host.get("profile_providers") or {}).get(profile, [])
    profile_provider = (host.get("profile_model_providers") or {}).get(profile)
    configured = (provider == "inherit"
                  or provider == host.get("configured_model_provider")
                  or provider == profile_provider
                  or provider in host.get("providers", [])
                  or provider in profile_providers)
    resolvable = True
    reasons: list[str] = []
    if not configured:
        reasons.append("provider_not_configured")
    catalog = set(host.get("catalog_models", []))
    configured_model = host.get("configured_model")
    if profile:
        profile_catalog = (host.get("profile_catalogs") or {}).get(profile)
        if isinstance(profile_catalog, dict):
            catalog = set(profile_catalog.get("catalog_models", catalog))
            configured_model = profile_catalog.get("configured_model") or configured_model
    if model:
        effective_default_provider = profile_provider or host.get("configured_model_provider")
        can_use_configured_default = provider in {"inherit", effective_default_provider}
        resolvable = model in catalog or (can_use_configured_default and model == configured_model)
        if not resolvable:
            reasons.append("model_not_in_catalog")
    if profile:
        if profile not in host.get("profiles", []):
            reasons.append("profile_not_found")
            resolvable = False
    desktop_visible = bool(model and model in set(host.get("desktop_visible_models") or []))
    return {
        "route": normalized,
        "configured": bool(configured),
        "resolvable": bool(resolvable),
        "desktop_visible": desktop_visible,
        "request_verified": False,
        "status": "NOT_CHECKED",
        "reasons": reasons,
    }
