"""Bounded metadata inventory and static output hints; never cleans project files."""
import ast
import os
from pathlib import Path
import stat

from .core import (inside)
from .verification_inputs import BUILD_DIRS


SKIP = BUILD_DIRS | {'.git', '.gsp', '.gstack', '.agents', '.claude', '.superpowers',
                     '.context', '.venv', 'venv', 'node_modules', 'vendor', '__pycache__'}
ENGINEERING = {'Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', 'config.toml',
               'package.json', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock',
               'pyproject.toml', 'requirements.txt', 'uv.lock', 'poetry.lock',
               'tsconfig.json', '.markdownlint.json', '.gitignore', '.gitattributes',
               '.editorconfig', 'VERSION', 'LICENSE', 'LICENSE.md', 'Makefile',
               'CMakeLists.txt', 'go.mod', 'go.sum', 'Dockerfile', 'compose.yaml'}
OUTPUT_NAMES = {'out', 'output', 'out_path', 'out_file', 'output_path', 'output_file'}


def is_link(path):
    """Detect Windows reparse points on every supported Python version."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def scan_assets(root, reports, max_entries=10000, max_scripts=200,
                max_script_bytes=65536, max_total_script_bytes=2_000_000):
    root, reports = Path(root).resolve(), Path(reports).resolve()
    assets, findings = [], []
    coverage = {'roots':['root files', 'docs non-Markdown', 'research non-Markdown', '.local'],
                'unscanned_root_directories':[],
                'generator_roots':['scripts', 'tools', 'prototype/tools'],
                'max_entries':max_entries, 'entries':0, 'truncated':False,
                'max_scripts':max_scripts, 'scripts_read':0,
                'max_script_bytes':max_script_bytes, 'max_total_script_bytes':max_total_script_bytes,
                'script_bytes_read':0, 'generators_truncated':False,
                'raw_content':'NOT_READ', 'generator_execution':'NOT_CHECKED',
                'git_ignore_check':'NOT_CHECKED',
                'generator_method':'PARTIAL: Python literal output assignments only; dynamic paths and other languages require review',
                'exclusions':sorted(SKIP)}

    def finding(rule, path, detail, severity='WARN'):
        findings.append({'rule':rule, 'path':path, 'detail':detail, 'severity':severity,
                         'confidence':'candidate', 'related':[]})

    def walk(directory, recursive):
        pending = [directory]
        while pending and not coverage['truncated']:
            folder = pending.pop()
            if not folder.exists():
                continue
            try:
                if is_link(folder) or not inside(root, folder):
                    finding('asset-link-skipped', folder.relative_to(root).as_posix(), 'Linked directory not followed')
                    continue
                with os.scandir(folder) as entries:
                    for entry in entries:
                        if coverage['entries'] >= max_entries:
                            coverage['truncated'] = True
                            finding('asset-scan-truncated', '.', 'Entry budget reached; asset coverage is incomplete', 'ERROR')
                            return
                        coverage['entries'] += 1
                        path = Path(entry.path)
                        rel = path.relative_to(root).as_posix()
                        if inside(reports, path) or inside(root/'docs/gsp/reports/document-governance', path):
                            continue
                        if is_link(path):
                            finding('asset-link-skipped', rel, 'Link not followed')
                        elif entry.is_dir(follow_symlinks=False):
                            if recursive and entry.name not in SKIP:
                                pending.append(path)
                            elif not recursive and entry.name not in SKIP | {'docs', 'research', '.local'}:
                                coverage['unscanned_root_directories'].append(rel)
                        elif entry.is_file(follow_symlinks=False):
                            yield path
            except OSError as exc:
                finding('asset-unreadable', folder.relative_to(root).as_posix(), type(exc).__name__, 'ERROR')

    for directory, recursive in ((root, False), (root/'docs', True), (root/'research', True), (root/'.local', True)):
        for path in walk(directory, recursive):
            if path.suffix.lower() == '.md':
                continue
            rel = path.relative_to(root).as_posix()
            try:
                size = path.stat(follow_symlinks=False).st_size
            except OSError as exc:
                finding('asset-unreadable', rel, type(exc).__name__, 'ERROR')
                continue
            role = 'project-material'
            if path.parent == root:
                role = 'engineering' if path.name in ENGINEERING else 'root-material'
                if role == 'root-material':
                    finding('root-artifact-candidate', rel, 'Review purpose, canonical location, producer and retention')
            elif rel.startswith('.local/'):
                role = 'local-artifact'
            elif 'evidence' in path.relative_to(root).parts or 'artifacts' in path.relative_to(root).parts:
                role = 'evidence'
            elif path.name == 'record.json' and rel.startswith('docs/work/'):
                role = 'workflow-metadata'
            if role == 'project-material':
                finding('asset-ownership-review', rel, 'Non-Markdown material needs purpose, source and retention review')
            assets.append({'path':rel, 'bytes':size, 'role':role, 'role_basis':'path/name hint; human review required',
                           'git_state':'unknown', 'content':'NOT_READ'})

    for directory in coverage['generator_roots']:
        for path in walk(root/directory, True):
            if path.suffix.lower() != '.py':
                continue
            rel = path.relative_to(root).as_posix()
            remaining = max_total_script_bytes - coverage['script_bytes_read']
            if coverage['scripts_read'] >= max_scripts or remaining <= 0:
                coverage['generators_truncated'] = True
                finding('generator-scan-truncated', directory, 'Script count/byte budget reached; generator coverage is incomplete', 'ERROR')
                break
            try:
                limit = min(max_script_bytes, remaining)
                if path.stat().st_size > limit:
                    finding('generator-unchecked', rel, 'Script exceeds remaining read budget')
                    continue
                with path.open('rb') as stream:
                    data = stream.read(limit)
                coverage['scripts_read'] += 1
                coverage['script_bytes_read'] += len(data)
                if path.stat().st_size > len(data):
                    finding('generator-unchecked', rel, 'Script grew beyond read budget')
                    continue
                tree = ast.parse(data, filename=rel)
            except (OSError, SyntaxError, UnicodeError, ValueError, RecursionError) as exc:
                finding('generator-unchecked', rel, type(exc).__name__)
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if not any(isinstance(t, ast.Name) and t.id.lower() in OUTPUT_NAMES for t in targets):
                    continue
                if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
                    continue
                try:
                    output = root/node.value.value
                    root_output = inside(root, output) and output.resolve().parent == root and output.suffix
                except (OSError, ValueError) as exc:
                    finding('generator-unchecked', rel, type(exc).__name__)
                    continue
                if root_output:
                    findings.append({'rule':'root-output-literal', 'path':rel,
                                     'detail':'Literal output points at project root; review generator and working-directory assumptions',
                                     'severity':'WARN', 'confidence':'candidate',
                                     'related':[output.resolve().relative_to(root).as_posix()]})
    return sorted(assets, key=lambda a:a['path']), findings, coverage
