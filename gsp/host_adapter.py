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
