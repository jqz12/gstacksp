"""Strict execution equivalence over canonical receipts and original logs.

The SQLite table is a rebuildable artifact locator in the existing project
database. It cannot grant a pass: every selected receipt, result and log is
reverified. Unknown input closure or a later applicable failure prevents reuse.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3

from .identity_store import IdentityStore, IdentityStoreError


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def execution_identity(*, argv, cwd, env, inputs, contract, phase, coverage,
                       closed_inputs=False, configuration=None,
                       source_fingerprint=None):
    if (closed_inputs is not True or not argv or not inputs or not contract
            or not phase or not coverage or not isinstance(env, dict)):
        return None
    cwd = Path(cwd).resolve()
    executable = Path(argv[0])
    if executable.is_absolute() or len(executable.parts) > 1:
        executable = executable if executable.is_absolute() else cwd / executable
    else:
        located = shutil.which(str(executable), path=env.get('PATH', env.get('Path')))
        if not located:
            return None
        executable = Path(located)
    try:
        executable = executable.resolve(strict=True)
        tool_digest = _sha(executable)
    except OSError:
        return None
    value = {'schema': 1, 'argv': list(argv), 'cwd': os.path.normcase(str(cwd)),
             'environment_digest': _digest(env), 'inputs': inputs,
             'tool': {'path': str(executable), 'sha256': tool_digest},
             'contract': contract, 'phase': phase, 'coverage': coverage,
             'closed_inputs': True}
    if configuration is not None:
        value['configuration'] = configuration
    if source_fingerprint is not None:
        value['source_fingerprint'] = source_fingerprint
    return {**value, 'digest': _digest(value)}


def _key(identity):
    if not isinstance(identity, dict) or identity.get('closed_inputs') is not True:
        return None
    body = {key: value for key, value in identity.items() if key != 'digest'}
    return identity['digest'] if identity.get('digest') == _digest(body) else None


def intact_execution(execution, timeout):
    if (execution.get('status') != 'completed' or execution.get('returncode') != 0
            or execution.get('launch_error') or execution.get('interruption')
            or execution.get('process_alive') is not False):
        return False
    duration = execution.get('duration_s')
    if not isinstance(duration, (int, float)) or duration > timeout:
        return False
    integrity = execution.get('log_integrity') or {}
    for name in ('stdout', 'stderr'):
        row = integrity.get(name) or {}
        path = Path(execution.get(name + '_path') or '')
        if (row.get('truncated') is not False or not path.is_file()
                or path.stat().st_size != row.get('bytes') or _sha(path) != row.get('sha256')):
            return False
    return True


class EvidenceCache:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.store = IdentityStore(self.root)
        self.path = self.store.path
        self._memory = {}

    def _open(self, write=False):
        if not self.path.is_file():
            return None
        try:
            self.store.project_uid()  # Existing authority only; never initialize here.
            db = sqlite3.connect(self.path if write else self.path.as_uri() + '?mode=ro',
                                 uri=not write, timeout=15)
            db.row_factory = sqlite3.Row
            if write:
                db.execute('CREATE TABLE IF NOT EXISTS evidence_cache ('
                           'ordinal INTEGER PRIMARY KEY, identity_digest TEXT NOT NULL, '
                           'receipt TEXT NOT NULL, receipt_digest TEXT NOT NULL, '
                           'result TEXT, result_digest TEXT, '
                           'UNIQUE(identity_digest,receipt,receipt_digest))')
                db.execute('CREATE INDEX IF NOT EXISTS evidence_cache_identity '
                           'ON evidence_cache(identity_digest, ordinal)')
            elif db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_cache'").fetchone() is None:
                db.close()
                return None
            return db
        except (IdentityStoreError, sqlite3.Error):
            return None

    def publish(self, identity, receipt):
        key = _key(identity)
        path = Path(receipt).resolve()
        if not key or not path.is_relative_to(self.root / '.gsp/runs'):
            return False
        value = json.loads(path.read_text(encoding='utf-8'))
        if value.get('execution_identity') != identity:
            return False
        result = Path(value.get('execution_result') or '')
        if not result.is_absolute():
            result = self.root / result
        result = result.resolve()
        if not result.is_relative_to(self.root / '.gsp/runs') or not result.is_file():
            return False
        locator = {'receipt': str(path.relative_to(self.root)), 'receipt_digest': _sha(path),
                   'result': str(result.relative_to(self.root)), 'result_digest': _sha(result)}
        self._memory[key] = locator
        db = self._open(write=True)
        if db is None:
            return False
        with closing(db), db:
            db.execute('INSERT OR IGNORE INTO evidence_cache('
                       'identity_digest,receipt,receipt_digest,result,result_digest) VALUES(?,?,?,?,?)',
                       (key, str(path.relative_to(self.root)), _sha(path),
                        str(result.relative_to(self.root)), _sha(result)))
        return True

    def select(self, identity, *, timeout):
        key = _key(identity)
        if key is None:
            return None
        row = self._memory.get(key)
        if row is None:
            db = self._open()
            if db is None:
                return None
            with closing(db):
                row = db.execute('SELECT * FROM evidence_cache WHERE identity_digest=? '
                                 'ORDER BY ordinal DESC LIMIT 1', (key,)).fetchone()
        if row is None:
            return None
        try:
            path, result = self.root / row['receipt'], self.root / row['result']
            if (not path.resolve().is_relative_to(self.root) or not result.resolve().is_relative_to(self.root)
                    or _sha(path) != row['receipt_digest'] or _sha(result) != row['result_digest']):
                return None
            receipt = json.loads(path.read_text(encoding='utf-8'))
            execution = json.loads(result.read_text(encoding='utf-8'))
            if (receipt.get('status') != 'pass' or receipt.get('execution_identity') != identity
                    or not intact_execution(execution, timeout)):
                return None
            return {'receipt': str(path), 'execution': execution, 'reused': True}
        except (OSError, ValueError, TypeError):
            return None

    def count(self):
        db = self._open()
        if db is None:
            return 0
        with closing(db):
            return db.execute('SELECT count(*) FROM evidence_cache').fetchone()[0]


class QualityReportLocator:
    """Rebuildable exact locator for one work's bound quality reports."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.store = IdentityStore(self.root)
        self.path = self.store.path

    def _open(self, write=False):
        if not self.path.is_file():
            return None
        try:
            self.store.project_uid()
            db = sqlite3.connect(
                self.path if write else self.path.as_uri() + '?mode=ro',
                uri=not write, timeout=15,
            )
            db.row_factory = sqlite3.Row
            if write:
                db.execute(
                    'CREATE TABLE IF NOT EXISTS quality_report_locator ('
                    'ordinal INTEGER PRIMARY KEY, work_uid TEXT NOT NULL, '
                    'mode TEXT NOT NULL, phase TEXT NOT NULL, '
                    'verification_epoch INTEGER NOT NULL, '
                    'binding_status TEXT NOT NULL, source_fingerprint TEXT, '
                    'contract_digest TEXT, config_digest TEXT NOT NULL, '
                    'report TEXT NOT NULL, report_digest TEXT NOT NULL, '
                    'UNIQUE(work_uid,mode,phase,verification_epoch,'
                    'binding_status,source_fingerprint,contract_digest,'
                    'config_digest,report,report_digest))'
                )
                db.execute(
                    'CREATE INDEX IF NOT EXISTS quality_report_exact '
                    'ON quality_report_locator(work_uid,mode,phase,'
                    'verification_epoch,binding_status,source_fingerprint,'
                    'contract_digest,config_digest,ordinal)'
                )
            elif db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='quality_report_locator'"
            ).fetchone() is None:
                db.close()
                return None
            return db
        except (IdentityStoreError, sqlite3.Error):
            return None

    def _values(self, report, work_uid, mode, phase, verification_epoch,
                binding_status, source_fingerprint, contract_digest,
                config_digest):
        path = Path(report).resolve()
        if not path.is_relative_to(self.root / '.gsp/runs') or not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, TypeError, ValueError):
            return None
        declared_work_uid = value.get('work_uid') or value.get('task_id')
        if declared_work_uid != str(work_uid):
            return None
        return {
            'work_uid': str(work_uid), 'mode': str(mode),
            'phase': str(phase), 'verification_epoch': int(verification_epoch),
            'binding_status': str(binding_status),
            'source_fingerprint': source_fingerprint,
            'contract_digest': contract_digest,
            'config_digest': str(config_digest),
            'report': str(path.relative_to(self.root)),
            'report_digest': _sha(path),
        }

    @staticmethod
    def _insert(db, values):
        db.execute(
            'INSERT OR IGNORE INTO quality_report_locator('
            'work_uid,mode,phase,verification_epoch,binding_status,'
            'source_fingerprint,contract_digest,config_digest,report,'
            'report_digest) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (
                values['work_uid'], values['mode'], values['phase'],
                values['verification_epoch'], values['binding_status'],
                values['source_fingerprint'], values['contract_digest'],
                values['config_digest'], values['report'],
                values['report_digest'],
            ),
        )

    def publish(self, report, work_uid, mode, phase, verification_epoch,
                binding_status, source_fingerprint, contract_digest,
                config_digest):
        values = self._values(
            report, work_uid, mode, phase, verification_epoch,
            binding_status, source_fingerprint, contract_digest,
            config_digest,
        )
        if values is None:
            return False
        db = self._open(write=True)
        if db is None:
            return False
        with closing(db), db:
            self._insert(db, values)
        return True

    def select_bound(self, work_uid, mode, phase, verification_epoch,
                     source_fingerprint, contract_digest, config_digest):
        db = self._open()
        if db is None:
            return None
        with closing(db):
            row = db.execute(
                'SELECT * FROM quality_report_locator '
                'WHERE work_uid=? AND mode=? AND phase=? '
                'AND verification_epoch=? AND binding_status=\'bound\' '
                'AND source_fingerprint=? AND contract_digest=? '
                'AND config_digest=? ORDER BY ordinal DESC LIMIT 1',
                (
                    str(work_uid), str(mode), str(phase),
                    int(verification_epoch), source_fingerprint,
                    contract_digest, str(config_digest),
                ),
            ).fetchone()
            return dict(row) if row is not None else None

    def latest(self, work_uid, mode):
        db = self._open()
        if db is None:
            return None
        with closing(db):
            row = db.execute(
                'SELECT * FROM quality_report_locator '
                'WHERE work_uid=? AND mode=? ORDER BY ordinal DESC LIMIT 1',
                (str(work_uid), str(mode)),
            ).fetchone()
            return dict(row) if row is not None else None

    def rebuild_work(self, work_uid):
        """Rebuild only one work's locator rows from its own report directory."""
        run_slugs = set()
        db = self._open()
        if db is not None:
            with closing(db):
                for row in db.execute(
                    'SELECT report FROM quality_report_locator WHERE work_uid=?',
                    (str(work_uid),),
                ):
                    parts = Path(row['report']).parts
                    if len(parts) >= 5 and parts[:2] == ('.gsp', 'runs'):
                        run_slugs.add(parts[2])
        try:
            controller = self.store.read_work_controller(str(work_uid))
        except IdentityStoreError:
            controller = None
        if controller is not None:
            run_slugs.add(controller['run_id'])
        runs_root = self.root / '.gsp/runs'
        if runs_root.is_dir():
            for state_path in runs_root.glob('*/state.json'):
                try:
                    state = json.loads(state_path.read_text(encoding='utf-8-sig'))
                except (OSError, TypeError, ValueError):
                    continue
                if (
                    isinstance(state, dict)
                    and state.get('work_uuid') == str(work_uid)
                    and state.get('id') == state_path.parent.name
                ):
                    run_slugs.add(state_path.parent.name)
        if not run_slugs and (runs_root / str(work_uid) / 'quality').is_dir():
            run_slugs.add(str(work_uid))
        if not run_slugs:
            return 0
        db = self._open(write=True)
        if db is None:
            return 0
        count = 0
        with closing(db), db:
            db.execute(
                'DELETE FROM quality_report_locator WHERE work_uid=?',
                (str(work_uid),),
            )
            for run_slug in sorted(run_slugs):
                quality_root = runs_root / run_slug / 'quality'
                if not quality_root.is_dir():
                    continue
                for path in quality_root.glob('*/report.json'):
                    try:
                        report = json.loads(path.read_text(encoding='utf-8'))
                        binding = report.get('binding') or {}
                        declared_work_uid = (
                            report.get('work_uid') or report.get('task_id')
                        )
                        if (
                            report.get('task_id') != run_slug
                            or declared_work_uid != str(work_uid)
                        ):
                            continue
                        values = self._values(
                            path, work_uid, report['mode'], report['phase'],
                            report.get('verification_epoch', 0),
                            report.get('binding_status'),
                            report.get('source_fingerprint'),
                            report.get('contract'), report.get('config_digest'),
                        )
                    except (OSError, TypeError, ValueError, KeyError):
                        continue
                    if values is None or binding.get('status') != values['binding_status']:
                        continue
                    self._insert(db, values)
                    count += 1
        return count

    def count(self, work_uid):
        db = self._open()
        if db is None:
            return 0
        with closing(db):
            return db.execute(
                'SELECT count(*) FROM quality_report_locator WHERE work_uid=?',
                (str(work_uid),),
            ).fetchone()[0]


def declared_inputs(root, paths, argv=()):
    """Hash an explicitly reviewed local input closure; missing inputs are unknown."""
    root = Path(root).resolve()
    result = {}
    candidates = []
    explicit_metadata = []
    for relative in paths or []:
        named = root / relative
        path = named.resolve()
        if (not path.is_relative_to(root) or not path.exists() or named.is_symlink()
                or (path.relative_to(root).parts and path.relative_to(root).parts[0] == '.git')):
            return None
        if path.relative_to(root).parts and path.relative_to(root).parts[0] == '.gsp':
            explicit_metadata.append(path)
        candidates.extend(path.rglob('*') if path.is_dir() else [path])
    # Direct local entrypoints remain inputs even when stored under .gsp.
    for arg in argv:
        if not isinstance(arg, str) or arg.startswith('-') or '\n' in arg:
            continue
        named = root / arg
        path = named.resolve()
        if named.is_symlink() or getattr(named, 'is_junction', lambda: False)():
            return None
        if path.is_relative_to(root) and path.relative_to(root).parts[:1] == ('.git',):
            return None
        if path.is_file() and path.is_relative_to(root):
            candidates.append(path)
    for path in candidates:
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            return None
        if not path.is_file():
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            return None
        relative = path.relative_to(root)
        if relative.parts[0] == '.git':
            continue
        if (relative.parts[0] == '.gsp'
                and not any(path == declared or path.is_relative_to(declared) for declared in explicit_metadata)
                and path not in [root / str(arg) for arg in argv]):
            continue
        result[relative.as_posix()] = _sha(path)
    return result or None
