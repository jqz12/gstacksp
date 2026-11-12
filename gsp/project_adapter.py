"""Project-declared language and UI capabilities for the acceptance protocol."""
import json
from pathlib import Path
from typing import Any

UI_TYPES = {"web", "desktop", "cli", "none"}
FIELDS = {"language", "commands", "ui", "parser"}
COMMANDS = {"test", "build"}


def validate_adapter(value: dict[str, Any]) -> None:
    if not isinstance(value, dict):
        raise ValueError("adapter must be an object")
    unknown = set(value) - FIELDS
    if unknown:
        raise ValueError("unknown adapter fields: " + ", ".join(sorted(unknown)))
    language = value.get("language")
    if not isinstance(language, str) or not language.strip():
        raise ValueError("language must be a non-empty string")
    commands = value.get("commands", {})
    if not isinstance(commands, dict) or set(commands) - COMMANDS:
        raise ValueError("commands must contain only test/build arrays")
    for name, argv in commands.items():
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or not arg for arg in argv):
            raise ValueError(f"commands.{name} must be a non-empty argv list")
    ui = value.get("ui", {"type": "none"})
    if not isinstance(ui, dict) or set(ui) != {"type"} or ui.get("type") not in UI_TYPES:
        raise ValueError("ui.type must be web, desktop, cli or none")
    parser = value.get("parser")
    if parser is not None and (not isinstance(parser, str) or not parser.strip()):
        raise ValueError("parser must be a non-empty string")


def load_adapter(root: Path, relative: str = ".gsp/adapter.json") -> dict[str, Any]:
    path = (Path(root) / relative).resolve()
    if not path.is_file():
        return {"status": "not_checked", "reason": "adapter not declared"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid adapter: {exc}") from exc
    validate_adapter(value)
    return {**value, "status": "declared"}
