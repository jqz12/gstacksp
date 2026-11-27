"""Read-only provider for the canonical finite skill manifest.

``skills/skill-manifest.json`` is the only editable skill inventory.  Generated
catalogs, installed manifests and package file lists are projections of this
module's validated view of that file.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable


MANIFEST_REL = "skills/skill-manifest.json"
INSTALLED_MANIFEST_REL = "skill-manifest.json"
CONTEXT_SOURCE_NAMES = frozenset({"gsp-context-save", "gsp-context-restore"})
CONTEXT_RESOURCE_PATH = "skills/gsp-workflow/references/contract.md"
METHOD_ROOT = "skills/gsp-workflow/methods"
RETIRED_PAYLOAD_RESOURCES = frozenset({
    "skills/gsp-workflow/references/learnings.md",
})
CLASS_COUNTS = {
    "default": 6,
    "on_demand": 4,
    "internal": 28,
    "retired": 12,
}
_HEX64 = re.compile(r"[a-f0-9]{64}")
_NAME = re.compile(r"gsp-[a-z0-9-]+")


class SkillCatalogError(ValueError):
    """The canonical manifest or its projection is invalid."""


@dataclass(frozen=True)
class ManifestEntry:
    name: str
    old_pack: str
    class_name: str
    source_kind: str
    source_provenance: tuple[str, ...]
    source_digest: str
    source_files: tuple[dict[str, str], ...]
    public_paths: tuple[str, ...]
    payload_root: str | None
    internal_method_path: str | None
    resource_digest: str
    stage_membership: tuple[str, ...]
    trigger: str
    shared_artifact: bool
    replaced_old_path: str
    attachment_refs: tuple[str, ...]
    retired_artifacts: tuple[str, ...]
    install_selection: str
    replacement: str | None
    public_order: int | None
    method_order: int | None


def short_name(name: str) -> str:
    return name[4:] if name.startswith("gsp-") else name


def full_name(name: str) -> str:
    return name if name.startswith("gsp-") else "gsp-" + name


def _default_root() -> Path:
    return Path(__file__).resolve().parents[1]


def manifest_path(root: str | Path | None = None) -> Path:
    candidate = Path(root) if root is not None else _default_root()
    if candidate.is_file():
        return candidate.resolve()
    source = candidate / MANIFEST_REL
    installed = candidate / INSTALLED_MANIFEST_REL
    if source.is_file():
        return source.resolve()
    if installed.is_file():
        return installed.resolve()
    raise FileNotFoundError(f"skill manifest not found under {candidate}")


def load_manifest(root: str | Path | None = None) -> dict[str, Any]:
    path = manifest_path(root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SkillCatalogError(f"skill manifest cannot be read: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SkillCatalogError("skill manifest root must be an object")
    return value


def _safe_rel(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts and path.as_posix() == value


def _string_list(row: dict[str, Any], field: str, errors: list[str]) -> list[str]:
    value = row.get(field)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        errors.append(f"{row.get('original_name', '?')}: {field} must be a string list")
        return []
    return value


def validate_manifest(
    manifest: dict[str, Any],
    root: str | Path | None = None,
    *,
    check_digests: bool = False,
) -> list[str]:
    """Return deterministic manifest errors; an empty list means valid."""
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest root must be an object"]
    if manifest.get("schema") != 1:
        errors.append("manifest schema must be 1")
    if not isinstance(manifest.get("version"), str) or not manifest["version"]:
        errors.append("manifest version must be a non-empty string")
    rows = manifest.get("entries")
    if not isinstance(rows, list):
        return errors + ["manifest entries must be a list"]
    if len(rows) != 50:
        errors.append(f"manifest entries count {len(rows)} != 50")

    names: list[str] = []
    source_paths: list[str] = []
    classes: dict[str, int] = {key: 0 for key in CLASS_COUNTS}
    entries: list[ManifestEntry] = []
    for index, row in enumerate(rows):
        label = f"entry {index}"
        if not isinstance(row, dict):
            errors.append(f"{label}: must be an object")
            continue
        name = row.get("original_name")
        if not isinstance(name, str) or _NAME.fullmatch(name) is None:
            errors.append(f"{label}: invalid original_name")
            continue
        label = name
        names.append(name)
        class_name = row.get("class")
        if class_name not in CLASS_COUNTS:
            errors.append(f"{label}: invalid class {class_name!r}")
            continue
        classes[class_name] += 1
        if row.get("old_pack") not in ("core", "optional"):
            errors.append(f"{label}: old_pack must be core or optional")
        for field in ("source_kind", "source_digest", "replaced_old_path", "install_selection"):
            if not isinstance(row.get(field), str) or not row[field]:
                errors.append(f"{label}: {field} must be a non-empty string")
        digest = row.get("source_digest")
        if not isinstance(digest, str) or _HEX64.fullmatch(digest) is None:
            errors.append(f"{label}: source_digest must be sha256")
        provenance = _string_list(row, "source_provenance", errors)
        stage = _string_list(row, "stage_membership", errors)
        attachments = _string_list(row, "attachment_refs", errors)
        retired = _string_list(row, "retired_artifacts", errors)
        public = _string_list(row, "public_paths", errors)
        for value in provenance:
            if ":" not in value:
                errors.append(f"{label}: invalid provenance {value!r}")
        for field in ("attachment_refs", "retired_artifacts", "public_paths"):
            for value in row.get(field, ()):
                if isinstance(value, str) and not _safe_rel(value):
                    errors.append(f"{label}: unsafe {field} path {value!r}")
        if not isinstance(row.get("trigger"), str) or not row["trigger"].strip():
            errors.append(f"{label}: trigger must be non-empty")
        if not isinstance(row.get("shared_artifact"), bool):
            errors.append(f"{label}: shared_artifact must be boolean")
        resource_digest = row.get("resource_digest")
        if not isinstance(resource_digest, str) or _HEX64.fullmatch(resource_digest) is None:
            errors.append(f"{label}: resource_digest must be sha256")
        replacement = row.get("replacement")
        if replacement is not None and not isinstance(replacement, str):
            errors.append(f"{label}: replacement must be a skill name or null")
        source_files = row.get("source_files")
        if not isinstance(source_files, list) or len(source_files) != 2:
            errors.append(f"{label}: source_files must contain SKILL.md and agents/openai.yaml")
            source_files = []
        for source in source_files:
            if not isinstance(source, dict):
                errors.append(f"{label}: source file entry must be an object")
                continue
            path = source.get("path")
            digest_value = source.get("digest")
            if not _safe_rel(path):
                errors.append(f"{label}: unsafe source path {path!r}")
            elif path in source_paths:
                errors.append(f"{label}: duplicate source path {path}")
            else:
                source_paths.append(path)
            if not isinstance(digest_value, str) or _HEX64.fullmatch(digest_value) is None:
                errors.append(f"{label}: source file digest must be sha256")
        expected_source_files = [
            f"skills/{name}/SKILL.md",
            f"skills/{name}/agents/openai.yaml",
        ]
        if [source.get("path") for source in source_files if isinstance(source, dict)] != expected_source_files:
            errors.append(f"{label}: source_files do not match the original two skill paths")

        public_root = row.get("payload_root")
        method_path = row.get("internal_method_path")
        if public_root is not None and not _safe_rel(public_root):
            errors.append(f"{label}: unsafe payload_root {public_root!r}")
        if method_path is not None and not _safe_rel(method_path):
            errors.append(f"{label}: unsafe internal_method_path {method_path!r}")

        if class_name in ("default", "on_demand"):
            if len(public) != 2 or not public_root:
                errors.append(f"{label}: public entry needs two public paths and payload_root")
            if method_path is not None or row.get("shared_artifact"):
                errors.append(f"{label}: public entry cannot have a method resource")
            expected_selection = "default" if class_name == "default" else "on_demand"
            if row.get("install_selection") != expected_selection:
                errors.append(f"{label}: install_selection must be {expected_selection}")
            if retired:
                errors.append(f"{label}: public entry cannot retire itself")
            if not isinstance(row.get("public_order"), int):
                errors.append(f"{label}: public_order must be an integer")
            if public != expected_source_files:
                errors.append(f"{label}: public_paths do not match the current two entry paths")
        elif class_name == "internal":
            if public or public_root is not None:
                errors.append(f"{label}: internal entry cannot remain publicly discoverable")
            if not method_path:
                errors.append(f"{label}: internal entry requires internal_method_path")
            if row.get("install_selection") != "resource":
                errors.append(f"{label}: internal install_selection must be resource")
            if len(retired) != 2:
                errors.append(f"{label}: internal entry must retire two old entry files")
            if retired != expected_source_files:
                errors.append(f"{label}: retired_artifacts do not match the original two entry paths")
            if not isinstance(row.get("method_order"), int):
                errors.append(f"{label}: method_order must be an integer")
        else:
            if public or public_root is not None or method_path is not None:
                errors.append(f"{label}: retired entry cannot have active paths")
            if row.get("install_selection") != "never":
                errors.append(f"{label}: retired install_selection must be never")
            if len(retired) != 2:
                errors.append(f"{label}: retired entry must retire two old entry files")
            if retired != expected_source_files:
                errors.append(f"{label}: retired_artifacts do not match the original two entry paths")
            if row.get("shared_artifact"):
                errors.append(f"{label}: retired entry cannot own a shared artifact")

        if row.get("class") and name in CONTEXT_SOURCE_NAMES:
            if class_name != "internal" or method_path != CONTEXT_RESOURCE_PATH:
                errors.append(
                    f"{label}: context entry must use {CONTEXT_RESOURCE_PATH}"
                )
            if row.get("shared_artifact") is not True:
                errors.append(f"{label}: context entry must be a shared artifact")

        try:
            entries.append(
                ManifestEntry(
                    name=name,
                    old_pack=row["old_pack"],
                    class_name=class_name,
                    source_kind=row["source_kind"],
                    source_provenance=tuple(row.get("source_provenance", ())),
                    source_digest=row["source_digest"],
                    source_files=tuple(row.get("source_files", ())),
                    public_paths=tuple(public),
                    payload_root=public_root,
                    internal_method_path=method_path,
                    resource_digest=row["resource_digest"],
                    stage_membership=tuple(stage),
                    trigger=row["trigger"],
                    shared_artifact=row["shared_artifact"],
                    replaced_old_path=row["replaced_old_path"],
                    attachment_refs=tuple(attachments),
                    retired_artifacts=tuple(retired),
                    install_selection=row["install_selection"],
                    replacement=replacement,
                    public_order=row.get("public_order"),
                    method_order=row.get("method_order"),
                )
            )
        except KeyError:
            pass

    if len(names) != len(set(names)):
        errors.append("manifest original_name values must be unique")
    if len(source_paths) != len(set(source_paths)):
        errors.append("manifest source paths must be unique")
    for class_name, expected in CLASS_COUNTS.items():
        if classes[class_name] != expected:
            errors.append(
                f"manifest class {class_name} count {classes[class_name]} != {expected}"
            )
    known_names = set(names)
    for entry in entries:
        if entry.replacement is not None and entry.replacement not in known_names:
            errors.append(f"{entry.name}: unknown replacement {entry.replacement}")
        if entry.class_name in ("default", "on_demand", "internal"):
            if not entry.stage_membership or full_name(entry.stage_membership[0]) not in known_names:
                errors.append(f"{entry.name}: unknown stage owner")
    public_orders = [
        entry.public_order for entry in entries
        if entry.class_name in ("default", "on_demand")
    ]
    method_orders = [
        entry.method_order for entry in entries
        if entry.class_name == "internal"
    ]
    if len(public_orders) != len(set(public_orders)):
        errors.append("manifest public_order values must be unique")
    if len(method_orders) != len(set(method_orders)):
        errors.append("manifest method_order values must be unique")

    internal = [entry for entry in entries if entry.class_name == "internal"]
    context = [entry for entry in internal if entry.name in CONTEXT_SOURCE_NAMES]
    if {entry.name for entry in context} != CONTEXT_SOURCE_NAMES:
        errors.append("manifest is missing a context source duty")
    if any(
        entry.name not in CONTEXT_SOURCE_NAMES and entry.shared_artifact
        for entry in internal
    ):
        errors.append("only context source duties may share a physical resource")
    distinct = {entry.internal_method_path for entry in internal if entry.internal_method_path}
    method_files = {
        path for path in distinct
        if path and path.startswith(METHOD_ROOT + "/")
    }
    if len(method_files) != 26:
        errors.append(f"manifest physical METHOD count {len(method_files)} != 26")
    if len(distinct) != 27:
        errors.append(f"manifest distinct internal resource count {len(distinct)} != 27")
    context_paths = {entry.internal_method_path for entry in context}
    if context_paths != {CONTEXT_RESOURCE_PATH}:
        errors.append(f"context resources must share {CONTEXT_RESOURCE_PATH}")
    context_digests = {entry.resource_digest for entry in context}
    if len(context_digests) != 1:
        errors.append("context source duties must record one shared resource_digest")
    context_sources = {entry.source_digest for entry in context}
    if len(context_sources) != 2:
        errors.append("context source duties must retain distinct source digests")

    if root is not None and check_digests:
        base = Path(root).resolve()
        for entry in entries:
            if entry.class_name in ("default", "on_demand"):
                for rel in entry.public_paths:
                    path = base / rel
                    if not path.is_file():
                        errors.append(f"{entry.name}: missing public path {rel}")
            elif entry.internal_method_path:
                path = base / entry.internal_method_path
                if not path.is_file():
                    errors.append(f"{entry.name}: missing method resource {entry.internal_method_path}")
                elif hashlib.sha256(path.read_bytes()).hexdigest() != entry.resource_digest:
                    errors.append(f"{entry.name}: resource digest mismatch {entry.internal_method_path}")
            for rel in entry.retired_artifacts:
                if (base / rel).exists():
                    errors.append(f"{entry.name}: retired path still exists {rel}")
        method_dir = base / METHOD_ROOT
        actual_methods = {
            path.relative_to(base).as_posix()
            for path in method_dir.glob("*/METHOD.md")
        } if method_dir.is_dir() else set()
        if actual_methods != method_files:
            errors.append(
                "physical METHOD projection differs from manifest: "
                f"missing={sorted(method_files - actual_methods)} "
                f"extra={sorted(actual_methods - method_files)}"
            )
    return errors


def _validated(manifest: dict[str, Any]) -> dict[str, Any]:
    errors = validate_manifest(manifest)
    if errors:
        raise SkillCatalogError("; ".join(errors))
    return manifest


def _all_entries(manifest: dict[str, Any]) -> tuple[ManifestEntry, ...]:
    _validated(manifest)
    result = []
    for row in manifest["entries"]:
        result.append(
            ManifestEntry(
                name=row["original_name"],
                old_pack=row["old_pack"],
                class_name=row["class"],
                source_kind=row["source_kind"],
                source_provenance=tuple(row["source_provenance"]),
                source_digest=row["source_digest"],
                source_files=tuple(row["source_files"]),
                public_paths=tuple(row["public_paths"]),
                payload_root=row.get("payload_root"),
                internal_method_path=row.get("internal_method_path"),
                resource_digest=row["resource_digest"],
                stage_membership=tuple(row["stage_membership"]),
                trigger=row["trigger"],
                shared_artifact=row["shared_artifact"],
                replaced_old_path=row["replaced_old_path"],
                attachment_refs=tuple(row["attachment_refs"]),
                retired_artifacts=tuple(row["retired_artifacts"]),
                install_selection=row["install_selection"],
                replacement=row.get("replacement"),
                public_order=row.get("public_order"),
                method_order=row.get("method_order"),
            )
        )
    return tuple(result)


def names_for_class(manifest: dict[str, Any], *classes: str) -> tuple[str, ...]:
    return tuple(
        entry.name for entry in _all_entries(manifest)
        if not classes or entry.class_name in classes
    )


def entry_for(manifest: dict[str, Any], name: str) -> ManifestEntry:
    target = full_name(name)
    for entry in _all_entries(manifest):
        if entry.name == target:
            return entry
    raise KeyError(target)


def public_entries(manifest: dict[str, Any]) -> tuple[ManifestEntry, ...]:
    rows = [
        entry for entry in _all_entries(manifest)
        if entry.class_name in ("default", "on_demand")
    ]
    return tuple(sorted(rows, key=lambda entry: entry.public_order or 0))


def stage_methods(
    manifest: dict[str, Any],
    stage: str | None = None,
) -> tuple[ManifestEntry, ...]:
    rows = [
        entry for entry in _all_entries(manifest)
        if entry.class_name == "internal"
    ]
    if stage is not None:
        target = full_name(stage)
        rows = [
            entry for entry in rows
            if full_name(entry.stage_membership[0]) == target
        ]
    return tuple(sorted(rows, key=lambda entry: entry.method_order or 0))


def install_selection(
    manifest: dict[str, Any],
    explicit_optional: Iterable[str] = (),
) -> tuple[str, ...]:
    public = public_entries(manifest)
    defaults = [entry.name for entry in public if entry.class_name == "default"]
    optional = [entry.name for entry in public if entry.class_name == "on_demand"]
    aliases = {short_name(name): name for name in optional}
    selected = set(defaults)
    for value in explicit_optional:
        if not isinstance(value, str):
            raise SkillCatalogError("optional skill selection must be a string")
        name = aliases.get(short_name(value))
        if name is None:
            raise SkillCatalogError("UNKNOWN_OPTIONAL " + value)
        selected.add(name)
    return tuple(name for name in (*defaults, *optional) if name in selected)


def physical_resource_paths(manifest: dict[str, Any], *, methods_only: bool = False) -> tuple[str, ...]:
    rows = [
        entry.internal_method_path
        for entry in stage_methods(manifest)
        if entry.internal_method_path
    ]
    if methods_only:
        rows = [path for path in rows if path.startswith(METHOD_ROOT + "/")]
    return tuple(dict.fromkeys(rows))


def catalog_payload(
    version: str,
    manifest: dict[str, Any],
    excluded: Iterable[str] = (),
) -> dict[str, Any]:
    public = public_entries(manifest)
    skills = [
        {
            "name": entry.name,
            "pack": "core" if entry.class_name == "default" else "optional",
            "sources": list(entry.source_provenance),
            "generated": True,
        }
        for entry in public
    ]
    methods = [
        {
            "name": entry.name,
            "owner": entry.stage_membership[0],
            "path": entry.internal_method_path,
            "resource_digest": entry.resource_digest,
            "shared_artifact": entry.shared_artifact,
        }
        for entry in stage_methods(manifest)
    ]
    retired = []
    for entry in _all_entries(manifest):
        if entry.class_name == "internal":
            retired.append({
                "name": entry.name,
                "kind": "internalized-method",
                "replacement": full_name(entry.stage_membership[0]),
                "path": entry.internal_method_path,
            })
        elif entry.class_name == "retired":
            retired.append({
                "name": entry.name,
                "kind": "deleted",
                "replacement": entry.replacement,
            })
    return {
        "schema": 2,
        "version": version,
        "manifest": MANIFEST_REL,
        "skills": skills,
        "methods": methods,
        "retired": retired,
        "excluded": list(excluded),
    }


def validate_catalog(catalog: dict[str, Any], manifest: dict[str, Any] | None = None) -> list[str]:
    errors = []
    if catalog.get("schema") != 2:
        errors.append(f"catalog schema: {catalog.get('schema')!r}")
    if catalog.get("manifest") != MANIFEST_REL:
        errors.append(f"catalog manifest must be {MANIFEST_REL}")
    if manifest is None:
        return errors
    expected = catalog_payload(catalog.get("version", ""), manifest, catalog.get("excluded", ()))
    for field in ("skills", "methods", "retired", "excluded"):
        if catalog.get(field) != expected[field]:
            errors.append(f"catalog {field} differs from manifest projection")
    return errors


def package_files(root: str | Path) -> tuple[Path, ...]:
    base = Path(root).resolve()
    manifest = load_manifest(base)
    errors = validate_manifest(manifest)
    if errors:
        raise SkillCatalogError("; ".join(errors))
    files: set[Path] = set()
    files.add(manifest_path(base))
    for entry in public_entries(manifest):
        payload = base / entry.payload_root
        if not payload.is_dir():
            raise SkillCatalogError(f"{entry.name}: missing payload root {entry.payload_root}")
        for path in payload.rglob("*"):
            if (
                path.is_file()
                and not path.is_symlink()
                and "__pycache__" not in path.parts
                and not path.name.endswith(".pyc")
                and path.resolve().is_relative_to(base)
            ):
                if path.relative_to(base).as_posix() in RETIRED_PAYLOAD_RESOURCES:
                    continue
                files.add(path)
    return tuple(sorted(files))


def orphan_skill_paths(root: str | Path) -> tuple[str, ...]:
    base = Path(root).resolve()
    manifest = load_manifest(base)
    public_roots = {
        (base / entry.payload_root).resolve()
        for entry in public_entries(manifest)
    }
    top_level = {
        path.resolve()
        for path in (base / "skills").glob("gsp-*")
        if path.is_dir()
    }
    return tuple(sorted(path.relative_to(base).as_posix() for path in top_level - public_roots))
