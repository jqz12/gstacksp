"""Append-only local knowledge events. Conclusions never authorize actions."""
import json
from pathlib import Path
import unicodedata
import uuid

from .runtime import WorkflowError, atomic, inside, now, redact


def normalized(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


class Learnings:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.file = self.root/'docs/gsp/learnings.jsonl'
        if not inside(self.root, self.file):
            raise WorkflowError('LEARNING_ESCAPE')

    def load(self):
        rows, events = {}, []
        if not self.file.exists(): return rows, events
        for number, line in enumerate(self.file.read_text(encoding='utf-8-sig').splitlines(), 1):
            try:
                e = json.loads(line)
                if not isinstance(e, dict): raise ValueError('object required')
                kind = e.get('event', 'add')
                if not isinstance(e.get('source'), str) or not e['source'].strip(): raise ValueError('source required')
                if 'reason' in e and (not isinstance(e['reason'],str) or not e['reason'].strip()): raise ValueError('reason must be text')
                if kind == 'add':
                    if not all(isinstance(e.get(k), str) and e[k].strip() for k in ('id','text','at')) or e['id'] in rows: raise ValueError('invalid add')
                    old = e.get('supersedes')
                    if old and (old not in rows or rows[old]['status'] != 'active' or not e.get('reason')): raise ValueError('invalid correction')
                    rows[e['id']] = {**e, 'status': 'active', 'sources': [e['source']]}
                    if old:
                        rows[old].update(status='superseded', replaced_by=e['id'], reason=e['reason'])
                elif kind == 'source':
                    row = rows[e['target']]
                    if row['status'] != 'active': raise ValueError('source targets inactive entry')
                    if e['source'] not in row['sources']: row['sources'].append(e['source'])
                elif kind == 'invalidate':
                    row = rows[e['target']]
                    if row['status'] != 'active' or not e.get('reason'): raise ValueError('invalid invalidation')
                    row.update(status='invalid', reason=e['reason'], invalidated_source=e['source'])
                else: raise ValueError('unknown event')
                events.append(e)
            except (ValueError, KeyError, TypeError) as exc:
                raise WorkflowError(f'LEARNING_CORRUPT line {number}: {exc}') from exc
        return rows, events

    def append(self, event):
        # Atomic whole-log publication prevents a killed writer leaving half JSON.
        content = self.file.read_text(encoding='utf-8-sig') if self.file.exists() else ''
        atomic(self.file, content + ('\n' if content and not content.endswith('\n') else '') + json.dumps(event, ensure_ascii=False) + '\n')

    def add(self, text, source, supersedes=None, reason=None):
        if not text or not text.strip() or not source or not source.strip(): raise WorkflowError('LEARNING_TEXT_SOURCE_REQUIRED')
        rows, _ = self.load()
        text, source = redact(text.strip()), redact(source.strip())
        if supersedes and (supersedes not in rows or rows[supersedes]['status'] != 'active' or not reason or not reason.strip()):
            raise WorkflowError('LEARNING_CORRECTION_REQUIRES_ACTIVE_ID_AND_REASON')
        matches = [r for r in rows.values() if normalized(r['text']) == normalized(text)]
        if matches:
            active = next((r for r in matches if r['status'] == 'active'), None)
            if supersedes or not active: raise WorkflowError('LEARNING_DUPLICATE_OR_INACTIVE inspect history before correcting')
            if source not in active['sources']:
                self.append({'event':'source','target':active['id'],'source':source,'at':now()})
            return self.load()[0][active['id']]
        event = {'event':'add','id':uuid.uuid4().hex[:12],'at':now(),'text':text,'source':source}
        if supersedes: event.update(supersedes=supersedes, reason=redact(reason.strip()))
        self.append(event)
        return self.load()[0][event['id']]

    def invalidate(self, target, reason, source):
        rows, _ = self.load()
        if target not in rows or rows[target]['status'] != 'active' or not reason or not reason.strip() or not source or not source.strip():
            raise WorkflowError('LEARNING_INVALIDATION_REQUIRES_ACTIVE_ID_REASON_SOURCE')
        self.append({'event':'invalidate','target':target,'reason':redact(reason.strip()),'source':redact(source.strip()),'at':now()})
        return self.load()[0][target]

    def query(self, query='', history=False):
        rows, events = self.load()
        result = []
        for row in rows.values():
            if not history and row['status'] != 'active': continue
            if query.casefold() not in (' '.join([row['text'], *row['sources'], row.get('reason','')])).casefold(): continue
            if history: row = {**row, 'events':[e for e in events if e.get('id') == row['id'] or e.get('target') == row['id'] or e.get('supersedes') == row['id']]}
            result.append(row)
        return result
