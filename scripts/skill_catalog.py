"""Compatibility forwarder for :mod:`gsp.skill_catalog`."""
from gsp import skill_catalog as provider
from gsp.skill_catalog import *  # noqa: F401,F403


_manifest = provider.load_manifest()
DEFAULT_SKILLS = tuple(
    provider.short_name(row["original_name"])
    for row in _manifest["entries"]
    if row.get("class") == "default"
)
ON_DEMAND_SKILLS = tuple(
    provider.short_name(row["original_name"])
    for row in _manifest["entries"]
    if row.get("class") == "on_demand"
)
