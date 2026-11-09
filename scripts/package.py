"""Create a standalone source/skill bundle, without private research or state."""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def collect_files(root):
    root=Path(root)
    files = []
    for entry in ('skills', 'gsp', 'scripts', 'hooks', '.claude-plugin', '.codex-plugin', 'docs', 'tests', 'sources'):
        for file in (root / entry).rglob('*'):
            rel=file.relative_to(root).as_posix()
            if rel.startswith(('docs/gsp/','docs/work/')):continue
            if file.is_file() and not file.is_symlink() and file.resolve().is_relative_to(root.resolve()) and '__pycache__' not in file.parts and not file.name.endswith('.pyc'):
                files.append(file)
    files += [root / name for name in ('README.md', 'AGENTS.md', 'CLAUDE.md', 'TODOS.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'catalog.json', '.gitignore') if (root/name).is_file()]
    if (root / '.gitattributes').is_file():
        files.append(root / '.gitattributes')
    return files


def package():
    files=collect_files(ROOT)
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
