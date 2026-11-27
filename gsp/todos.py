"""Human Markdown owns backlog prose; local records own execution provenance."""
from pathlib import Path
import json
import re
import uuid

from .core import (WorkflowError, atomic, inside, now, read, redact, sha)
from .identity_store import IdentityStore, IdentityStoreError
from .orchestration_types import DocumentEdit, RequestRef, digest_value

FIELDS = ('what', 'why', 'context', 'priority', 'effort', 'source', 'origin', 'blocking',
          'status', 'evidence', 'resolution', 'history')
HUMAN = ('what', 'why', 'context', 'effort', 'priority', 'depends_on', 'blocked_by', 'completed')
LABELS = {key: key.capitalize() for key in HUMAN}
LABELS.update(depends_on='Depends on', blocked_by='Blocked by')
MUTABLE_FIELDS = ('title', 'what', 'why', 'context', 'priority', 'effort', 'depends_on', 'blocked_by')
LIFECYCLE_FIELDS = {'status', 'evidence', 'resolution', 'history', 'completed', 'completion_unverified', 'source', 'origin', 'blocking', 'links', 'object_uid', 'component'}
RECORD = ('source', 'origin', 'blocking', 'evidence', 'resolution', 'history', 'links')
IDENTITY = r'(?:GSP-\d{4,}|TODO-\d{6,})'
PATTERN = re.compile(
    r'<!-- gsp-todo:(' + IDENTITY + r') -->\n(.*?)<!-- /gsp-todo:\1 -->',
    re.S,
)
MARKER = '<!-- gsp-backlog:v2 -->'
INTRO = '项目长期待办；当前实施任务在 docs/gsp/tasks/。未解决事项不能仅因交付而标为完成。'
SKELETON = '# TODOS\n\n' + MARKER + '\n\n' + INTRO + '\n\n## 项目\n\n## Completed\n'


def field_digest(value):
    return sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def clean(value):
    text = ' '.join(str(value or '').split())
    if '<!--' in text or '-->' in text:
        raise WorkflowError('BACKLOG_INVALID reserved marker in item text')
    return redact(text)


def prose(value):
    text = str(value or '').strip()
    if '<!--' in text or '-->' in text or re.search(r'^## ', text, re.M):
        raise WorkflowError('BACKLOG_INVALID reserved marker/section in item text')
    return redact(text)


def signature(item):
    return sha(json.dumps({k: item.get(k, '') for k in ('title', *HUMAN, '_extra')}, sort_keys=True).encode())


class Backlog:
    def __init__(self, root):
        self.root = Path(root).resolve()
        candidates = [self.root / name for name in ('TODOS.md', 'TODO.md')]
        self.meta = self.root / '.gsp/backlog'
        if any(not inside(self.root, p) for p in (*candidates, self.meta)):
            raise WorkflowError('BACKLOG_ESCAPE backlog resolves outside project')
        existing = [p for p in candidates if p.exists()]
        if len(existing) > 1:
            raise WorkflowError('BACKLOG_AMBIGUOUS both TODOS.md and TODO.md exist; choose one explicitly')
        self.file = existing[0] if existing else candidates[0]
        self.records_file = self.meta / 'records.json'
        self.transaction = self.meta / 'transaction.json'
        if any(not inside(self.root, p) for p in (self.records_file, self.transaction, self.meta/'history', self.meta/'migrations')):
            raise WorkflowError('BACKLOG_ESCAPE execution storage resolves outside project')
        if self.file.exists() and not self.file.is_file():
            raise WorkflowError('BACKLOG_INVALID backlog is not a file')

    def initialize(self):
        if not self.file.exists():
            atomic(self.file, SKELETON)

    def records(self):
        if self.records_file.exists():
            data = read(self.records_file)
            if data.get('schema') != 2 or not isinstance(data.get('items'), dict) or not isinstance(data.get('document'), dict):
                raise WorkflowError('BACKLOG_INVALID execution records schema')
            for ident, record in data['items'].items():
                if (not re.fullmatch(IDENTITY, ident) or not isinstance(record, dict)
                        or any(not isinstance(record.get(k), str) for k in RECORD if k != 'links')
                        or record.get('blocking') not in ('yes','no') or not isinstance(record.get('links'), list)
                        or (record['blocking']=='yes' and not record['origin']) or not isinstance(record.get('component'), str)
                        or (record.get('completed_hash') is not None and not re.fullmatch(r'[a-f0-9]{64}', str(record['completed_hash'])))
                        or (record.get('object_uid') not in (None, '') and not re.fullmatch(r'[a-f0-9]{32}', str(record['object_uid'])))
                        or any(not isinstance(l, dict) or not all(isinstance(l.get(k), str) for k in ('run','task','at')) for l in record['links'])):
                    raise WorkflowError('BACKLOG_INVALID incomplete execution record ' + ident)
            return data
        return {'schema': 2, 'items': {}, 'document': {}}

    def load(self):
        if self.transaction.exists():
            raise WorkflowError('BACKLOG_RECOVERY_REQUIRED inspect pending transaction; use todo-recover')
        text = self.file.read_text(encoding='utf-8-sig') if self.file.exists() else ''
        records = self.records()
        rows = []
        for match in PATTERN.finditer(text):
            body = match[2]
            lines = body.splitlines()
            prefix = f'### {match[1]} — '
            if not lines or not lines[0].startswith(prefix):
                raise WorkflowError('BACKLOG_INVALID malformed item heading')
            headings = re.findall(r'^## (.+)$', text[:match.start()], re.M)
            section = headings[-1] if headings else '项目'
            fields = list(re.finditer(r'^\*\*([^*\n]+):\*\*[ \t]*(.*)$', body, re.M))
            values, extras = {}, []
            for index, field in enumerate(fields):
                key = field[1].lower().replace(' ', '_')
                value = body[field.start(2):fields[index+1].start() if index+1 < len(fields) else len(body)].strip()
                if key in values:
                    raise WorkflowError('BACKLOG_INVALID duplicate field in ' + match[1])
                values[key] = '' if value == '(none)' else value
                if key not in (*FIELDS, *HUMAN):
                    extras.append(body[field.start():fields[index+1].start() if index+1 < len(fields) else len(body)].strip())
            legacy = MARKER not in text and all(k in values for k in FIELDS)
            if MARKER not in text and not legacy and any(k in values for k in ('origin','blocking','status','evidence','resolution','history')):
                raise WorkflowError('BACKLOG_INVALID incomplete legacy execution fields')
            preamble = body[len(lines[0]):fields[0].start() if fields else len(body)].strip()
            if preamble:
                extras.insert(0, preamble)
            if not legacy:
                for index, field in enumerate(fields):
                    if field[1].lower().replace(' ', '_') in FIELDS and field[1].lower().replace(' ', '_') not in HUMAN:
                        extras.append(body[field.start():fields[index+1].start() if index+1 < len(fields) else len(body)].strip())
            record = records['items'].get(match[1], {})
            if not isinstance(record, dict):
                raise WorkflowError('BACKLOG_INVALID malformed execution record')
            item = {'id': match[1], 'title': lines[0][len(prefix):],
                    **{k: values.get(k, '') for k in HUMAN},
                    **{k: record.get(k, [] if k == 'links' else '') for k in RECORD},
                    'component': record.get('component', '项目') if section == 'Completed' else section,
                    'object_uid': record.get('object_uid', ''),
                    '_extra': '\n\n'.join(extras), '_legacy': legacy}
            if legacy:
                item.update({k: values[k] for k in FIELDS})
                item['links'] = []
                if item['status'] not in ('open', 'done') or item['blocking'] not in ('yes', 'no'):
                    raise WorkflowError('BACKLOG_INVALID legacy execution state')
                if item['status'] == 'done':
                    if not all(item[k] for k in ('evidence', 'resolution', 'history')):
                        raise WorkflowError('BACKLOG_INVALID legacy completion has no provenance')
                    date = re.search(r'\d{4}-\d{2}-\d{2}', item['history'])
                    item['completed'] = f'历史完成记录 ({date[0] if date else "日期未记录"})'
                item['completion_unverified'] = False
            else:
                item['source'] = item['source'] or 'manual:' + self.file.name + '#' + item['id']
                item['blocking'] = item['blocking'] or 'no'
                displayed_done = section == 'Completed' or bool(item['completed'])
                trusted = bool(displayed_done and item['completed'] and record.get('completed_hash') == signature(item)
                               and item['evidence'] and item['history'] and item['resolution'])
                item['status'] = 'done' if trusted else 'open'
                item['completion_unverified'] = displayed_done and not trusted
            if (not all(item[k].strip() for k in ('title','what','why','context'))
                    or item['priority'] not in ('P0','P1','P2','P3','P4') or item['effort'] not in ('S','M','L','XL')):
                raise WorkflowError('BACKLOG_INVALID missing human fields or invalid priority/effort')
            rows.append(item)
        if len(re.findall(r'<!-- /?gsp-todo:', text)) != 2 * len(rows) or len({r['id'] for r in rows}) != len(rows):
            raise WorkflowError('BACKLOG_INVALID incomplete markers or duplicate IDs')
        if MARKER in text and rows and not self.records_file.exists():
            raise WorkflowError('BACKLOG_RECORDS_MISSING restore execution records from backup; do not assume no blockers')
        self._text, self._rows, self._records = text, rows, records
        return text, rows

    @staticmethod
    def public(item):
        return {k: v for k, v in item.items() if not k.startswith('_')}

    @staticmethod
    def render(item):
        lines = [f'<!-- gsp-todo:{item["id"]} -->', f'### {item["id"]} — {item["title"]}', '']
        for key in HUMAN:
            if item.get(key):
                lines.extend([f'**{LABELS[key]}:** {item[key]}', ''])
        if item.get('_extra'):
            lines.extend([item['_extra'], ''])
        return '\n'.join(lines) + f'<!-- /gsp-todo:{item["id"]} -->'

    @staticmethod
    def execution(item):
        record = {k: item.get(k, [] if k == 'links' else '') for k in RECORD}
        record.update(component=item.get('component', '项目'),
                      object_uid=item.get('object_uid', ''),
                      completed_hash=signature(item) if item['status'] == 'done' else None)
        return record

    def compose(self, text, rows):
        # Reorder only owned blocks. Preserve unowned sections and notes.
        base = PATTERN.sub('', text)
        base = re.sub(r'\n{3,}', '\n\n', base).strip() + '\n'
        if MARKER not in base:
            base = base.rstrip() + '\n\n' + MARKER + '\n'
        sections = {}
        for row in rows:
            section = 'Completed' if row['status'] == 'done' else row.get('component', '项目')
            sections.setdefault(section, []).append(row)
        if not re.search(r'^## Completed\s*$', base, re.M):
            base = base.rstrip() + '\n\n## Completed\n'
        for section, items in sections.items():
            heading = re.search(r'^## ' + re.escape(section) + r'\s*$', base, re.M)
            if not heading:
                pos = re.search(r'^## Completed\s*$', base, re.M).start()
                base = base[:pos].rstrip() + '\n\n## ' + section + '\n\n' + base[pos:]
                heading = re.search(r'^## ' + re.escape(section) + r'\s*$', base, re.M)
            next_heading = re.search(r'^## ', base[heading.end():], re.M)
            end = heading.end()+next_heading.start() if next_heading else len(base)
            blocks = '\n\n'.join(self.render(r) for r in sorted(items, key=lambda r:(r['priority'], r['id'])))
            base = base[:end].rstrip() + '\n\n' + blocks + '\n\n' + base[end:].lstrip('\n')
        return base.rstrip() + '\n'

    def _target_path(self):
        return self.file.resolve().relative_to(self.root).as_posix()

    def _check_transaction_owner(self, payload, operation_uid, target_path):
        owned = isinstance(payload.get('operation_uid'), str) and bool(payload.get('operation_uid').strip())
        if operation_uid is None:
            if owned:
                raise WorkflowError('BACKLOG_RECOVERY_OWNERSHIP_MISMATCH')
            return
        if not isinstance(operation_uid, str) or not operation_uid.strip() or target_path is None:
            raise WorkflowError('BACKLOG_RECOVERY_OWNERSHIP_MISMATCH')
        try:
            candidate = Path(target_path)
            if not candidate.is_absolute():
                candidate = self.root / candidate
            expected_target = candidate.resolve().relative_to(self.root).as_posix()
        except (OSError, ValueError) as exc:
            raise WorkflowError('BACKLOG_RECOVERY_OWNERSHIP_MISMATCH') from exc
        if (payload.get('operation_uid') != operation_uid
                or payload.get('target_path') != expected_target):
            raise WorkflowError('BACKLOG_RECOVERY_OWNERSHIP_MISMATCH')

    def commit(self, text, records, event, operation_uid=None):
        if self.transaction.exists():
            raise WorkflowError('BACKLOG_RECOVERY_REQUIRED')
        before = self.file.read_text(encoding='utf-8-sig') if self.file.exists() else ''
        if hasattr(self, '_text') and before != self._text:
            raise WorkflowError('BACKLOG_CHANGED canonical document changed before publication')
        old_records = self.records_file.read_text(encoding='utf-8') if self.records_file.exists() else None
        records['document'] = {'path': self.file.name, 'sha256': sha(text.encode())}
        records['updated'] = now()
        payload = {'document': self.file.name, 'target_path': self._target_path(),
                   'operation_uid': operation_uid, 'before': before, 'after': text,
                   'old_records': old_records, 'records': records, 'event': event, 'at': now()}
        atomic(self.transaction, payload)
        atomic(self.file, text)
        atomic(self.records_file, records)
        self.finish_transaction(payload, 'normal publication')

    def finish_transaction(self, payload, reason):
        atomic(self.meta / 'history' / (uuid.uuid4().hex + '.json'), {**payload, 'finished': now(), 'reason': clean(reason)})
        self.transaction.unlink()

    def recover(self, reason, operation_uid=None, target_path=None):
        if not self.transaction.exists() or not clean(reason):
            raise WorkflowError('BACKLOG_RECOVERY_REQUIRED pending transaction and investigation reason required')
        payload = read(self.transaction)
        self._check_transaction_owner(payload, operation_uid, target_path)
        if payload.get('document') != self.file.name:
            raise WorkflowError('BACKLOG_RECOVERY_DIVERGED canonical document differs')
        text = self.file.read_text(encoding='utf-8-sig') if self.file.exists() else ''
        current = self.records_file.read_text(encoding='utf-8') if self.records_file.exists() else None
        expected = json.dumps(payload['records'], ensure_ascii=False, indent=2) + '\n'
        if text not in (payload['before'], payload['after']) or current not in (payload['old_records'], expected):
            raise WorkflowError('BACKLOG_RECOVERY_DIVERGED preserve intervening human changes; reconcile manually')
        atomic(self.file, payload['after'])
        atomic(self.records_file, payload['records'])
        self.finish_transaction(payload, reason)
        return {'recovered': True, 'document': str(self.file)}

    def migrate(self):
        text, rows = self.load()
        if not any(r['_legacy'] for r in rows):
            return {'migrated': False, 'file': str(self.file)}
        backup = self.meta / 'migrations' / uuid.uuid4().hex / self.file.name
        atomic(backup, text)
        records = self._records
        for item in rows:
            records['items'][item['id']] = self.execution(item)
        self.commit(self.compose(text, rows), records, 'legacy migration')
        return {'migrated': True, 'file': str(self.file), 'backup': str(backup), 'items': len(rows)}

    def store(self, item):
        self.initialize()
        self.migrate()
        text, rows = self.load()
        rows = [r for r in rows if r['id'] != item['id']] + [item]
        self._records['items'][item['id']] = self.execution(item)
        self.commit(self.compose(text, rows), self._records, 'store ' + item['id'])

    def summary(self, origin=None):
        text, rows = self.load()
        outside = PATTERN.sub('', text)
        outside = re.sub(r'^## .+$', '', outside, flags=re.M).replace(MARKER, '').replace(INTRO, '')
        unmanaged = bool(outside.replace('# TODOS', '').strip())
        selected = [r for r in rows if origin is None or r['origin'] == origin or any(l['run'] == origin for l in r['links'])]
        present = {r['id'] for r in rows}
        orphaned = [{'id': ident, 'origin': record['origin'], 'blocking': record['blocking'], 'completed': bool(record.get('completed_hash'))}
                    for ident, record in self._records['items'].items() if ident not in present]
        return {'file': str(self.file), 'exists': self.file.exists(), 'records': str(self.records_file), 'orphaned_records': orphaned,
                'document_changed': bool(self._records['document'] and self._records['document'].get('sha256') != sha(text.encode())),
                'items': [self.public(r) for r in sorted(selected, key=lambda r:(r['status']=='done', r['priority'], r['id']))],
                'open': sum(r['status']=='open' for r in selected), 'unmanaged_content': unmanaged}

    def format(self):
        self.migrate()
        text, rows = self.load()
        for row in rows:
            self._records['items'][row['id']] = self.execution(row)
        self.commit(self.compose(text, rows), self._records, 'format human document')
        return {'formatted': True, 'file': str(self.file), 'items': len(rows)}

    def plan_field_merge(self, expected_version, operations):
        """Validate and compose a field-level merge against the latest document."""
        if not operations or any(not isinstance(item, DocumentEdit) for item in operations):
            raise WorkflowError('DOCUMENT_EDIT_REQUIRED')
        if (
            not isinstance(expected_version, str)
            or re.fullmatch(r"[a-f0-9]{64}", expected_version) is None
        ):
            raise WorkflowError('DOCUMENT_VERSION_REQUIRED')
        text, loaded = self.load()
        rows = [dict(row) for row in loaded]
        records = json.loads(json.dumps(self._records, ensure_ascii=False))
        seen = set()
        for edit in operations:
            if not isinstance(edit.target_uid, str) or not edit.target_uid.strip():
                raise WorkflowError('DOCUMENT_TARGET_REQUIRED')
            if not isinstance(edit.field, str) or not edit.field.strip():
                raise WorkflowError('DOCUMENT_FIELD_REQUIRED')
            if edit.operation != 'update':
                raise WorkflowError('DOCUMENT_OPERATION_UNSUPPORTED')
            if edit.field in LIFECYCLE_FIELDS:
                raise WorkflowError('DOCUMENT_LIFECYCLE_FORBIDDEN')
            if edit.field not in MUTABLE_FIELDS:
                raise WorkflowError('DOCUMENT_FIELD_UNSUPPORTED')
            row = next((item for item in rows
                        if edit.target_uid in (item['id'], item.get('object_uid'))), None)
            if row is None:
                raise WorkflowError('DOCUMENT_TARGET_NOT_FOUND')
            if row['status'] != 'open' or row.get('completion_unverified'):
                raise WorkflowError('DOCUMENT_ITEM_NOT_EDITABLE')
            token = (row['id'], edit.field)
            if token in seen:
                raise WorkflowError('DOCUMENT_EDIT_CONFLICT')
            seen.add(token)
            if not edit.before_digest or edit.before_digest != field_digest(row.get(edit.field, '')):
                raise WorkflowError('DOCUMENT_EDIT_CONFLICT')
            value = prose(edit.new_value or '')
            if edit.field == 'title' and not value:
                raise WorkflowError('DOCUMENT_FIELD_VALUE_REQUIRED')
            if edit.field == 'priority' and value not in ('P0', 'P1', 'P2', 'P3', 'P4'):
                raise WorkflowError('DOCUMENT_FIELD_VALUE_INVALID')
            if edit.field == 'effort' and value not in ('S', 'M', 'L', 'XL'):
                raise WorkflowError('DOCUMENT_FIELD_VALUE_INVALID')
            if edit.field in ('what', 'why', 'context') and not value:
                raise WorkflowError('DOCUMENT_FIELD_VALUE_REQUIRED')
            row[edit.field] = value
        for row in rows:
            records['items'][row['id']] = self.execution(row)
        merged = self.compose(text, rows)
        return {
            'text': merged,
            'records': records,
            'new_digest': sha(merged.encode()),
            'expected_version': expected_version,
        }

    def add(self, args):
        _, rows = self.load()
        item = {key: prose(getattr(args, key, '')) for key in ('title','what','why','context','priority','effort','source','origin','depends_on','blocked_by')}
        item['component'] = clean(getattr(args, 'component', '项目'))
        if not all(item[k] for k in ('title','what','why','context','source')) or not item['component'] or item['component']=='Completed':
            raise WorkflowError('BACKLOG_INVALID human fields, source and component required')
        if '\n' in item['title']:
            raise WorkflowError('BACKLOG_INVALID multiline heading')
        if args.blocking and not item['origin']:
            raise WorkflowError('BACKLOG_ORIGIN_REQUIRED blockers must name the affected workflow')
        match = next((r for r in rows if r['what'].casefold()==item['what'].casefold() and r['source'].casefold()==item['source'].casefold()), None)
        if match:
            if match['origin'] != item['origin'] or match['blocking'] != ('yes' if args.blocking else 'no'):
                raise WorkflowError('BACKLOG_CONFLICT duplicate has different origin or blocking classification; reconcile explicitly')
            return {'item': self.public(match), 'reused': True}
        store = IdentityStore(self.root)
        try:
            store.ensure_authority()
        except IdentityStoreError as exc:
            raise WorkflowError(str(exc)) from exc
        request_input = {key: item[key] for key in (
            'title', 'what', 'why', 'context', 'priority', 'effort', 'source',
            'origin', 'depends_on', 'blocked_by', 'component',
        )}
        input_digest = digest_value(request_input)
        logical_key = 'todo-create:' + input_digest
        request = RequestRef(
            request_uid=digest_value({
                'project_uid': store.project_uid(),
                'logical_key': logical_key,
                'operation_kind': 'todo-create',
            }),
            logical_key=logical_key,
            input_digest=input_digest,
            project_uid=store.project_uid(),
            work_uid=None,
            attempt_uid=None,
            operation_kind='todo-create',
        )
        try:
            allocated = store.allocate('todo', None, request)
        except IdentityStoreError as exc:
            raise WorkflowError(str(exc)) from exc
        item.update(id=allocated.canonical_id,
                    object_uid=allocated.uid,
                    blocking='yes' if args.blocking else 'no', status='open',
                    evidence='', resolution='', history='', links=[], completed='')
        self.store(item)
        return {'item': self.public(item), 'reused': False}

    def item(self, ident):
        rows = self.load()[1]
        match = next((r for r in rows if r['id']==ident), None)
        store = IdentityStore(self.root)
        if store.exists() and match is None:
            try:
                resolved = store.resolve('todo', ident, None, write=False)
            except IdentityStoreError as exc:
                raise WorkflowError(str(exc)) from exc
            match = next((r for r in rows
                          if r.get('object_uid') == resolved.uid or
                          r['id'] == resolved.canonical_id), None)
        if match is None:
            raise WorkflowError('BACKLOG_NOT_FOUND ' + ident)
        return match

    def done(self, ident, evidence, run, task, reason, version=None):
        item = self.item(ident)
        if item['status'] != 'open':
            raise WorkflowError('BACKLOG_ALREADY_DONE')
        if not clean(reason):
            raise WorkflowError('BACKLOG_REASON_REQUIRED explain verified behavior')
        item.update(status='done', evidence=evidence, resolution=clean(reason),
                    completed=f'{clean(version) if version else "未指定版本"} ({now()[:10]})')
        item['history'] = (item['history'] + f' | {now()} completed by {run}/{task}: {clean(reason)}; evidence={evidence}').strip(' |')
        self.store(item)
        return {'item': self.public(item)}

    def reopen(self, ident, reason):
        item = self.item(ident)
        if item['status'] != 'done' and not item.get('completion_unverified'):
            raise WorkflowError('BACKLOG_NOT_DONE only completed items need reopening')
        if not clean(reason):
            raise WorkflowError('BACKLOG_REASON_REQUIRED')
        item['history'] += f' | {now()} reopened: {clean(reason)}'
        item.update(status='open', evidence='', resolution='', completed='')
        self.store(item)
        return {'item': self.public(item)}

    def link(self, ident, run, task):
        self.migrate()
        item = self.item(ident)
        if not any(l['run']==run and l['task']==task for l in item['links']):
            item['links'].append({'run':run,'task':task,'at':now()})
        self._records['items'][item['id']] = self.execution(item)
        self.commit(self._text, self._records, 'link ' + item['id'])
        return {'item':self.public(item)}
