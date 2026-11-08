"""Create a standalone source/skill bundle, without private research or state."""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def package():
    files = []
    for entry in ('skills', 'gsp', 'scripts', 'hooks', '.claude-plugin', '.codex-plugin', 'docs', 'tests'):
        for file in (ROOT / entry).rglob('*'):
            if file.is_file() and '__pycache__' not in file.parts and not file.name.endswith('.pyc'):
                files.append(file)
    files += [ROOT / name for name in ('README.md', 'AGENTS.md', 'CLAUDE.md', 'TODOS.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'catalog.json', '.gitignore')]
    dist = ROOT / 'dist'
    dist.mkdir(exist_ok=True)
    version = json.loads((ROOT / 'catalog.json').read_text(encoding='utf-8'))['version']
    output = dist / f'GstackSP-{version}.zip'
    manifest = {}
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(set(files)):
            rel = file.relative_to(ROOT).as_posix()
            data = file.read_bytes()
            archive.writestr('GstackSP/' + rel, data)
            manifest[rel] = hashlib.sha256(data).hexdigest()
        archive.writestr('GstackSP/bundle-manifest.json', json.dumps(manifest, indent=2))
    print(json.dumps({'archive': str(output), 'files': len(files), 'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}))


if __name__ == '__main__':
    package()
