"""Project-local, ownership-aware installation; no global profile editing."""
from pathlib import Path
import json
import re
import uuid

from . import skill_catalog
from .core import (WorkflowError, atomic, digest, inside, now, read, sha)
from .workflow_locks import (locked)


def _publish(path, data):
    path = Path(path)
    if data is None:
        path.unlink(missing_ok=True)
    else:
        atomic(path, data.decode('utf-8-sig'))


def _safe_project_path(project, base, rel):
    if not isinstance(rel, str) or Path(rel).is_absolute() or '..' in Path(rel).parts:
        raise WorkflowError('INSTALL_MANIFEST_INVALID ' + str(rel))
    path = project / rel
    if not inside(project, path) or not inside(base, path):
        raise WorkflowError('INSTALL_ESCAPE ' + rel)
    return path


def _restore(project, host, journal, dry_run=False):
    if (
        journal.get('schema') != 1
        or journal.get('project') != str(project)
        or journal.get('host') != host
        or not isinstance(journal.get('changes'), list)
    ):
        raise WorkflowError('INSTALL_RECOVERY_INVALID')
    backup_rel = journal.get('backup', '')
    if not re.fullmatch(r'\.gsp/install-backups/[a-f0-9]{32}', str(backup_rel)):
        raise WorkflowError('INSTALL_RECOVERY_INVALID')
    backup = project / backup_rel
    if not inside(project, backup) or not backup.is_dir():
        raise WorkflowError('INSTALL_RECOVERY_INVALID backup missing')
    restore = []
    seen = set()
    for row in journal['changes']:
        if (
            not isinstance(row, dict)
            or not all(key in row for key in ('path', 'before', 'after', 'backup'))
            or not isinstance(row['path'], str)
            or row['path'] in seen
        ):
            raise WorkflowError('INSTALL_RECOVERY_INVALID')
        seen.add(row['path'])
        before, after = row['before'], row['after']
        for value in (before, after):
            if value is not None and (
                not isinstance(value, str)
                or not re.fullmatch(r'[a-f0-9]{64}', value)
            ):
                raise WorkflowError('INSTALL_RECOVERY_INVALID')
        path = project / row['path']
        if not inside(project, path):
            raise WorkflowError('INSTALL_RECOVERY_INVALID')
        current = digest(path) if path.is_file() else None
        if current not in (before, after):
            raise WorkflowError('INSTALL_RECOVERY_CONFLICT user changed ' + row['path'])
        if not str(row['backup']).isdigit():
            raise WorkflowError('INSTALL_RECOVERY_INVALID')
        copy = backup / row['backup']
        data = copy.read_bytes() if before is not None else None
        if data is not None and sha(data) != before:
            raise WorkflowError('INSTALL_BACKUP_CORRUPT')
        restore.append((path, data))
    if dry_run:
        return {'recovered': False, 'dry_run': True, 'project': str(project), 'host': host}
    for path, data in reversed(restore):
        _publish(path, data)
    (project / '.gsp' / f'install-{host}.transaction.json').unlink(missing_ok=True)
    return {'recovered': True, 'dry_run': False, 'project': str(project), 'host': host}


def recover(project, host, dry_run=False):
    project = Path(project).resolve()
    if host not in ('claude', 'codex', 'zcode'):
        raise WorkflowError('INSTALL_HOST_REQUIRED choose claude, codex, or zcode')
    journal_path = project / '.gsp' / f'install-{host}.transaction.json'
    with locked(project):
        if not journal_path.is_file():
            raise WorkflowError('INSTALL_NO_TRANSACTION')
        return _restore(project, host, read(journal_path), dry_run=dry_run)


def _commit(project, host, changes, backup_rel):
    journal_path = project / '.gsp' / f'install-{host}.transaction.json'
    backup = project / backup_rel
    if not inside(project, backup):
        raise WorkflowError('INSTALL_ESCAPE backup')
    records = []
    for rel, after in sorted(changes.items()):
        path = project / rel
        before = path.read_bytes() if path.is_file() else None
        if before == after:
            continue
        index = str(len(records))
        if before is not None:
            backup.mkdir(parents=True, exist_ok=True)
            (backup / index).write_bytes(before)
        records.append({
            'path': rel,
            'before': sha(before) if before is not None else None,
            'after': sha(after) if after is not None else None,
            'backup': index,
        })
    operation = {
        'schema': 1,
        'project': str(project),
        'host': host,
        'backup': backup_rel,
        'changes': records,
        'at': now(),
    }
    backup.mkdir(parents=True, exist_ok=True)
    atomic(backup / 'operation.json', operation)
    atomic(journal_path, operation)
    try:
        for row in records:
            _publish(project / row['path'], changes[row['path']])
        journal_path.unlink()
    except BaseException:
        _restore(project, host, operation)
        raise
    return backup_rel


def _cleanup_empty_dirs(project, base, deleted_paths):
    for rel in deleted_paths:
        path = project / rel
        parent = path.parent
        while parent != base and inside(base, parent):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def _load_source_manifest(source):
    try:
        manifest = skill_catalog.load_manifest(source)
        errors = skill_catalog.validate_manifest(
            manifest, source, check_digests=True
        )
    except (OSError, skill_catalog.SkillCatalogError) as exc:
        raise WorkflowError('INSTALL_CATALOG_INVALID ' + str(exc)) from exc
    if errors:
        raise WorkflowError('INSTALL_CATALOG_INVALID ' + '; '.join(errors))
    return manifest


def install(project, host, optional, source_root=None):
    """Install one exact catalog projection, migrating only owned legacy files."""
    source = Path(source_root or Path(__file__).resolve().parents[1]).resolve()
    if not (source / skill_catalog.MANIFEST_REL).is_file() or not (source / 'skills').is_dir():
        raise WorkflowError('SOURCE_PACKAGE_REQUIRED install from the GstackSP source checkout')
    source_manifest = _load_source_manifest(source)
    public = list(skill_catalog.public_entries(source_manifest))
    names = [row.name for row in public]
    if len(names) != len(set(names)):
        raise WorkflowError('INSTALL_CATALOG_INVALID duplicate or invalid skill name')
    aliases = {name.removeprefix('gsp-'): name for name in names}
    allowed_optional = {
        row.name for row in public if row.class_name == 'on_demand'
    }
    explicit = set()
    for value in optional or []:
        name = aliases.get(value, value)
        if name not in allowed_optional:
            raise WorkflowError('UNKNOWN_OPTIONAL ' + str(value))
        explicit.add(name)

    project = Path(project).resolve()
    if not project.is_dir():
        raise WorkflowError('PROJECT_MISSING')
    allowed_hosts = {'claude', 'codex', 'zcode', 'both'}
    if host not in allowed_hosts:
        raise WorkflowError('INSTALL_HOST_REQUIRED choose claude, codex, zcode, or both')

    with locked(project):
        plans = []
        changes_report = []
        for target in (['claude', 'codex'] if host == 'both' else [host]):
            base = project / (
                '.claude/skills' if target == 'claude' else
                '.zcode/skills' if target == 'zcode' else
                '.agents/skills'
            )
            if not inside(project, base):
                raise WorkflowError('INSTALL_ESCAPE skill root resolves outside project')
            manifest = project / '.gsp' / f'install-{target}.json'
            journal = project / '.gsp' / f'install-{target}.transaction.json'
            if journal.exists():
                raise WorkflowError('INSTALL_RECOVERY_REQUIRED run install recovery for ' + target)
            previous = read(manifest) if manifest.exists() else {}
            if previous and previous.get('host') not in (None, target):
                raise WorkflowError('INSTALL_MANIFEST_INVALID host mismatch')
            old = previous.get('files', {})
            if previous and (not isinstance(old, dict) or not old):
                raise WorkflowError('INSTALL_MANIFEST_INVALID missing file inventory')
            for rel, expected in old.items():
                if (
                    not isinstance(rel, str)
                    or not isinstance(expected, str)
                    or not re.fullmatch(r'[a-f0-9]{64}', expected)
                ):
                    raise WorkflowError('INSTALL_MANIFEST_INVALID file inventory')

            prior_optional = previous.get('requested_optional', [])
            if not isinstance(prior_optional, list):
                raise WorkflowError('INSTALL_MANIFEST_INVALID requested_optional')
            requested_optional = set()
            for value in prior_optional:
                if not isinstance(value, str):
                    raise WorkflowError('INSTALL_MANIFEST_INVALID requested_optional')
                name = aliases.get(value, value)
                if name in allowed_optional:
                    requested_optional.add(name)
            requested_optional |= explicit
            selection = set(skill_catalog.install_selection(
                source_manifest, requested_optional
            ))
            selected = [row for row in public if row.name in selection]
            selected_names = {row.name for row in selected}

            desired = {}
            for skill in selected:
                folder = source / skill.payload_root
                for item in folder.rglob('*'):
                    if item.is_file() and '__pycache__' not in item.parts:
                        desired[base / skill.name / item.relative_to(folder)] = item.read_bytes()
            central = base / 'gsp-workflow' / 'scripts'
            desired[central / 'gsp.py'] = (source / 'scripts/gsp.py').read_bytes()
            for file in (source / 'gsp').glob('*.py'):
                desired[central / 'gsp' / file.name] = file.read_bytes()
            desired[central / 'skill-manifest.json'] = (
                source / skill_catalog.MANIFEST_REL
            ).read_bytes()
            desired[central / 'catalog.json'] = (
                json.dumps(
                    skill_catalog.catalog_payload(
                        source_manifest['version'],
                        source_manifest,
                        source_manifest.get('excluded', ()),
                    ),
                    ensure_ascii=False,
                    indent=2,
                ) + '\n'
            ).encode('utf-8')
            for notice in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
                desired[base / 'gsp-workflow' / notice] = (source / notice).read_bytes()

            desired_keys = {path.relative_to(project).as_posix() for path in desired}
            for path, data in desired.items():
                if not inside(project, path):
                    raise WorkflowError('INSTALL_ESCAPE ' + str(path))
                for parent in path.parents:
                    if parent == project:
                        break
                    if parent.exists() and not parent.is_dir():
                        raise WorkflowError('INSTALL_CONFLICT parent is not a directory: ' + str(parent))
                key = path.relative_to(project).as_posix()
                if path.exists() and (
                    not path.is_file()
                    or path.is_symlink()
                    or old.get(key) != digest(path)
                ):
                    raise WorkflowError('INSTALL_CONFLICT refusing to overwrite unowned or edited file: ' + key)

            if base.exists():
                for folder in base.iterdir():
                    if not folder.is_dir() or folder.is_symlink() or not folder.name.startswith('gsp-'):
                        continue
                    if folder.name in selected_names:
                        continue
                    for item in folder.rglob('*'):
                        if item.is_symlink():
                            raise WorkflowError('INSTALL_CONFLICT unowned symlink: ' + str(item))
                        if not item.is_file():
                            continue
                        key = item.relative_to(project).as_posix()
                        if old.get(key) != digest(item):
                            raise WorkflowError(
                                'INSTALL_CONFLICT unowned or edited retired file: ' + key
                            )

            deletions = {}
            for rel, expected in old.items():
                if rel in desired_keys:
                    continue
                path = _safe_project_path(project, base, rel)
                if path.exists():
                    if not path.is_file() or path.is_symlink() or digest(path) != expected:
                        raise WorkflowError('INSTALL_CONFLICT edited retired file: ' + rel)
                    deletions[rel] = None

            instruction = project / ('CLAUDE.md' if target == 'claude' else 'AGENTS.md')
            if instruction.exists() and not instruction.is_file():
                raise WorkflowError('INSTRUCTION_NOT_FILE ' + str(instruction))
            if not inside(project, instruction):
                raise WorkflowError('INSTALL_ESCAPE instruction file')
            block = (
                f'<!-- gstacksp:{target}:begin -->\n## GstackSP\n\n'
                f'本项目使用 GstackSP。首次处理开发任务时读取 '
                f'`{base.relative_to(project).as_posix()}/gsp-workflow/SKILL.md`。'
                '只加载当前阶段相关技能；存在活动任务先核对并恢复。用户明确指示优先。\n'
                f'<!-- gstacksp:{target}:end -->\n'
            )
            existing = instruction.read_text(encoding='utf-8-sig') if instruction.exists() else ''
            begin = f'<!-- gstacksp:{target}:begin -->'
            end = f'<!-- gstacksp:{target}:end -->'
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
            block_hash = sha(block.rstrip().encode())

            backup_rel = '.gsp/install-backups/' + uuid.uuid4().hex
            hashes = {
                path.relative_to(project).as_posix(): sha(data)
                for path, data in desired.items()
            }
            manifest_record = {
                'schema': 2,
                'at': now(),
                'host': target,
                'files': hashes,
                'skills': sorted(selected_names),
                'requested_optional': sorted(requested_optional),
                'skill_manifest_sha256': sha(
                    (source / skill_catalog.MANIFEST_REL).read_bytes()
                ),
                'instruction_block_sha256': block_hash,
                'last_backup': backup_rel,
                'live_discovery': 'NOT_CHECKED',
            }
            changes = {
                path.relative_to(project).as_posix(): data
                for path, data in desired.items()
            }
            changes.update(deletions)
            changes[instruction.relative_to(project).as_posix()] = instruction_text.encode('utf-8')
            changes[manifest.relative_to(project).as_posix()] = (
                json.dumps(manifest_record, ensure_ascii=False, indent=2) + '\n'
            ).encode('utf-8')
            plans.append((target, base, changes, deletions, backup_rel, instruction))
            changes_report.append({
                'host': target,
                'skills_added_or_updated': len(selected),
                'removed_files': len(deletions),
                'backup': backup_rel,
                'instruction': str(instruction),
            })

        for target, base, changes, deletions, backup_rel, _ in plans:
            _commit(project, target, changes, backup_rel)
            _cleanup_empty_dirs(project, base, deletions)
    return {
        'installed': changes_report,
        'live_discovery': 'NOT_CHECKED',
        'freeze': 'path checks bundled; automatic hook activation is separate',
    }
