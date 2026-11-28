"""Verified legacy identity mapping and recoverable allocator cutover.

Planning never initializes an allocator or grants a legacy session write access.
The old records, run paths, evidence and Git resources remain source facts.
"""
import json
import hashlib
import os
from contextlib import closing, contextmanager
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import uuid

from .core import WorkflowError, atomic, read
from .identity_store import (
    IDENTITY_SPECS,
    MIGRATION_PENDING_RELATIVE,
    IdentityStore,
    IdentityStoreError,
    _normalize_explicit,
)
from .orchestration_migration import verify_source, verify_source_for_apply


def _uid(project, kind, scope, alias):
    return uuid.uuid5(uuid.NAMESPACE_URL, ':'.join((project, kind, scope, alias))).hex


def _task_id_normalizable(raw):
    """A task id is adoptable when the allocator can rewrite it canonically."""
    try:
        _normalize_explicit('task', raw)
    except IdentityStoreError:
        return False
    return True


def _build_identity_plan(root, manifest, *, verify=True):
    """Derive a deterministic mapping from the verified T03 manifest."""
    root = Path(root).resolve()
    if verify:
        verify_source(root, manifest)
    if manifest.blockers:
        raise WorkflowError('MIGRATION_BLOCKED ' + ','.join(sorted({row['code'] for row in manifest.blockers})))
    if not manifest.project_uid:
        raise WorkflowError('MIGRATION_PROJECT_UID_REQUIRED')
    project = manifest.project_uid
    objects, aliases, sequences = {}, {}, {}
    read_only, runs, requires_reverification = [], {}, []

    def floor(kind, scope='', value=1, width=None):
        key = kind, scope
        prefix, minimum_width = IDENTITY_SPECS[kind]
        row = sequences.setdefault(key, {'kind': kind, 'scope_uid': scope,
                                         'next_value': 1, 'minimum_width': minimum_width})
        row['next_value'] = max(row['next_value'], int(value))
        row['minimum_width'] = max(row['minimum_width'], int(width or minimum_width))

    def alias(raw, kind, scope, uid):
        key = raw, kind, scope
        previous = aliases.get(key)
        if previous and previous['object_uid'] != uid:
            raise WorkflowError('MIGRATION_ALIAS_CONFLICT ' + raw)
        aliases[key] = {'alias': raw, 'object_kind': kind, 'scope_uid': scope, 'object_uid': uid}

    def add(kind, raw, source_path, *, uid=None, scope=''):
        value = raw
        if kind == 'todo':
            prefix, width = IDENTITY_SPECS[kind]
            digits = raw.rsplit('-', 1)[-1]
            if raw.startswith(('GSP-', 'TODO-')) and digits.isdigit():
                value = prefix + str(int(digits)).zfill(width)
        try:
            canonical, explicit_alias, number = _normalize_explicit(kind, value)
        except IdentityStoreError as exc:
            raise WorkflowError('MIGRATION_IDENTITY_INVALID ' + str(exc)) from exc
        uid = uid or _uid(project, kind, scope, raw)
        key = kind, scope, canonical
        previous = objects.get(key)
        if previous and previous['uid'] != uid:
            raise WorkflowError('MIGRATION_IDENTITY_CONFLICT ' + raw)
        objects[key] = {'uid': uid, 'kind': kind, 'project_uid': project,
                        'scope_uid': scope, 'canonical_id': canonical,
                        'source_path': source_path}
        alias(raw, kind, scope, uid)
        if explicit_alias:
            alias(explicit_alias, kind, scope, uid)
        digits = canonical.rsplit('-', 1)[-1]
        floor(kind, scope, number + 1, len(digits))
        return objects[key]

    for row in manifest.backup_policy.get('authority', {}).get('sequences', []):
        floor(row['kind'], row.get('scope_uid') or '', row['next_value'], row['minimum_width'])
    works = {}
    work_runs = {}
    for row in manifest.classification_manifest:
        if row.get('domain') != 'work_record':
            continue
        work = add('work', row['number'], row['source_path'], uid=row['uuid'])
        works[work['uid']] = work
        if row.get('run_id'):
            work_runs[row['run_id']] = work
    for row in manifest.classification_manifest:
        if row.get('domain') == 'todo_marker':
            add('todo', row['marker_id'], row['source_path'])
        elif row.get('domain') == 'run_state':
            work = works.get(row.get('work_uuid')) or work_runs.get(row['identity'])
            if work is None:
                read_only.append({'domain': 'run_state', 'source_path': row['source_path'],
                                  'identity': row['identity'], 'reason': 'EXPLICIT_WORK_ADOPTION_REQUIRED'})
                continue
            state = json.loads((root / row['source_path']).read_text(encoding='utf-8-sig'))
            legacy_tasks = [task['id'] for task in state.get('tasks', [])
                            if not _task_id_normalizable(task.get('id'))]
            if legacy_tasks:
                # A legacy run holding task ids the allocator cannot issue keeps
                # its history read-only: renumbering would collide with siblings.
                read_only.append({'domain': 'run_state', 'source_path': row['source_path'],
                                  'identity': row['identity'],
                                  'reason': 'LEGACY_TASK_ID_NOT_CANONICAL',
                                  'task_ids': legacy_tasks})
                continue
            runs[row['identity']] = {
                'work_uid': work['uid'], 'canonical_id': work['canonical_id'],
                'legacy_number': row.get('number'), 'source_path': row['source_path'],
                'original_status': row.get('status'),
            }
            if row.get('number') and row.get('number') != work['canonical_id']:
                alias(row['number'], 'work', 'run:' + row['identity'], work['uid'])
            if row.get('status') == 'ready':
                requires_reverification.append(row['identity'])
            for task in state.get('tasks', []):
                add('task', task['id'], row['source_path'], scope=work['uid'])
    for row in manifest.aliases:
        kind = row.get('object_kind')
        if kind in {'session', 'dispatch'}:
            read_only.append({**row, 'reason': 'LEGACY_SESSION_REQUIRES_NEW_VERIFIED_BINDING'})
            text = str(row.get('identity') or '')
            number = re.search(r'(\d+)$', text)
            if number:
                floor(kind, '', int(number[1]) + 1)
    counter = manifest.backup_policy.get('counter', {})
    # Legacy GSP work/todo numbers shared a visible namespace. Neither namespace
    # may issue a number reserved by its historical peer after cutover.
    global_floor = max(int(counter.get('effective_next') or 1),
                       int(counter.get('todo_highest') or 0) + 1)
    floor('work', value=global_floor)
    floor('todo', value=max(global_floor, int(counter.get('todo_highest') or 0) + 1))
    return {'schema': 1, 'migration_uid': manifest.uid, 'project_uid': project,
            'source_digest': manifest.source_digest,
            'objects': sorted(objects.values(), key=lambda row: (row['kind'], row['scope_uid'], row['canonical_id'])),
            'aliases': sorted(aliases.values(), key=lambda row: (row['object_kind'], row['scope_uid'], row['alias'])),
            'sequences': sorted(sequences.values(), key=lambda row: (row['kind'], row['scope_uid'])),
            'runs': runs, 'requires_reverification': sorted(requires_reverification),
            'read_only': read_only, 'resources': list(manifest.resources),
            'scope_history': 'preserved-inactive', 'accepted': False}


def plan_identity_import(root, manifest):
    """Public planning entry: always verify the source before returning data."""
    return _build_identity_plan(root, manifest, verify=True)


MIGRATION_SQLITE_SOURCES = frozenset({
    '.gsp/documents.sqlite',
    '.gsp/orchestration/registry.sqlite',
})


def _require_control_root(root):
    from .workflow_support import (_brief_control_root)

    control_root = Path(_brief_control_root(root)).resolve()
    if control_root != Path(root).resolve():
        raise WorkflowError(
            'MIGRATION_CONTROL_ROOT_REQUIRED use ' + str(control_root)
        )
    return control_root


def _digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _digest_file(path):
    return _digest_bytes(Path(path).read_bytes())


def _json_bytes(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        + '\n'
    ).encode('utf-8')


def _write_bytes(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        '.' + path.name + '.' + uuid.uuid4().hex + '.tmp'
    )
    try:
        with temporary.open('wb') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(4):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(.05)
    finally:
        temporary.unlink(missing_ok=True)


def _backup_sqlite(source, destination):
    source_path = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        '.' + destination.name + '.' + uuid.uuid4().hex + '.tmp'
    )
    try:
        with tempfile.TemporaryDirectory(
            prefix='gsp-migration-sqlite-'
        ) as snapshot_dir:
            snapshot = Path(snapshot_dir) / 'source.sqlite'
            _write_bytes(snapshot, source_path.read_bytes())
            for suffix in ('-wal', '-shm'):
                sidecar = Path(str(source_path) + suffix)
                if sidecar.is_file():
                    _write_bytes(
                        Path(str(snapshot) + suffix), sidecar.read_bytes(),
                    )
            with closing(sqlite3.connect(snapshot, timeout=30)) as src:
                src.execute('PRAGMA query_only=ON')
                with closing(sqlite3.connect(temporary, timeout=30)) as dst:
                    src.backup(dst)
                    result = dst.execute('PRAGMA integrity_check').fetchone()
                    if result is None or result[0] != 'ok':
                        raise WorkflowError('MIGRATION_SQLITE_BACKUP_INVALID')
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _sqlite_snapshot(path):
    """Yield a private SQLite snapshot without opening the source database."""
    with tempfile.TemporaryDirectory(
        prefix='gsp-migration-read-'
    ) as snapshot_dir:
        snapshot = Path(snapshot_dir) / 'source.sqlite'
        _backup_sqlite(path, snapshot)
        yield snapshot


def _sqlite_mutation_digest_snapshot(snapshot):
    with closing(sqlite3.connect(
        Path(snapshot).as_uri() + '?mode=ro', uri=True,
    )) as db:
        db.execute('PRAGMA query_only=ON')
        pragmas = {
            name: db.execute('PRAGMA ' + name).fetchone()[0]
            for name in ('application_id', 'user_version')
        }
        dump = '\n'.join(db.iterdump())
    return _digest_bytes(_json_bytes({
        'schema': 1,
        'pragmas': pragmas,
        'dump': dump,
    }))


def _sqlite_mutation_digest(path):
    with _sqlite_snapshot(path) as snapshot:
        return _sqlite_mutation_digest_snapshot(snapshot)


def _backup_source_files(root, stage):
    from .orchestration_migration import _source_files

    files = set(_source_files(root))
    for path in (root / '.gsp' / 'runs').glob('*/evidence/**/*'):
        if path.is_file():
            files.add(path)
    rows = []
    for source in sorted(files):
        relative = source.relative_to(root).as_posix()
        if relative in {'.gsp/writer.lock', '.gsp/writer.mutex'}:
            continue
        before = _digest_file(source)
        if relative in MIGRATION_SQLITE_SOURCES:
            backup = stage / 'source-backup' / relative
            _backup_sqlite(source, backup)
            mode = 'sqlite-backup'
        elif relative.endswith(('-wal', '-shm')):
            backup = None
            mode = 'sqlite-sidecar-observed'
        else:
            backup = stage / 'source-backup' / relative
            _write_bytes(backup, source.read_bytes())
            mode = 'byte-copy'
        rows.append({
            'source_path': relative,
            'mode': mode,
            'before_digest': before,
            'backup_path': (
                backup.relative_to(root).as_posix() if backup is not None else None
            ),
            'backup_digest': (
                _digest_file(backup) if backup is not None else None
            ),
        })
    for row in rows:
        row['after_digest'] = _digest_file(root / row['source_path'])
        if row['after_digest'] != row['before_digest']:
            raise WorkflowError(
                'MIGRATION_SOURCE_CHANGED during source backup: '
                + row['source_path']
            )
    return rows


def _sqlite_logical_digest(path):
    with closing(sqlite3.connect(
        Path(path).as_uri() + '?mode=ro', uri=True,
    )) as db:
        db.row_factory = sqlite3.Row
        value = {}
        for table, order in (
            ('metadata', 'key'),
            ('objects', 'uid'),
            ('aliases', 'alias,object_kind,scope_uid'),
            ('sequences', 'kind,scope_uid'),
        ):
            value[table] = [
                dict(row)
                for row in db.execute(
                    f'SELECT * FROM {table} ORDER BY {order}'
                )
            ]
    return _digest_bytes(_json_bytes(value))


def _validate_staged_candidate(candidate, mapping):
    with closing(sqlite3.connect(
        Path(candidate).as_uri() + '?mode=ro', uri=True,
    )) as db:
        db.row_factory = sqlite3.Row
        integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity != 'ok':
            raise WorkflowError('MIGRATION_STAGE_INVALID')
        if db.execute(
            "SELECT 1 FROM metadata WHERE key='schema_version'"
        ).fetchone():
            raise WorkflowError('MIGRATION_SCHEMA_MARKER_EARLY')
        state = db.execute(
            "SELECT value FROM metadata WHERE key='migration_state'"
        ).fetchone()
        if state is None or state['value'] != 'staged':
            raise WorkflowError('MIGRATION_STAGE_INVALID')
        expected = {
            'objects': len(mapping['objects']),
            'aliases': len(mapping['aliases']),
            'sequences': len(mapping['sequences']),
        }
        actual = {
            'objects': db.execute('SELECT COUNT(*) FROM objects').fetchone()[0],
            'aliases': db.execute('SELECT COUNT(*) FROM aliases').fetchone()[0],
            'sequences': db.execute('SELECT COUNT(*) FROM sequences').fetchone()[0],
        }
        if actual != expected:
            raise WorkflowError('MIGRATION_STAGE_INVALID')


def stage_migration(root, manifest, *, request_uid=None):
    """Persist one source-pinned, inactive identity candidate."""
    root = _require_control_root(Path(root).resolve())
    pending_path = root / MIGRATION_PENDING_RELATIVE
    if not pending_path.exists():
        verify_source(root, manifest)
        if manifest.blockers:
            raise WorkflowError(
                'MIGRATION_BLOCKED '
                + ','.join(sorted({row['code'] for row in manifest.blockers}))
            )
    from .workflow_locks import (locked)

    with locked(root):
        stage = root / '.gsp' / 'migrations' / manifest.uid
        ignored_paths = (
            '.gsp/writer.lock',
            '.gsp/writer.mutex',
            MIGRATION_PENDING_RELATIVE,
            stage.relative_to(root).as_posix(),
        )
        verify_source_for_apply(
            root, manifest, ignored_paths=ignored_paths,
        )
        mapping = _build_identity_plan(root, manifest, verify=False)
        mapping_digest = _digest_bytes(_json_bytes(mapping))
        fingerprint = _digest_bytes(_json_bytes({
            'schema': 1,
            'migration_uid': manifest.uid,
            'project_uid': mapping['project_uid'],
            'source_digest': manifest.source_digest,
            'mapping_digest': mapping_digest,
        }))
        existing = read(pending_path) if pending_path.is_file() else None
        if existing is not None:
            if (
                existing.get('schema') != 1
                or existing.get('fingerprint') != fingerprint
                or existing.get('mapping_digest') != mapping_digest
                or existing.get('source_digest') != manifest.source_digest
            ):
                raise WorkflowError('MIGRATION_REQUEST_CONFLICT pending remains')
            if request_uid is not None and existing.get('request_uid') != request_uid:
                raise WorkflowError('MIGRATION_REQUEST_CONFLICT pending remains')
            request_uid = existing.get('request_uid')
            created_at = existing.get('created_at')
        else:
            if stage.exists():
                raise WorkflowError(
                    'MIGRATION_RECOVERY_REQUIRED stage exists without pending intent'
                )
            request_uid = request_uid or ('migration-' + fingerprint[:24])
            created_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        if not isinstance(request_uid, str) or not request_uid:
            raise WorkflowError('MIGRATION_REQUEST_INVALID')
        pending = {
            'schema': 1,
            'state': existing.get('state', 'staging') if existing else 'staging',
            'request_uid': request_uid,
            'fingerprint': fingerprint,
            'migration_uid': manifest.uid,
            'project_uid': mapping['project_uid'],
            'source_digest': manifest.source_digest,
            'mapping_digest': mapping_digest,
            'stage_path': stage.relative_to(root).as_posix(),
            'created_at': created_at,
        }
        atomic(pending_path, pending)
        stage.mkdir(parents=True, exist_ok=True)
        _write_bytes(stage / 'manifest.json', _json_bytes(manifest.to_dict()))
        _write_bytes(stage / 'mapping.json', _json_bytes(mapping))
        cursors = _backup_source_files(root, stage)
        candidate = stage / 'candidate' / 'registry.sqlite'
        store = IdentityStore(root)
        try:
            staged = store.stage_identity_import(
                candidate,
                mapping,
                request_uid=request_uid,
                manifest_uid=manifest.uid,
                mapping_digest=mapping_digest,
                created_at=created_at,
            )
        except IdentityStoreError as exc:
            raise WorkflowError(str(exc)) from exc
        _validate_staged_candidate(candidate, mapping)
        logical_digest = _sqlite_logical_digest(candidate)
        cursor_document = {
            'schema': 1,
            'request_uid': request_uid,
            'migration_uid': manifest.uid,
            'source_digest': manifest.source_digest,
            'mapping_digest': mapping_digest,
            'files': cursors,
            'candidate_path': candidate.relative_to(root).as_posix(),
            'candidate_logical_digest': logical_digest,
        }
        _write_bytes(stage / 'cursor.json', _json_bytes(cursor_document))
        verify_source_for_apply(
            root, manifest, ignored_paths=ignored_paths,
        )
        result = {
            'schema': 1,
            'state': 'staged',
            'request_uid': request_uid,
            'migration_uid': manifest.uid,
            'project_uid': mapping['project_uid'],
            'source_digest': manifest.source_digest,
            'mapping_digest': mapping_digest,
            'stage_path': stage.relative_to(root).as_posix(),
            'candidate_path': candidate.relative_to(root).as_posix(),
            'candidate_sha256': staged['candidate_sha256'],
            'candidate_logical_digest': logical_digest,
            'backup_path': (stage / 'source-backup').relative_to(root).as_posix(),
            'mapping_path': (stage / 'mapping.json').relative_to(root).as_posix(),
            'cursor_path': (stage / 'cursor.json').relative_to(root).as_posix(),
        }
        _write_bytes(stage / 'result.json', _json_bytes(result))
        pending.update({'state': 'staged', 'result': result})
        atomic(pending_path, pending)
        return result


def _activation_path(stage):
    return Path(stage) / 'activation.json'


def _stage_from_pending(root, pending):
    stage = root / pending.get('stage_path', '')
    if (
        not pending.get('stage_path')
        or not stage.is_dir()
        or not (stage / 'manifest.json').is_file()
        or not (stage / 'candidate/registry.sqlite').is_file()
    ):
        raise WorkflowError('MIGRATION_RECOVERY_REQUIRED stage is incomplete')
    return stage


def _stored_manifest(stage):
    value = read(Path(stage) / 'manifest.json')
    from .orchestration_types import MigrationManifest
    return MigrationManifest.from_dict(value)


def _authority_counts(path):
    if not Path(path).is_file():
        return {'exists': False, 'objects': 0, 'requests': 0,
                'operations': 0, 'events': 0, 'aliases': 0, 'sequences': 0}
    with _sqlite_snapshot(path) as snapshot:
        with closing(sqlite3.connect(
            snapshot.as_uri() + '?mode=ro', uri=True,
        )) as db:
            return {
                'exists': True,
                **{
                    table: db.execute(
                        'SELECT COUNT(*) FROM ' + table
                    ).fetchone()[0]
                    for table in (
                        'objects', 'requests', 'operations', 'events',
                        'aliases', 'sequences',
                    )
                },
            }


def _target_reusable(path):
    counts = _authority_counts(path)
    if not counts['exists']:
        return {'existing': False, 'empty': True}
    empty = all(
        counts[table] == 0
        for table in (
            'objects', 'requests', 'operations', 'events',
            'aliases', 'sequences',
        )
    )
    return {'existing': True, 'empty': empty}


def _published_activation(path, pending):
    if not Path(path).is_file():
        return None
    try:
        with _sqlite_snapshot(path) as snapshot:
            with closing(sqlite3.connect(
                snapshot.as_uri() + '?mode=ro', uri=True,
            )) as db:
                db.row_factory = sqlite3.Row
                if IdentityStore._schema_problem(db) is not None:
                    return None
                metadata = {
                    row['key']: row['value']
                    for row in db.execute(
                        "SELECT key,value FROM metadata WHERE key IN ("
                        "'migration_state','migration_request_uid',"
                        "'migration_manifest_uid','migration_mapping_digest')"
                    )
                }
            logical_digest = _sqlite_logical_digest(snapshot)
            mutation_digest = _sqlite_mutation_digest_snapshot(snapshot)
    except (OSError, sqlite3.Error, WorkflowError):
        return None
    if (
        metadata.get('migration_state') != 'active'
        or metadata.get('migration_request_uid') != pending.get('request_uid')
        or metadata.get('migration_manifest_uid') != pending.get('migration_uid')
        or metadata.get('migration_mapping_digest') != pending.get('mapping_digest')
    ):
        return None
    return {
        'logical_digest': logical_digest,
        'mutation_digest': mutation_digest,
    }


def _activate_stage(root, pending):
    stage = _stage_from_pending(root, pending)
    manifest = _stored_manifest(stage)
    mapping = read(stage / 'mapping.json')
    mapping_digest = _digest_bytes(_json_bytes(mapping))
    cursor = read(stage / 'cursor.json')
    candidate = stage / 'candidate/registry.sqlite'
    if (
        cursor.get('request_uid') != pending.get('request_uid')
        or cursor.get('mapping_digest') != mapping_digest
        or cursor.get('candidate_logical_digest')
        != _sqlite_logical_digest(candidate)
    ):
        raise WorkflowError('MIGRATION_STAGE_CHANGED')
    authority = root / '.gsp/orchestration/registry.sqlite'
    published = _published_activation(authority, pending)
    activated_digest = (
        published['logical_digest'] if published is not None else None
    )
    activated_mutation_digest = (
        published['mutation_digest'] if published is not None else None
    )
    ignored_paths = [
        '.gsp/writer.lock',
        '.gsp/writer.mutex',
        MIGRATION_PENDING_RELATIVE,
        stage.relative_to(root).as_posix(),
    ]
    if activated_digest is not None:
        ignored_paths.extend((
            '.gsp/orchestration/registry.sqlite',
            '.gsp/orchestration/registry.sqlite-wal',
            '.gsp/orchestration/registry.sqlite-shm',
            '.gsp/orchestration/registry.json',
        ))
    verify_source_for_apply(root, manifest, ignored_paths=ignored_paths)
    reusable = _target_reusable(authority)
    if activated_digest is None and not reusable['empty']:
        raise WorkflowError('MIGRATION_TARGET_NOT_EMPTY pending remains')
    authority.parent.mkdir(parents=True, exist_ok=True)
    temporary = authority.with_name(
        '.registry.activation.' + uuid.uuid4().hex + '.sqlite'
    )
    try:
        if activated_digest is None:
            _backup_sqlite(candidate, temporary)
            with closing(sqlite3.connect(temporary, timeout=30)) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('BEGIN IMMEDIATE')
                try:
                    state = db.execute(
                        "SELECT value FROM metadata WHERE key='migration_state'"
                    ).fetchone()
                    if state is None or state['value'] != 'staged':
                        raise WorkflowError('MIGRATION_STAGE_INVALID')
                    db.execute(
                        "UPDATE metadata SET value='active' "
                        "WHERE key='migration_state'"
                    )
                    db.execute(
                        "INSERT INTO metadata(key,value) VALUES('incarnation',?)",
                        (uuid.uuid4().hex,),
                    )
                    # This is the final database mutation. The authority cannot
                    # be opened until this transaction publishes the canonical
                    # schema marker.
                    db.execute(
                        "INSERT INTO metadata(key,value) VALUES('schema_version',?)",
                        ('1',),
                    )
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
                problem = IdentityStore._schema_problem(db)
                if problem is not None:
                    raise WorkflowError('MIGRATION_ACTIVATION_INVALID ' + problem)
                integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
                if integrity != 'ok':
                    raise WorkflowError('MIGRATION_ACTIVATION_INVALID')
            activated_digest = _sqlite_logical_digest(temporary)
            activated_mutation_digest = _sqlite_mutation_digest(temporary)
        prior_cursor = next(
            (
                row for row in cursor.get('files', [])
                if row.get('source_path')
                == '.gsp/orchestration/registry.sqlite'
            ),
            None,
        )
        prior_metadata_cursor = next(
            (
                row for row in cursor.get('files', [])
                if row.get('source_path')
                == '.gsp/orchestration/registry.json'
            ),
            None,
        )
        if temporary.is_file():
            os.replace(temporary, authority)
        receipt = {
            'schema': 1,
            'state': 'active',
            'request_uid': pending['request_uid'],
            'fingerprint': pending.get('fingerprint'),
            'migration_uid': pending['migration_uid'],
            'project_uid': pending['project_uid'],
            'source_digest': pending['source_digest'],
            'mapping_digest': pending['mapping_digest'],
            'candidate_logical_digest': pending['result'][
                'candidate_logical_digest'
            ],
            'activated_logical_digest': activated_digest,
            'activated_mutation_digest': activated_mutation_digest,
            'target_path': authority.relative_to(root).as_posix(),
            'prior_authority': prior_cursor,
            'prior_metadata': prior_metadata_cursor,
        }
        metadata_path = root / '.gsp/orchestration/registry.json'
        atomic(metadata_path, {
            'schema': 1,
            'project_uid': pending['project_uid'],
            'created_at': pending['created_at'],
            'migration_uid': pending['migration_uid'],
            'migration_request_uid': pending['request_uid'],
        })
        _write_bytes(_activation_path(stage), _json_bytes(receipt))
        pending.update({
            'state': 'activated',
            'activation': receipt,
        })
        atomic(root / MIGRATION_PENDING_RELATIVE, pending)
        (root / MIGRATION_PENDING_RELATIVE).unlink()
        return receipt
    finally:
        temporary.unlink(missing_ok=True)


def _read_activation(root):
    for path in sorted((root / '.gsp/migrations').glob('*/activation.json')):
        try:
            value = read(path)
        except (OSError, WorkflowError):
            continue
        value['_path'] = path
        return value
    return None


def apply_migration(root, manifest, *, request_uid=None):
    """Stage, activate and make the canonical allocator writable at the last step."""
    root = _require_control_root(Path(root).resolve())
    receipt = _read_activation(root)
    if receipt is not None:
        if (
            manifest.uid != receipt.get('migration_uid')
            or manifest.source_digest != receipt.get('source_digest')
        ):
            raise WorkflowError('MIGRATION_REQUEST_CONFLICT')
        if request_uid is not None and request_uid != receipt.get('request_uid'):
            raise WorkflowError('MIGRATION_REQUEST_CONFLICT')
        return {key: value for key, value in receipt.items() if key != '_path'}
    staged = stage_migration(root, manifest, request_uid=request_uid)
    from .workflow_locks import (locked)
    with locked(root):
        pending = read(root / MIGRATION_PENDING_RELATIVE)
        if pending.get('request_uid') != staged['request_uid']:
            raise WorkflowError('MIGRATION_REQUEST_CONFLICT')
        return _activate_stage(root, pending)


def resume_migration(root, *, request_uid=None):
    """Resume one persisted stage using the original source manifest and request."""
    root = _require_control_root(Path(root).resolve())
    pending_path = root / MIGRATION_PENDING_RELATIVE
    if not pending_path.is_file():
        receipt = _read_activation(root)
        if receipt is None:
            raise WorkflowError('MIGRATION_RECOVERY_REQUIRED no pending migration')
        if request_uid is not None and receipt.get('request_uid') != request_uid:
            raise WorkflowError('MIGRATION_REQUEST_CONFLICT')
        return {key: value for key, value in receipt.items() if key != '_path'}
    pending = read(pending_path)
    if request_uid is not None and pending.get('request_uid') != request_uid:
        raise WorkflowError('MIGRATION_REQUEST_CONFLICT')
    if pending.get('state') == 'restored-readonly':
        raise WorkflowError('MIGRATION_RESTORE_REQUIRES_EXPLICIT_RESTAGE')
    from .workflow_locks import (locked)
    with locked(root):
        return _activate_stage(root, pending)


def restore_migration(root, *, request_uid=None):
    """Restore only before new writes and return a read-only recovery point."""
    root = _require_control_root(Path(root).resolve())
    pending_path = root / MIGRATION_PENDING_RELATIVE
    if pending_path.is_file():
        pending = read(pending_path)
        if pending.get('state') == 'restored-readonly':
            if request_uid is not None and (
                pending.get('request_uid') != request_uid
            ):
                raise WorkflowError('MIGRATION_REQUEST_CONFLICT')
            return pending['restore']
    receipt = _read_activation(root)
    if receipt is None:
        raise WorkflowError('MIGRATION_RECOVERY_REQUIRED no active migration')
    if request_uid is not None and request_uid != receipt.get('request_uid'):
        raise WorkflowError('MIGRATION_REQUEST_CONFLICT')
    authority = root / receipt['target_path']
    from .workflow_locks import (locked)
    with locked(root):
        expected_mutation = receipt.get('activated_mutation_digest')
        if (
            not authority.is_file()
            or not isinstance(expected_mutation, str)
            or _sqlite_mutation_digest(authority) != expected_mutation
        ):
            raise WorkflowError(
                'MIGRATION_RESTORE_AFTER_WRITES destructive restore refused'
            )
        stage = Path(receipt['_path']).parent
        prior = receipt.get('prior_authority')
        restore_point = stage / 'recovery/restored-registry.sqlite'
        metadata_path = root / '.gsp/orchestration/registry.json'
        if prior and prior.get('backup_path'):
            _backup_sqlite(root / prior['backup_path'], restore_point)
            _backup_sqlite(restore_point, authority)
            prior_metadata = receipt.get('prior_metadata')
            if prior_metadata and prior_metadata.get('backup_path'):
                _write_bytes(
                    metadata_path,
                    (root / prior_metadata['backup_path']).read_bytes(),
                )
            else:
                metadata_path.unlink(missing_ok=True)
        else:
            restore_point.parent.mkdir(parents=True, exist_ok=True)
            _backup_sqlite(authority, restore_point)
            authority.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
        result = {
            'schema': 1,
            'state': 'restored-readonly',
            'request_uid': receipt['request_uid'],
            'migration_uid': receipt['migration_uid'],
            'recovery_point': restore_point.relative_to(root).as_posix(),
            'allocator_write': False,
        }
        pending = {
            'schema': 1,
            'state': 'restored-readonly',
            'request_uid': receipt['request_uid'],
            'fingerprint': receipt.get('fingerprint'),
            'migration_uid': receipt['migration_uid'],
            'project_uid': receipt['project_uid'],
            'source_digest': receipt['source_digest'],
            'mapping_digest': receipt['mapping_digest'],
            'stage_path': stage.relative_to(root).as_posix(),
            'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'restore': result,
        }
        atomic(root / MIGRATION_PENDING_RELATIVE, pending)
        return result


LEGACY_TASK_FIELDS = (
    'id', 'title', 'acceptance', 'depends', 'role', 'parent_task',
)


def _legacy_document(root, state, kind):
    documents = state.get('documents') if isinstance(state.get('documents'), dict) else {}
    relative = documents.get(kind)
    if kind == 'design' and not relative:
        relative = documents.get('spec')
    if not relative:
        folder = state.get('doc_dir')
        if isinstance(folder, str) and folder:
            relative = str(
                Path(folder) / ('spec.md' if kind == 'design' else 'plan.md')
            )
    if not isinstance(relative, str) or not relative:
        raise WorkflowError('LEGACY_ADOPTION_DOCUMENT_MISSING ' + kind)
    candidate = Path(relative)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        candidate = candidate.resolve()
        candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        raise WorkflowError('LEGACY_ADOPTION_DOCUMENT_ESCAPE ' + kind) from exc
    if not candidate.is_file():
        raise WorkflowError('LEGACY_ADOPTION_DOCUMENT_MISSING ' + kind)
    return candidate


def _legacy_task_definitions(state):
    result = []
    for task in state.get('tasks') or ():
        if not isinstance(task, dict):
            continue
        definition = {
            key: task[key] for key in LEGACY_TASK_FIELDS if key in task
        }
        definition['status'] = 'pending'
        result.append(definition)
    return result


def _lookup_adoption_request(store, request):
    with closing(store._connect()) as db:
        row = db.execute(
            'SELECT * FROM requests WHERE request_uid=?',
            (request.request_uid,),
        ).fetchone()
        if row is None:
            return None
        if not store._request_row_matches(row, request, 'work', ''):
            raise WorkflowError('LEGACY_ADOPTION_REQUEST_CONFLICT')
        object_row = db.execute(
            'SELECT * FROM objects WHERE uid=?',
            (row['result_uid'],),
        ).fetchone()
        if object_row is None:
            raise WorkflowError('IDENTITY_AUTHORITY_CORRUPT')
        return store._object(object_row)


def adopt_legacy_run(root, legacy_run_id, new_run_id, request_uid):
    """Adopt one legacy-only run without changing its source facts."""
    from argparse import Namespace
    from .documents import Governance
    from .orchestration_types import RequestRef
    from .project import (Project)
    from .workflow_locks import (locked)
    from .workflow_support import (explicit_work_number_from_run_id, slug)

    root = _require_control_root(Path(root).resolve())
    slug(legacy_run_id)
    slug(new_run_id)
    if not isinstance(request_uid, str) or not request_uid.strip():
        raise WorkflowError('LEGACY_ADOPTION_REQUEST_REQUIRED')
    if legacy_run_id == new_run_id:
        raise WorkflowError('LEGACY_ADOPTION_RUN_CONFLICT')

    with locked(root):
        legacy_state_path = root / '.gsp/runs' / legacy_run_id / 'state.json'
        if not legacy_state_path.is_file():
            raise WorkflowError('LEGACY_ADOPTION_SOURCE_NOT_FOUND')
        try:
            legacy_bytes = legacy_state_path.read_bytes()
            legacy_state = json.loads(legacy_bytes.decode('utf-8-sig'))
        except (OSError, UnicodeError, ValueError) as exc:
            raise WorkflowError('LEGACY_ADOPTION_SOURCE_INVALID') from exc
        if legacy_state.get('id') != legacy_run_id:
            raise WorkflowError('LEGACY_ADOPTION_SOURCE_CHANGED')
        design_source = _legacy_document(root, legacy_state, 'design')
        plan_source = _legacy_document(root, legacy_state, 'plan')
        document_bytes = {
            'design': design_source.read_bytes(),
            'plan': plan_source.read_bytes(),
        }
        source_digest = _digest_bytes(_json_bytes({
            'schema': 1,
            'legacy_run_id': legacy_run_id,
            'state': legacy_bytes.hex(),
            'design': document_bytes['design'].hex(),
            'plan': document_bytes['plan'].hex(),
        }))
        input_digest = _digest_bytes(_json_bytes({
            'schema': 1,
            'legacy_run_id': legacy_run_id,
            'new_run_id': new_run_id,
            'source_digest': source_digest,
        }))

        store = IdentityStore(root)
        store.ensure_authority()
        request = RequestRef(
            request_uid=request_uid,
            logical_key='legacy-adopt:' + legacy_run_id,
            input_digest=input_digest,
            project_uid=store.project_uid(),
            work_uid=None,
            attempt_uid=None,
            operation_kind='legacy-run-adopt',
        )

        run_dir, doc_dir = Project(root).paths(new_run_id)
        new_state_path = run_dir / 'state.json'
        existing_state = None
        if new_state_path.is_file():
            try:
                existing_state = read(new_state_path)
            except (OSError, WorkflowError) as exc:
                raise WorkflowError('LEGACY_ADOPTION_RUN_CONFLICT') from exc
            adoption = existing_state.get('legacy_adoption')
            if adoption is None:
                allocated = _lookup_adoption_request(store, request)
                if (
                    allocated is None
                    or existing_state.get('id') != new_run_id
                    or existing_state.get('work_uuid') != allocated.uid
                ):
                    raise WorkflowError('LEGACY_ADOPTION_RUN_CONFLICT')
            elif (
                not isinstance(adoption, dict)
                or adoption.get('request_uid') != request_uid
                or adoption.get('legacy_run_id') != legacy_run_id
                or adoption.get('new_run_id') != new_run_id
                or adoption.get('source_digest') != source_digest
            ):
                raise WorkflowError('LEGACY_ADOPTION_RUN_CONFLICT')
        else:
            allocated = _lookup_adoption_request(store, request)
            if allocated is None and (run_dir.exists() or doc_dir.exists()):
                raise WorkflowError('LEGACY_ADOPTION_RUN_CONFLICT')

        try:
            allocated = store.allocate(
                'work',
                None,
                request,
                explicit_id=explicit_work_number_from_run_id(new_run_id),
            )
        except IdentityStoreError as exc:
            raise WorkflowError(str(exc)) from exc
        work_uid = allocated.uid
        number = allocated.canonical_id

        book = Governance(root)
        try:
            record = book.get(work_uid)
        except WorkflowError as exc:
            if not str(exc).startswith('DOCUMENT_NOT_FOUND '):
                raise
            suffix = re.sub(
                '[^a-z0-9]+',
                '-',
                str(legacy_state.get('title') or new_run_id).lower(),
            ).strip('-')[:36] or 'legacy-adoption'
            created = time.gmtime()
            folder = (
                f'docs/work/{time.strftime("%Y", created)}/'
                f'{time.strftime("%m", created)}/{number}-{suffix}'
            )
            brief = (
                'Legacy-only run adopted into a new run; approvals and accepted '
                'state require revalidation.'
            )
            record = {
                'schema': 1,
                'kind': 'work',
                'uuid': work_uid,
                'number': number,
                'title': str(legacy_state.get('title') or new_run_id),
                'summary': brief,
                'source': 'legacy-only adoption from ' + legacy_run_id,
                'stage': 'idea',
                'folder': folder,
                'documents': {
                    'brief': folder + '/brief.md',
                    'design': folder + '/spec.md',
                    'plan': folder + '/plan.md',
                },
                'modules': [],
                'relations': [],
                'run_id': new_run_id,
                'generation': 1,
                'created': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'updated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'history': [],
                'legacy_aliases': [
                    {'alias': legacy_run_id, 'writable': False},
                ],
                'legacy_adoption': {
                    'schema': 1,
                    'request_uid': request_uid,
                    'legacy_run_id': legacy_run_id,
                    'source_digest': source_digest,
                },
            }
            book._commit({
                folder + '/record.json': record,
                folder + '/brief.md': '# ' + record['title'] + '\n\n' + brief + '\n',
                '.gsp/work-counter.json': {'next': store.next_value('work', None)},
            })
        else:
            adoption = record.get('legacy_adoption') or {}
            if (
                record.get('uuid') != work_uid
                or record.get('run_id') != new_run_id
                or adoption.get('request_uid') != request_uid
                or adoption.get('legacy_run_id') != legacy_run_id
            ):
                raise WorkflowError('LEGACY_ADOPTION_RECORD_CONFLICT')

        if record.get('stage') == 'idea':
            record = book.stage(
                work_uid,
                'design',
                'Legacy-only adoption starts a new design session',
            )
        record = book.get(work_uid)
        plan_relative = record['folder'] + '/plan.md'
        adoption = {
            'schema': 1,
            'request_uid': request_uid,
            'legacy_run_id': legacy_run_id,
            'source_digest': source_digest,
        }
        aliases = [{'alias': legacy_run_id, 'writable': False}]
        record_needs_save = (
            (record.get('documents') or {}).get('plan') != plan_relative
            or record.get('legacy_adoption') != adoption
            or record.get('legacy_aliases') != aliases
        )
        if record_needs_save:
            record['documents']['plan'] = plan_relative
            record['legacy_adoption'] = adoption
            record['legacy_aliases'] = aliases
            record = book._save(record)

        adoption_complete = (
            isinstance(existing_state, dict)
            and isinstance(existing_state.get('legacy_adoption'), dict)
        )
        if not adoption_complete:
            for relative, value in (
                (record['documents']['design'], document_bytes['design']),
                (record['documents']['plan'], document_bytes['plan']),
            ):
                target = root / relative
                if target.is_file():
                    if target.read_bytes() != value:
                        raise WorkflowError('LEGACY_ADOPTION_TARGET_CHANGED')
                else:
                    _write_bytes(target, value)

        project = Project(root)
        if not new_state_path.is_file():
            project.new(Namespace(
                id=new_run_id,
                title=record['title'],
                mode='supervised',
                quote='legacy-only adoption; approvals require revalidation',
                work=work_uid,
                legacy_layout=False,
                intent='single-task',
                size='small',
                independent_verification=False,
                protect_existing=False,
            ))
        state = project.ledger_state(new_run_id)
        if state.get('work_uuid') != work_uid:
            raise WorkflowError('LEGACY_ADOPTION_RUN_CONFLICT')
        adoption = {
            'schema': 1,
            'request_uid': request_uid,
            'legacy_run_id': legacy_run_id,
            'new_run_id': new_run_id,
            'source_digest': source_digest,
            'legacy_aliases': [
                {'alias': legacy_run_id, 'writable': False},
            ],
            'documents': {
                'design': record['documents']['design'],
                'plan': record['documents']['plan'],
            },
            'copied_tasks': len(legacy_state.get('tasks') or ()),
            'acceptance_reused': False,
            'approval_reused': False,
            'seal_reused': False,
            'sessions_reused': False,
            'receipts_reused': False,
        }
        if state.get('legacy_adoption') != adoption:
            state['tasks'] = _legacy_task_definitions(legacy_state)
            state['legacy_adoption'] = adoption
            state['design_approval'] = None
            state['plan_seal'] = None
            state['review'] = None
            state['status'] = 'design'
            state['evidence'] = []
            state['authorizations'] = []
            state['external_links'] = []
            project.write(
                state,
                'legacy-adopted',
                'Legacy-only source copied; approvals and acceptance reset',
                action_uid='legacy-adopt-' + input_digest,
            )
            state = project.ledger_state(new_run_id)

        if (
            legacy_state_path.read_bytes() != legacy_bytes
            or design_source.read_bytes() != document_bytes['design']
            or plan_source.read_bytes() != document_bytes['plan']
        ):
            raise WorkflowError('LEGACY_ADOPTION_SOURCE_CHANGED')
        return {
            'schema': 1,
            'state': 'adopted-design',
            'request_uid': request_uid,
            'legacy_run_id': legacy_run_id,
            'new_run_id': new_run_id,
            'work_uid': work_uid,
            'number': number,
            'source_digest': source_digest,
            'design_digest': _digest_bytes(document_bytes['design']),
            'plan_digest': _digest_bytes(document_bytes['plan']),
            'accepted': False,
            'approval_reused': False,
            'seal_reused': False,
        }
