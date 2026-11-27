"""Preview-first Markdown relocation with durable recovery and old entry links.

Execution-bound documents are deliberately refused: their approvals and receipts
need the separate documents-adopt protocol, not a generic file organizer.
"""
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote
import uuid

from .document_health import SKIP
from .core import (WorkflowError, atomic, digest, inside, now, read, sha)


def safe(root,rel):
    rel=Path(rel)
    if rel.is_absolute() or '..' in rel.parts: raise WorkflowError('DOCUMENT_PATH_ESCAPE')
    p=root/rel
    if not inside(root,p) or p.is_symlink(): raise WorkflowError('DOCUMENT_PATH_ESCAPE')
    return p


def rewrite(text, old_parent, new_parent, source, destination):
    lines=[];fence=None
    def replace(m):
        raw=m.group(2);value=unquote(raw.strip('<>'))
        if value.startswith(('#','http:','https:','mailto:','data:','app:','plugin:')):return m.group(0)
        parts=re.split('([#?].*)',value,maxsplit=1);target=Path(old_parent/parts[0]).resolve()
        if target==source:target=destination
        # Rebase only moved outgoing links, or incoming links to the moved file.
        if old_parent==new_parent and target!=destination:return m.group(0)
        rel=os.path.relpath(target,new_parent).replace('\\','/')
        if len(parts)>1:rel+=parts[1]
        if ' ' in rel or raw.startswith('<'):rel='<'+rel+'>'
        return m.group(1)+rel+m.group(3)
    for line in text.splitlines(keepends=True):
        m=re.match(r'^\s*(`{3,}|~{3,})',line)
        if m:
            if fence is None:fence=m.group(1)[0]
            elif fence==m.group(1)[0]:fence=None
            lines.append(line);continue
        if fence is None:
            line=re.sub(r'(!?\[[^\]\n]*\]\()(<[^>]+>|[^\s)]+)(\))',replace,line)
        lines.append(line)
    return ''.join(lines)


def move(root,source,destination,apply=False):
    root=Path(root).resolve();src=safe(root,source);dst=safe(root,destination)
    safe(root,'docs')
    if not src.is_file() or src.suffix!='.md' or dst.suffix!='.md':raise WorkflowError('MARKDOWN_SOURCE_REQUIRED')
    if not str(Path(destination).as_posix()).startswith('docs/'):raise WorkflowError('DOCUMENT_DESTINATION_REQUIRED docs/')
    if dst.exists():raise WorkflowError('DOCUMENT_DESTINATION_EXISTS')
    pending=safe(root,'.gsp/document-migration.json')
    if pending.exists():raise WorkflowError('DOCUMENT_MIGRATION_PENDING run docs-move-recover')
    if (root/'.gsp/document-transaction.json').exists():raise WorkflowError('DOCUMENT_TRANSACTION_PENDING')
    protected=set()
    for p in (root/'.gsp/runs').glob('*/state.json'):
        if not inside(root,p):raise WorkflowError('DOCUMENT_STATE_ESCAPE')
        s=read(p)
        if s.get('inflight') or (p.parent/'driver/state.json').exists():raise WorkflowError('DOCUMENT_MIGRATION_BUSY')
        for rel in s.get('documents',{}).values():protected.add(safe(root,rel).resolve())
        folder=s.get('doc_dir','docs/gsp/tasks/'+s['id'])
        for name in ('design.md','plan.md','review.md','delivery.md','decisions.md'):protected.add(safe(root,folder+'/'+name).resolve())
    records = (p for pattern in ('*/*/record.json', '*/*/*/record.json')
               for p in (root/'docs/work').glob(pattern))
    for p in records:
        if not inside(root,p):raise WorkflowError('DOCUMENT_RECORD_ESCAPE')
        for rel in read(p).get('documents',{}).values():protected.add(safe(root,rel).resolve())
    if src in protected or dst in protected:raise WorkflowError('DOCUMENT_BOUND use explicit documents-adopt with approval/evidence reconciliation')
    text=src.read_text(encoding='utf-8-sig')
    changes={dst:rewrite(text,src.parent,dst.parent,src,dst)}
    redirect=os.path.relpath(dst,src.parent).replace('\\','/')
    changes[src]='# 文档已归位\n\n正文正本：['+dst.name+']('+redirect+')。请维护目标文件。\n'
    candidates=list(root.glob('*.md'))
    for folder,dirs,files in os.walk(root/'docs',followlinks=False):
        dirs[:]=[d for d in dirs if d not in SKIP and not (Path(folder)/d).is_symlink() and not (Path(folder)/d).as_posix().endswith('/gsp/reports')]
        candidates.extend(Path(folder)/name for name in files if name.endswith('.md'))
    for p in candidates:
        if p==src or p.is_symlink() or not inside(root,p):continue
        if p.stat().st_size>2_000_000:raise WorkflowError('MIGRATION_CONTENT_LIMIT '+str(p))
        original=p.read_text(encoding='utf-8-sig');updated=rewrite(original,p.parent,p.parent,src,dst)
        if original!=updated:
            if p.resolve() in protected:raise WorkflowError('DOCUMENT_BOUND_REFERENCE '+str(p))
            changes[p]=updated
    ops=[]
    for p,value in changes.items():
        ops.append({'path':p.relative_to(root).as_posix(),'before':digest(p) if p.exists() else None,
                    'backup':p.read_text(encoding='utf-8-sig') if p.exists() else None,'after':sha(value.encode()),'text':value})
    active=root/'.gsp/active.json'
    if active.exists():
        from .project import (Project)
        project=Project(root);state,_,_=project.active_state()
        if state.get('scope',{}).get('mode')=='frozen':
            for op in ops:project.scope_path(state,op['path'])
    result={'applied':False,'source':str(src),'destination':str(dst),'changed_paths':[o['path'] for o in ops],
            'redirect_retained':True,'limits':'inline links only; reference-style/HTML/external consumers require manual review'}
    if apply:
        transaction={'id':uuid.uuid4().hex,'at':now(),'operations':ops}
        atomic(pending,transaction);finish(root,transaction);result['applied']=True
    return result


def finish(root,transaction):
    for op in transaction['operations']:
        if sha(op['text'].encode())!=op['after']:raise WorkflowError('DOCUMENT_MIGRATION_CORRUPT')
        p=safe(root,op['path']);actual=digest(p) if p.exists() else None
        if actual not in (op['before'],op['after']):raise WorkflowError('DOCUMENT_MIGRATION_CONFLICT '+op['path'])
    for op in transaction['operations']:
        p=safe(root,op['path'])
        actual=digest(p) if p.exists() else None
        if actual not in (op['before'],op['after']):raise WorkflowError('DOCUMENT_MIGRATION_CONFLICT '+op['path'])
        if not p.exists() or digest(p)!=op['after']:atomic(p,op['text'])
    atomic(safe(root,'.gsp/document-migrations/'+transaction['id']+'.json'),transaction)
    safe(root,'.gsp/document-migration.json').unlink()


def recover(root):
    root=Path(root).resolve();p=safe(root,'.gsp/document-migration.json')
    if not p.exists():return {'recovered':False}
    t=read(p);finish(root,t);return {'recovered':True,'transaction':t['id']}
