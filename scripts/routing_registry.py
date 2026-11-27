"""Compatibility CLI for the installed GSP routing registry."""
from pathlib import Path
import json
import sys
from dataclasses import asdict, replace

# When invoked as ``python scripts/routing_registry.py``, Python puts the
# scripts directory first and its gsp.py would shadow the gsp package.
ROOT = Path(__file__).resolve().parents[1]
sys.path = [entry for entry in sys.path if Path(entry or ".").resolve() != ROOT / "scripts"]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gsp.routing import ROUTES, Route, route_dicts, route_for, validate_routes
from scripts import skill_catalog


def validate_generated_catalog(root):
    catalog = json.loads((Path(root) / "catalog.json").read_text(encoding="utf-8"))
    errors = skill_catalog.validate_catalog(catalog)
    known = {item["name"] for item in catalog.get("skills", [])}
    errors.extend(validate_routes(known))
    return errors

__all__ = ["ROUTES", "Route", "route_dicts", "route_for", "validate_generated_catalog", "validate_routes"]

if __name__ == "__main__":
    import argparse, json
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    errors = validate_generated_catalog(Path(args.root).resolve())
    print(json.dumps({"routes": len(ROUTES), "errors": errors, "valid": not errors}, ensure_ascii=False))
    raise SystemExit(1 if errors else 0)
