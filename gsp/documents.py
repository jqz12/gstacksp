"""Versioned document records and rebuildable local discovery.

Mutations must run under runtime.locked(root), as CLI dispatch does. Index rows
are hints: execution approval and evidence continue to belong to runtime.
"""
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from .runtime import WorkflowError, atomic, digest, git_identity, inside, now, read, redact, sha


WORK_STAGE_ALLOWED = {
    'idea': {'design', 'paused', 'cancelled'},
    'design': {'paused', 'cancelled'},
    'approved': {'design', 'paused', 'cancelled'},
    'implementing': {'design', 'paused', 'cancelled'},
    'ready': {'design', 'paused', 'cancelled', 'delivered', 'closed'},
    'delivered': {'closed'},
    'paused': {'design', 'cancelled'},
    'cancelled': {'design'},
    'closed': set(),
}


def work_record_snapshot(root, identity):
    """Read one work record without Git probes or the document full scan.

    The document index is a cache and is used only as a bounded hint.  A
    missing or stale row falls back to the two supported record locations;
    callers still perform the authoritative Governance validation later.
    """
    root = Path(root).resolve()
    index = root / '.gsp/documents.sqlite'
    if index.is_file():
        try:
            db = sqlite3.connect(index, timeout=2)
            try:
                row = db.execute(
                    'SELECT path FROM entries WHERE uuid=? OR number=?',
                    (identity, identity)).fetchone()
            finally:
                db.close()
        except sqlite3.Error:
            row = None
        if row:
            path = root / row[0]
            if inside(root, path) and path.is_file():
                try:
                    record = read(path)
                except (WorkflowError, OSError):
                    record = None
                if record and (record.get('uuid') == identity or
                               (record.get('number') and record.get('number') == identity)):
                    return record
    base = root / 'docs/work'
    if not base.is_dir():
        return None
    for pattern in ('*/*/record.json', '*/*/*/record.json'):
        for path in base.glob(pattern):
            if not inside(root, path):
                continue
            try:
                record = read(path)
            except (WorkflowError, OSError):
                continue
            if record.get('uuid') == identity or (record.get('number') and
                                                  record.get('number') == identity):
                return record
    return None


class Governance:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.base = self.root/'docs/work'
        self.modules = self.root/'docs/product/modules'
        self.index = self.root/'.gsp/documents.sqlite'
        self.pending = self.root/'.gsp/document-transaction.json'
        self.context={'root':str(self.root),'git':git_identity(self.root)}

    def path(self, rel):
        if Path(rel).is_absolute() or '..' in Path(rel).parts:
            raise WorkflowError('DOCUMENT_PATH_ESCAPE')
        p = self.root/rel
        if not inside(self.root, p): raise WorkflowError('DOCUMENT_PATH_ESCAPE')
        return p

    def _db(self):
        self.path('.gsp/documents.sqlite')
        self.index.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.index, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE IF NOT EXISTS entries (uuid TEXT PRIMARY KEY, kind TEXT, number TEXT, title TEXT, stage TEXT, path TEXT UNIQUE, hash TEXT, data TEXT)')
        db.execute('CREATE INDEX IF NOT EXISTS numbers ON entries(number)')
        db.execute('CREATE INDEX IF NOT EXISTS kinds ON entries(kind,stage)')
        db.execute('CREATE TABLE IF NOT EXISTS texts (path TEXT PRIMARY KEY, hash TEXT, body TEXT)')
        return db

    def _ensure(self):
        if self.pending.exists(): raise WorkflowError('DOCUMENT_TRANSACTION_PENDING run docs-recover')
        if self.path('.gsp/document-migration.json').exists():raise WorkflowError('DOCUMENT_MIGRATION_PENDING run docs-move-recover')
        if not self.index.exists(): self.rebuild()
        context=self.path('.gsp/document-index-context.json')
        if not context.exists() or read(context)!=self.context:
            self.rebuild()  # A branch/HEAD transition is an explicit cache boundary.
        try:
            with closing(self._db()) as db: db.execute('SELECT uuid FROM entries LIMIT 1').fetchall()
        except sqlite3.DatabaseError as exc:
            raise WorkflowError('DOCUMENT_INDEX_CORRUPT run docs-index --rebuild') from exc

    def _upsert(self, db, record, rel):
        db.execute('INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?,?,?,?)',
                   (record['uuid'], record['kind'], record.get('number'), record['title'], record.get('stage','active'), rel,
                    digest(self.path(rel)), json.dumps(record, ensure_ascii=False)))

    def rebuild(self):
        if self.pending.exists(): raise WorkflowError('DOCUMENT_TRANSACTION_PENDING run docs-recover')
        records = []
        for directory, pattern in ((self.base, '*/*/record.json'),
                                   (self.base, '*/*/*/record.json'), (self.modules, '*.json')):
            self.path(directory.relative_to(self.root))
            for p in directory.glob(pattern):
                self.path(p.relative_to(self.root))
                r = read(p)
                if r.get('kind') not in ('work','module') or not re.fullmatch('[a-f0-9]{32}', r.get('uuid','')):
                    raise WorkflowError('DOCUMENT_RECORD_INVALID '+str(p))
                records.append((r, p.relative_to(self.root).as_posix()))
        ids = [r['uuid'] for r,_ in records]
        if len(set(ids)) != len(ids): raise WorkflowError('DOCUMENT_UUID_CONFLICT')
        # Preserve a corrupt index for diagnosis. Only this explicit rebuild may
        # replace it; records above were validated before touching the cache.
        if self.index.exists(): self.index.replace(self.index.with_name('documents-old-'+uuid.uuid4().hex+'.sqlite'))
        with closing(self._db()) as db:
            for r,p in records: self._upsert(db,r,p)
            for p in self._markdown():
                if p.stat().st_size<=2_000_000:
                    try:text=p.read_text(encoding='utf-8-sig')
                    except UnicodeError:continue
                    db.execute('INSERT OR REPLACE INTO texts VALUES (?,?,?)',(p.relative_to(self.root).as_posix(),digest(p),text))
            db.commit()
        atomic(self.path('.gsp/document-index-context.json'),self.context)
        return {'indexed':len(records), 'index':str(self.index), 'full_scan':True}

    def _markdown(self):
        from .document_health import SKIP
        for p in self.root.glob('*.md'):
            if not p.is_symlink() and inside(self.root,p):yield p
        if not inside(self.root,self.root/'docs') or (self.root/'docs').is_symlink():raise WorkflowError('DOCUMENT_PATH_ESCAPE')
        for folder,dirs,files in os.walk(self.root/'docs',followlinks=False):
            dirs[:]=[d for d in dirs if d not in SKIP and not (Path(folder)/d).is_symlink() and not (Path(folder)/d).as_posix().endswith('/gsp/reports')]
            for name in files:
                p=Path(folder)/name
                if p.suffix=='.md' and not p.is_symlink() and inside(self.root,p):yield p

    def _row(self, identity):
        self._ensure()
        with closing(self._db()) as db:
            rows = db.execute('SELECT * FROM entries WHERE uuid=? OR number=?',(identity,identity)).fetchall()
        if len(rows) != 1: raise WorkflowError('DOCUMENT_ID_AMBIGUOUS' if rows else 'DOCUMENT_NOT_FOUND '+identity)
        return dict(rows[0])

    def get(self, identity):
        row = self._row(identity)
        record = read(self.path(row['path']))
        if record.get('uuid') != row['uuid']: raise WorkflowError('DOCUMENT_ID_CHANGED refresh index')
        if record.get('kind')=='work':
            folder=record.get('folder','')
            if not folder.startswith('docs/work/') or '..' in Path(folder).parts:
                raise WorkflowError('DOCUMENT_FOLDER_INVALID')
            self.path(folder)
        return record

    def _commit(self, changes):
        if self.pending.exists(): raise WorkflowError('DOCUMENT_TRANSACTION_PENDING run docs-recover')
        ops=[]
        for rel,value in changes.items():
            p=self.path(rel)
            text=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2)+'\n'
            ops.append({'path':rel,'before':digest(p) if p.exists() else None,'after':sha(text.encode()),'text':text})
        atomic(self.pending, {'id':uuid.uuid4().hex,'created':now(),'operations':ops})
        self._finish(read(self.pending))

    def _finish(self, transaction):
        # Preflight all destinations before applying anything, including recovery.
        for op in transaction['operations']:
            if sha(op['text'].encode())!=op['after']:raise WorkflowError('DOCUMENT_TRANSACTION_CORRUPT')
            p=self.path(op['path']); current=digest(p) if p.exists() else None
            if current not in (op['before'],op['after']): raise WorkflowError('DOCUMENT_RECOVERY_CONFLICT '+op['path'])
        for op in transaction['operations']:
            p=self.path(op['path'])
            current=digest(p) if p.exists() else None
            if current not in (op['before'],op['after']):raise WorkflowError('DOCUMENT_RECOVERY_CONFLICT '+op['path'])
            if not p.exists() or digest(p)!=op['after']: atomic(p,op['text'])
        with closing(self._db()) as db:
            for op in transaction['operations']:
                if op['path'].endswith('/record.json') or op['path'].startswith('docs/product/modules/'):
                    r=json.loads(op['text']); self._upsert(db,r,op['path'])
                elif op['path'].endswith('.md'):
                    db.execute('INSERT OR REPLACE INTO texts VALUES (?,?,?)',(op['path'],op['after'],op['text']))
            db.commit()
        archive=self.path('.gsp/document-history/'+transaction['id']+'.json')
        atomic(archive, {'id':transaction['id'],'completed':now(),'paths':[op['path'] for op in transaction['operations']]})
        self.pending.unlink()

    def recover(self):
        if not self.pending.exists(): return {'recovered':False}
        t=read(self.pending); self._finish(t)
        return {'recovered':True,'transaction':t['id']}

    def create(self, title, brief, source, modules=(), run_id=None, folder=None, number=None):
        self._ensure()
        if not title.strip() or not brief.strip() or not source.strip(): raise WorkflowError('DOCUMENT_SOURCE_REQUIRED')
        module_ids=[]
        for m in modules:
            r=self.get(m)
            if r['kind']!='module': raise WorkflowError('MODULE_REQUIRED')
            module_ids.append(r['uuid'])
        counter=self.path('.gsp/work-counter.json')
        if counter.exists():
            existing=read(counter)['next']
        else:
            with closing(self._db()) as db:
                nums=[int(r[0].split('-')[1]) for r in db.execute("SELECT number FROM entries WHERE kind='work'") if r[0]]
            existing=max(nums,default=0)+1
        if number:
            if not isinstance(number,str) or not re.fullmatch(r'GSP-\d{6}', number):
                raise WorkflowError('WORK_NUMBER_INVALID expected GSP-000000')
            token=number
            base=int(existing) if isinstance(existing,int) and not isinstance(existing,bool) else 0
            next_value=max(base + 1, int(number.split('-')[1]) + 1)
        else:
            if not isinstance(existing,int) or isinstance(existing,bool) or existing<1:raise WorkflowError('WORK_COUNTER_INVALID')
            number_val=existing
            with closing(self._db()) as db:
                if db.execute('SELECT 1 FROM entries WHERE number=?',('GSP-'+str(number_val).zfill(6),)).fetchone():
                    maximum=db.execute("SELECT MAX(CAST(substr(number,5) AS INTEGER)) FROM entries WHERE kind='work'").fetchone()[0]
                    number_val=max(number_val,(maximum or 0)+1)
            token='GSP-'+str(number_val).zfill(6)
            next_value=number_val+1
        wid=uuid.uuid4().hex
        suffix=re.sub('[^a-z0-9]+','-',title.lower()).strip('-')[:36]
        if not suffix and run_id:
            suffix=re.sub('[^a-z0-9]+','-',str(run_id).lower()).strip('-')[:36]
        suffix=suffix or 'work'
        created = datetime.now()
        folder=folder or f'docs/work/{created.year}/{created.month:02d}/{token}-{suffix}'
        if self.path(folder).exists(): raise WorkflowError('DOCUMENT_FOLDER_EXISTS')
        record={'schema':1,'kind':'work','uuid':wid,'number':token,'title':title,'summary':brief[:320],
                'source':redact(source),'stage':'idea','folder':folder,'documents':{'brief':folder+'/brief.md'},
                'modules':module_ids,'relations':[],'run_id':run_id,'generation':1,'created':now(),'updated':now(),'history':[]}
        changes={folder+'/record.json':record,folder+'/brief.md':'# '+title+'\n\n'+brief+'\n',
                 '.gsp/work-counter.json':{'next':next_value}}
        if not self.path('docs/work/project.json').exists():
            ident=read(self.root/'.gsp/project.json')['id'] if (self.root/'.gsp/project.json').exists() else uuid.uuid4().hex
            changes['docs/work/project.json']={'schema':1,'id':ident,'created':now()}
        self._commit(changes)
        return record

    def _save(self, r, extra=None):
        row=self._row(r['uuid'])
        if digest(self.path(row['path']))!=row['hash']: raise WorkflowError('DOCUMENT_EXTERNAL_EDIT run docs-index --rebuild')
        r['updated']=now()
        self._commit({row['path']:r,**(extra or {})})
        return r

    def refresh(self,paths):
        """Refresh only explicitly touched Markdown, never scan history."""
        self._ensure()
        with closing(self._db()) as db:
            for rel in paths:
                p=self.path(rel)
                if not p.is_file() or p.suffix!='.md' or p.stat().st_size>2_000_000:continue
                h=digest(p);old=db.execute('SELECT hash FROM texts WHERE path=?',(rel,)).fetchone()
                if not old or old[0]!=h:
                    db.execute('INSERT OR REPLACE INTO texts VALUES (?,?,?)',(rel,h,p.read_text(encoding='utf-8-sig')))
            db.commit()

    def stage(self, identity, target, reason, run_state=None, context=None):
        r=self.get(identity)
        if r['kind']!='work' or not reason.strip(): raise WorkflowError('WORK_REASON_REQUIRED')
        if target not in ('idea','design','paused','cancelled','delivered','closed'): raise WorkflowError('WORK_STAGE_REQUIRES_EXECUTION use approve/seal-plan/ready')
        allowed = WORK_STAGE_ALLOWED
        if target!=r['stage'] and target not in allowed.get(r['stage'],set()):raise WorkflowError('WORK_TRANSITION_INVALID')
        if r.get('run_id') and target in ('idea','design','paused','cancelled'):
            run=self.path('.gsp/runs/'+r['run_id']+'/state.json')
            if run.exists() and (read(run).get('inflight') or (run.parent/'driver/state.json').exists()):
                raise WorkflowError('WORK_EXECUTION_BUSY stop/reconcile the driver first')
        if target in ('delivered','closed'):
            from .runtime import Project
            if not r.get('run_id'): raise WorkflowError('WORK_NO_READY_RUN')
            state = run_state
            if state is None:
                state,_,_=Project(self.root).state(r['run_id'], context=context)
            if state['status']!='ready' or state.get('work_uuid')!=r['uuid']: raise WorkflowError('WORK_NOT_READY')
            if not r.get('closure_condition'): raise WorkflowError('WORK_CLOSURE_CONDITION_REQUIRED')
        before=r['stage']; r['stage']=target
        if target=='paused': r['previous_stage']=before
        r['history'].append({'at':now(),'from':before,'to':target,'reason':redact(reason)})
        extra={}
        if target=='design' and 'design' not in r['documents']:
            rel=r['folder']+'/spec.md'; r['documents']['design']=rel
            if not self.path(rel).exists():
                extra[rel]='# '+r['title']+' — spec\n\nDraft: discuss goals, constraints, alternatives and acceptance before approval.\n'
        return self._save(r,extra)

    def module(self, name, module_id=None, aliases=(), paths=(), parents=(), status=None):
        self._ensure()
        r=self.get(module_id) if module_id else {'schema':1,'kind':'module','uuid':uuid.uuid4().hex,'title':name,
           'aliases':[],'paths':[],'parents':[],'stage':'active','created':now(),'history':[]}
        if r['kind']!='module': raise WorkflowError('MODULE_REQUIRED')
        if status not in (None,'active','retired'):raise WorkflowError('MODULE_STATE_INVALID')
        if status:r['stage']=status
        ancestry=[]
        for p in parents:
            parent=self.get(p)
            if parent['kind']!='module' or parent['uuid']==r['uuid']: raise WorkflowError('MODULE_PARENT_INVALID')
            if r['uuid'] in self._ancestors(parent['uuid'],depth=10000): raise WorkflowError('MODULE_CYCLE')
            ancestry.append(parent['uuid'])
        for path in paths: self.path(path)
        r['history'].append({'at':now(),'title':r['title'],'paths':r['paths']})
        r['aliases']=sorted(set(r['aliases']+list(aliases)+[r['title']]))
        r['title']=name; r['paths']=list(paths) if paths else r['paths']
        r['parents']=ancestry if parents else r['parents']; r['updated']=now()
        if module_id: return self._save(r)
        self._commit({'docs/product/modules/'+r['uuid']+'.json':r}); return r

    def _ancestors(self, identity, depth=2):
        seen={identity}; frontier=[identity]
        for _ in range(depth):
            nexts=[]
            for mid in frontier:
                for pid in self.get(mid).get('parents',[]):
                    if pid not in seen: seen.add(pid); nexts.append(pid)
            frontier=nexts
            if not frontier:break
        return seen

    def update(self,identity,title=None,summary=None,modules=None):
        r=self.get(identity)
        if r['kind']!='work':raise WorkflowError('WORK_REQUIRED')
        if title:r['title']=title
        if summary:r['summary']=summary[:320]
        if modules is not None:
            rows=[self.get(m) for m in modules]
            if any(m['kind']!='module' for m in rows):raise WorkflowError('MODULE_REQUIRED')
            r['modules']=[m['uuid'] for m in rows]
        return self._save(r)

    def link(self, identity, target, relation, reason):
        if relation not in ('fixes','extends','supersedes','depends','derived-from','duplicate'): raise WorkflowError('WORK_RELATION_INVALID')
        r=self.get(identity); other=self.get(target)
        if r['kind']!='work' or other['kind']!='work' or not reason: raise WorkflowError('WORK_RELATION_INVALID')
        frontier=[other['uuid']]; seen=set()
        while frontier:
            token=frontier.pop()
            if token==r['uuid']: raise WorkflowError('WORK_RELATION_CYCLE')
            if token in seen: continue
            seen.add(token)
            frontier.extend(x['target'] for x in self.get(token).get('relations',[]) if x['type']==relation)
        r['relations'].append({'target':other['uuid'],'type':relation,'reason':redact(reason),'at':now(),'confirmed':True})
        return self._save(r)

    def search(self, query='', module=None, limit=10, offset=0, related=None):
        self._ensure()
        if not 1<=limit<=50 or offset<0: raise WorkflowError('DOCUMENT_QUERY_LIMIT 1..50')
        mids=self._ancestors(module) if module else None
        with closing(self._db()) as db:
            # Structural filtering in SQL keeps history out of model context;
            # module JSON membership is evaluated by SQLite's bundled JSON1.
            clauses=["kind='work'"]; params=[]
            if mids:
                clauses.append('EXISTS (SELECT 1 FROM json_each(entries.data,\'$.modules\') WHERE value IN ('+','.join('?' for _ in mids)+'))'); params+=list(mids)
            if related:
                token=self.get(related)['uuid']
                clauses.append("(uuid IN (SELECT json_extract(value,'$.target') FROM entries e,json_each(e.data,'$.relations') WHERE e.uuid=?) OR EXISTS (SELECT 1 FROM json_each(entries.data,'$.relations') WHERE json_extract(value,'$.target')=?))")
                params += [token,token]
            if query:
                # Escape LIKE metacharacters; user query is literal, never SQL.
                needle='%'+query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
                clauses.append("(title LIKE ? ESCAPE '\\' OR data LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM entries m, json_each(entries.data,'$.modules') j WHERE m.uuid=j.value AND m.kind='module' AND m.data LIKE ? ESCAPE '\\') OR EXISTS (SELECT 1 FROM texts t,json_each(entries.data,'$.documents') j WHERE t.path=j.value AND t.body LIKE ? ESCAPE '\\'))")
                params += [needle]*4
            sql='SELECT * FROM entries WHERE '+' AND '.join(clauses)+' ORDER BY rowid DESC LIMIT ? OFFSET ?'
            rows=db.execute(sql,params+[limit+1,offset]).fetchall()
            documents=[]
            if query and not module and not related:
                for row in db.execute("SELECT * FROM texts WHERE body LIKE ? ESCAPE '\\' ORDER BY path LIMIT ? OFFSET ?",(needle,limit+1,offset)):
                    pos=row['body'].lower().find(query.lower());start=max(0,pos-60)
                    p=self.path(row['path'])
                    documents.append({'path':row['path'],'excerpt':redact(row['body'][start:start+240]),
                                      'stale':not p.exists() or digest(p)!=row['hash'],'reason':'document body keyword match; validity requires review'})
        results=[]
        for row in rows[:limit]:
            r=json.loads(row['data']); p=self.path(row['path'])
            stale=not p.exists() or digest(p)!=row['hash']
            results.append({'uuid':r['uuid'],'number':r['number'],'title':r['title'],'summary':r['summary'],
                            'stage':r['stage'],'folder':r['folder'],'stale':stale,
                            'reason':'explicit task relationship' if related else 'module/ancestor match' if module else 'keyword/alias match' if query else 'recent record',
                            'execution_requires_resume_validation':bool(r.get('run_id'))})
        budget=max(0,limit-len(results))
        return {'results':results,'documents':documents[:budget],'truncated':len(rows)>limit or len(documents)>budget,'offset':offset,'limit':limit,
                'freshness':'returned records checked; external additions require docs-index --rebuild',
                'relation_depth':2,'semantic_recall':False}


def add_parsers(sub):
    p=sub.add_parser('work-new',help='save an idea without starting implementation')
    p.add_argument('--title',required=True);p.add_argument('--brief',required=True);p.add_argument('--source',required=True)
    p.add_argument('--module',action='append',default=[])
    p=sub.add_parser('work-show');p.add_argument('work')
    p=sub.add_parser('work-update');p.add_argument('work');p.add_argument('--title');p.add_argument('--summary');p.add_argument('--module',action='append')
    p=sub.add_parser('work-stage');p.add_argument('work');p.add_argument('stage',choices=['idea','design','paused','cancelled','delivered','closed'])
    p.add_argument('--reason',required=True);p.add_argument('--closure-condition')
    p=sub.add_parser('work-link');p.add_argument('work');p.add_argument('target');p.add_argument('--relation',required=True);p.add_argument('--reason',required=True)
    p=sub.add_parser('module-set');p.add_argument('--name',required=True);p.add_argument('--id')
    p.add_argument('--alias',action='append',default=[]);p.add_argument('--path',action='append',default=[]);p.add_argument('--parent',action='append',default=[])
    p.add_argument('--state',choices=['active','retired'])
    p=sub.add_parser('docs-search');p.add_argument('--query',default='');p.add_argument('--module');p.add_argument('--related');p.add_argument('--limit',type=int,default=10);p.add_argument('--offset',type=int,default=0)
    p=sub.add_parser('docs-index');p.add_argument('--rebuild',action='store_true',required=True)
    sub.add_parser('docs-recover')
    p=sub.add_parser('docs-move');p.add_argument('--source',required=True);p.add_argument('--destination',required=True);p.add_argument('--apply',action='store_true')
    sub.add_parser('docs-move-recover')


def handle(project,args):
    b=Governance(project.root);cmd=args.command
    if cmd=='docs-recover': return b.recover()
    if cmd=='docs-index': return b.rebuild()
    if cmd=='work-new': return b.create(args.title,args.brief,args.source,args.module)
    if cmd=='work-show': return b.get(args.work)
    if cmd=='work-update':return b.update(args.work,args.title,args.summary,args.module)
    if cmd=='work-stage':
        if args.closure_condition:
            r=b.get(args.work);r['closure_condition']=redact(args.closure_condition);b._save(r)
        return b.stage(args.work,args.stage,args.reason)
    if cmd=='work-link': return b.link(args.work,args.target,args.relation,args.reason)
    if cmd=='module-set': return b.module(args.name,args.id,args.alias,args.path,args.parent,args.state)
    if cmd=='docs-search': return b.search(args.query,args.module,args.limit,args.offset,args.related)
    raise WorkflowError('DOCUMENT_COMMAND_UNKNOWN')
