"""Structural package checks; intentionally not a claim of host behavior."""
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gsp import __version__
from gsp import skill_catalog


def validate():
    manifest = skill_catalog.load_manifest(ROOT)
    catalog = json.loads((ROOT / 'catalog.json').read_text(encoding='utf-8'))
    assert catalog['version'] == __version__
    for plugin_manifest in ('.claude-plugin/plugin.json', '.codex-plugin/plugin.json'):
        assert json.loads((ROOT / plugin_manifest).read_text(encoding='utf-8'))['version'] == __version__
    from scripts.sp_preservation import verify
    verify(ROOT)
    manifest_errors=skill_catalog.validate_manifest(manifest, ROOT, check_digests=True)
    assert not manifest_errors, manifest_errors
    catalog_errors=skill_catalog.validate_catalog(catalog, manifest)
    assert not catalog_errors, catalog_errors
    names = set()
    for item in skill_catalog.public_entries(manifest):
        name = item.name
        assert re.fullmatch(r'gsp-[a-z0-9-]+', name), name
        assert name not in names, name
        names.add(name)
        file = ROOT / 'skills' / name / 'SKILL.md'
        text = file.read_text(encoding='utf-8')
        assert text.startswith(f'---\nname: {name}\n'), file
        front = text.split('---', 2)[1]
        assert re.search(r'^description: .+', front, re.M), file
        if '<!-- sp-preserved-body:begin -->' not in text:
            assert not re.search(r'\b(?:TODO|TBD)\b', text), file
        for link in re.findall(r'\]\(([^)]+)\)', text):
            if not link.startswith(('https://', 'http://', '#')):
                assert (file.parent / link).is_file(), (file, link)
        metadata = file.parent / 'agents/openai.yaml'
        meta = metadata.read_text(encoding='utf-8')
        assert '$' + name in meta, metadata
        assert 'allow_implicit_invocation: false' not in meta, metadata
    assert names == {entry.name for entry in skill_catalog.public_entries(manifest)}
    assert len(names) == 10
    assert 'gsp-document-governance' in names
    assert sum(s['pack'] == 'core' for s in catalog['skills']) == 6
    assert sum(s['pack'] == 'optional' for s in catalog['skills']) == 4
    assert len(catalog['excluded']) == 15
    for name in catalog['excluded']:
        assert 'gsp-' + name not in names, name
    discoverable={path.parent.name for path in (ROOT/'skills').glob('gsp-*/SKILL.md')}
    assert discoverable == names, ('ORPHAN_SKILL_ENTRY', sorted(discoverable-names))
    orphan_dirs = skill_catalog.orphan_skill_paths(ROOT)
    assert not orphan_dirs, ('ORPHAN_SKILL_DIRECTORY', orphan_dirs)
    methods = skill_catalog.stage_methods(manifest)
    assert len(methods) == 28
    assert len(skill_catalog.physical_resource_paths(manifest, methods_only=True)) == 26
    assert len(skill_catalog.physical_resource_paths(manifest)) == 27
    for method in methods:
        path=ROOT/method.internal_method_path
        assert path.is_file(), method.name
        text=path.read_text(encoding='utf-8')
        if not method.shared_artifact:
            assert f'<!-- internal-method: {method.name} -->' in text
            assert f'Owner: `{skill_catalog.full_name(method.stage_membership[0])}`' in text
        for link in re.findall(r'\]\(([^)]+)\)', text):
            if not link.startswith(('https://','http://','#')):
                assert (path.parent/link).is_file(), (path,link)
    contract = (ROOT / skill_catalog.CONTEXT_RESOURCE_PATH).read_text(encoding='utf-8')
    assert '## 业务恢复与原生上下文' in contract
    for plugin_manifest in ('.claude-plugin/plugin.json', '.codex-plugin/plugin.json', 'hooks/hooks.json'):
        json.loads((ROOT / plugin_manifest).read_text(encoding='utf-8'))
    assert json.loads((ROOT / '.codex-plugin/plugin.json').read_text())['hooks'] == {}
    # All human guide links must refer to actual local files, excluding example output paths.
    for file in (ROOT / 'README.md', ROOT / 'docs/README.md', ROOT / 'docs/安装与使用.md'):
        for link in re.findall(r'\]\(([^)]+)\)', file.read_text(encoding='utf-8')):
            if not link.startswith(('https://', 'http://', '#')):
                assert (file.parent / link).is_file(), (file, link)
    print(json.dumps({
        'skills': 10,
        'core': 6,
        'optional': 4,
        'internal_sources': 28,
        'physical_methods': 26,
        'shared_context_resources': 1,
        'retired': 40,
        'structural_checks': 'PASS',
        'live_host': 'NOT_CHECKED',
    }))


if __name__ == '__main__':
    try:
        validate()
    except (AssertionError, OSError, ValueError) as exc:
        print('VALIDATION_FAILED:', exc, file=sys.stderr)
        raise SystemExit(1)
