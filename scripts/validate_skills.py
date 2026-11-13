"""Structural package checks; intentionally not a claim of host behavior."""
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gsp import __version__


def validate():
    catalog = json.loads((ROOT / 'catalog.json').read_text(encoding='utf-8'))
    assert catalog['version'] == __version__
    for manifest in ('.claude-plugin/plugin.json', '.codex-plugin/plugin.json'):
        assert json.loads((ROOT / manifest).read_text(encoding='utf-8'))['version'] == __version__
    from sp_preservation import verify
    verify(ROOT)
    names = set()
    for item in catalog['skills']:
        name = item['name']
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
    assert len(names) == 50
    assert 'gsp-document-governance' in names
    assert sum(s['pack'] == 'core' for s in catalog['skills']) == 22
    assert len(catalog['excluded']) == 15
    for name in catalog['excluded']:
        assert 'gsp-' + name not in names, name
    for manifest in ('.claude-plugin/plugin.json', '.codex-plugin/plugin.json', 'hooks/hooks.json'):
        json.loads((ROOT / manifest).read_text(encoding='utf-8'))
    assert json.loads((ROOT / '.codex-plugin/plugin.json').read_text())['hooks'] == {}
    # All human guide links must refer to actual local files, excluding example output paths.
    for file in (ROOT / 'README.md', ROOT / 'docs/README.md', ROOT / 'docs/安装与使用.md'):
        for link in re.findall(r'\]\(([^)]+)\)', file.read_text(encoding='utf-8')):
            if not link.startswith(('https://', 'http://', '#')):
                assert (file.parent / link).is_file(), (file, link)
    print(json.dumps({'skills': 50, 'core': 22, 'optional': 28, 'structural_checks': 'PASS', 'live_host': 'NOT_CHECKED'}))


if __name__ == '__main__':
    try:
        validate()
    except (AssertionError, OSError, ValueError) as exc:
        print('VALIDATION_FAILED:', exc, file=sys.stderr)
        raise SystemExit(1)
