"""Project-local, ownership-aware installation; no global profile editing."""
from pathlib import Path
import json
import shutil

from .runtime import WorkflowError, atomic, digest, inside, locked, now, read, sha


def install(project, host, optional, source_root=None):
    """Install from this checkout or an explicitly staged source package."""
    source = Path(source_root or Path(__file__).resolve().parents[1]).resolve()
    if not (source / 'catalog.json').is_file() or not (source / 'skills').is_dir():
        raise WorkflowError('SOURCE_PACKAGE_REQUIRED install from the GstackSP source checkout')
    catalog = read(source / 'catalog.json')
    aliases = {s['name'].removeprefix('gsp-'): s['name'] for s in catalog['skills']}
    requested = {aliases.get(n, n) for n in optional}
    allowed = {s['name'] for s in catalog['skills'] if s['pack'] == 'optional'}
    if requested - allowed:
        raise WorkflowError('UNKNOWN_OPTIONAL ' + ', '.join(sorted(requested - allowed)))
    selected = [s for s in catalog['skills'] if s['pack'] == 'core' or s['name'] in requested]
    changes = []
    project = Path(project).resolve()
    if not project.is_dir():
        raise WorkflowError('PROJECT_MISSING')
    allowed_hosts = {'claude', 'codex', 'zcode', 'both'}
    if host not in allowed_hosts:
        raise WorkflowError('INSTALL_HOST_REQUIRED choose claude, codex, zcode, or both')
    with locked(project):
        plans = []
        for target in (['claude', 'codex'] if host == 'both' else [host]):
            base = project / ('.claude/skills' if target == 'claude' else
                              '.zcode/skills' if target == 'zcode' else '.agents/skills')
            if not inside(project, base):
                raise WorkflowError('INSTALL_ESCAPE skill root resolves outside project')
            manifest = project / '.gsp' / f'install-{target}.json'
            previous = read(manifest) if manifest.exists() else {}
            old = previous.get('files', {})
            files = {}
            for skill in selected:
                folder = source / 'skills' / skill['name']
                for item in folder.rglob('*'):
                    if item.is_file() and '__pycache__' not in item.parts:
                        files[base / skill['name'] / item.relative_to(folder)] = item.read_bytes()
            central = base / 'gsp-workflow' / 'scripts'
            files[central / 'gsp.py'] = (source / 'scripts/gsp.py').read_bytes()
            for file in (source / 'gsp').glob('*.py'):
                files[central / 'gsp' / file.name] = file.read_bytes()
            files[central / 'catalog.json'] = (source / 'catalog.json').read_bytes()
            for notice in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
                files[base / 'gsp-workflow' / notice] = (source / notice).read_bytes()
            for dest, data in files.items():
                if not inside(project, dest):
                    raise WorkflowError('INSTALL_ESCAPE ' + str(dest))
                for parent in dest.parents:
                    if parent == project:
                        break
                    if parent.exists() and not parent.is_dir():
                        raise WorkflowError('INSTALL_CONFLICT parent is not a directory: ' + str(parent))
                key = dest.relative_to(project).as_posix()
                if dest.exists() and (not dest.is_file() or digest(dest) != old.get(key)):
                    raise WorkflowError('INSTALL_CONFLICT refusing to overwrite unowned or edited file: ' + key)
            instruction = project / ('CLAUDE.md' if target == 'claude' else 'AGENTS.md')
            if instruction.exists() and not instruction.is_file():
                raise WorkflowError('INSTRUCTION_NOT_FILE ' + str(instruction))
            if not inside(project, instruction):
                raise WorkflowError('INSTALL_ESCAPE instruction file')
            block = (f'<!-- gstacksp:{target}:begin -->\n## GstackSP\n\n'
                     f'本项目使用 GstackSP。首次处理开发任务时读取 `{base.relative_to(project).as_posix()}/gsp-workflow/SKILL.md`。'
                     '只加载当前阶段相关技能；存在活动任务先核对并恢复。用户明确指示优先。\n'
                     f'<!-- gstacksp:{target}:end -->\n')
            existing = instruction.read_text(encoding='utf-8-sig') if instruction.exists() else ''
            begin, end = f'<!-- gstacksp:{target}:begin -->', f'<!-- gstacksp:{target}:end -->'
            if begin in existing:
                if existing.count(begin) != 1 or existing.count(end) != 1:
                    raise WorkflowError('INSTRUCTION_MARKERS_INVALID ' + str(instruction))
                a, b = existing.index(begin), existing.index(end) + len(end)
                if b <= a:
                    raise WorkflowError('INSTRUCTION_MARKERS_INVALID reverse marker order')
                if sha(existing[a:b].encode()) != previous.get('instruction_block_sha256'):
                    raise WorkflowError('INSTALL_CONFLICT instruction block is unowned or user edited: ' + str(instruction))
                instruction_text = existing[:a] + block.rstrip() + existing[b:]
            elif end in existing:
                raise WorkflowError('INSTRUCTION_MARKERS_INVALID unmatched end marker')
            else:
                instruction_text = existing.rstrip() + ('\n\n' if existing.strip() else '') + block
            plans.append((target, manifest, old, files, instruction, instruction_text, sha(block.rstrip().encode())))
        # All hosts preflight before the first mutation. No existing user file is deleted.
        for target, manifest, old, files, instruction, instruction_text, block_hash in plans:
            hashes = dict(old)
            for dest, data in files.items():
                # UTF-8 files only in this bundle; atomic replacement keeps complete files.
                atomic(dest, data.decode('utf-8-sig'))
                hashes[dest.relative_to(project).as_posix()] = digest(dest)
            atomic(instruction, instruction_text)
            atomic(manifest, {'schema': 1, 'at': now(), 'host': target, 'files': hashes,
                              'instruction_block_sha256': block_hash,
                              'skills': sorted(set((read(manifest).get('skills', []) if manifest.exists() else [])) | {s['name'] for s in selected})})
            changes.append({'host': target, 'skills_added_or_updated': len(selected), 'instruction': str(instruction)})
    return {'installed': changes, 'live_discovery': 'NOT_CHECKED',
            'freeze': 'path checks bundled; automatic hook activation is separate'}
