"""Read-only document audit. Reports are evidence, never automatic cleanup."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote
import uuid

from .runtime import WorkflowError, atomic, digest, git_identity, inside, now, read, redact, sha
from .project_assets import is_link, scan_assets
from .routing import route_for

RULE_VERSION=2
SKIP={'.git','.gsp','.gstack','.claude','.agents','node_modules','.venv','venv','target','dist','build','__pycache__'}
ROLES={'product':'current','reference':'reference','howto':'operation','work':'task','research':'research',
       'archive':'historical','ops':'verification','gsp':'workflow','specs':'legacy-spec','reviews':'legacy-review','designs':'legacy-design','reverse':'research'}


def git_route(root):
    """Read-only Git classification and compact next-skill handoff."""
    root = Path(root).resolve()
    def run(*args):
        return subprocess.run(['git', *args], cwd=root, capture_output=True,
                              text=True, encoding='utf-8', errors='replace',
                              timeout=15, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        probe = run('rev-parse', '--show-toplevel')
        if probe.returncode != 0:
            route = route_for('repository_missing')
            return {'status':'not-a-repository','route_id':route.route_id,'findings':[{'code':'GIT_REPOSITORY_MISSING','severity':'blocking','detail':'Git repository is not available'}],
                    'next_skill':route.next_skill,'blocking':route.blocking,'user_decision_required':route.user_decision_required,'decision_kind':route.decision_kind,'handoff':{'reason':'Choose whether to initialize or continue without Git'},'evidence':[]}
        branch = run('branch','--show-current').stdout.strip() or None
        head = run('rev-parse','HEAD').stdout.strip() or None
        porcelain = run('status','--porcelain=v1','-z','--untracked-files=all')
        if porcelain.returncode != 0: raise RuntimeError(porcelain.stderr.strip() or 'git status failed')
        entries=[x for x in porcelain.stdout.split('\0') if x]
        findings=[]
        if not branch:
            findings.append({'code':'GIT_DETACHED_HEAD','severity':'blocking','detail':'HEAD is detached'})
        if entries:
            findings.append({'code':'GIT_DIRTY_WORKTREE','severity':'warning','detail':f'{len(entries)} changed or untracked paths require attribution'})
        active = None
        try:
            active_file=root/'.gsp/active.json'
            if active_file.exists(): active=read(active_file).get('task')
        except (OSError,ValueError,TypeError): pass
        status='clean' if not findings else 'needs-decision'
        if any(f['code']=='GIT_DETACHED_HEAD' for f in findings): route = route_for('detached_head')
        elif entries: route = route_for('dirty_or_untracked')
        else: route = route_for('clean_and_no_active_task' if not active else 'clean_and_active_task')
        return {'status':status,'route_id':route.route_id,'branch':branch,'head':head,'changed_paths':len(entries),'active_task':active,
                'findings':findings,'next_skill':route.next_skill,'blocking':route.blocking,
                'user_decision_required':route.user_decision_required,'decision_kind':route.decision_kind,
                'handoff':{'task':active,'stage':'git-health','next_skill':route.next_skill,'reason':('attribute existing changes' if entries else 'Git baseline is usable')},'evidence':[]}
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        route = route_for('git_probe_unknown')
        return {'status':'unknown','route_id':route.route_id,'findings':[{'code':'GIT_HEALTH_UNKNOWN','severity':'blocking','detail':type(exc).__name__+': '+str(exc)}],
                'next_skill':route.next_skill,'blocking':route.blocking,'user_decision_required':route.user_decision_required,'decision_kind':route.decision_kind,'handoff':{'reason':'Investigate Git health before continuing'},'evidence':[]}


def markdown_links(text):
    """Conservative inline link parser; reference/HTML/anchor validation excluded."""
    visible=[];fence=None
    for line in text.splitlines():
        m=re.match(r'^\s*(`{3,}|~{3,})',line)
        if m:
            if fence is None: fence=m.group(1)[0]
            elif m.group(1)[0]==fence: fence=None
            continue
        if fence is None: visible.append(line)
    clean=re.sub(r'`[^`\n]+`','', '\n'.join(visible))
    return re.findall(r'!?\[[^\]\n]*\]\((<[^>]+>|[^\s)]+)(?:\s+"[^"]*")?\)',clean)


def audit(root, report_dir=None, max_files=100000, max_bytes=2_000_000):
    root=Path(root).resolve()
    if not root.is_dir(): raise WorkflowError('PROJECT_MISSING')
    reports=Path(report_dir).resolve() if report_dir else root/'docs/gsp/reports/document-governance'
    default_reports=root/'docs/gsp/reports/document-governance'
    if reports==root or inside(reports,root): raise WorkflowError('REPORT_DIRECTORY_OVERLAPS_PROJECT')
    if max_files<1 or max_bytes<1: raise WorkflowError('AUDIT_LIMIT_INVALID')
    findings=[]; inventory=[];contents={};hashes=defaultdict(list);referenced=set();records=[]
    def finding(rule,path,detail,severity='WARN',confidence='confirmed',related=None):
        key=rule+'|'+path+'|'+('|'.join(sorted(related or [])))
        findings.append({'id':'DOC-'+sha(key.encode())[:14],'rule':rule,'path':path,'detail':redact(detail),
                         'severity':severity,'confidence':confidence,'related':related or [],'action':'review'})
    def read_doc(p):
        rel=p.relative_to(root).as_posix()
        try:
            if is_link(p) or not inside(root,p):
                finding('symlink-skipped',rel,'Link or escape: content not read',confidence='not-checked');return
            if p.stat().st_size>max_bytes:
                finding('large-document-skipped',rel,'Exceeds per-file content limit',confidence='not-checked');return
            data=p.read_bytes();text=data.decode('utf-8-sig')
        except (OSError,UnicodeError) as exc:
            finding('unreadable-document',rel,type(exc).__name__,severity='ERROR');return
        parts=p.relative_to(root).parts
        role=ROLES.get(parts[1],'unclassified') if len(parts)>2 and parts[0]=='docs' else 'entry'
        if len(parts)>2 and parts[0]=='docs' and parts[1].startswith(('reference-','howto-','explanation-')): role='reference'
        if len(parts)==2 and parts[0]=='docs':
            role='reference' if p.name.startswith(('reference-','howto-','explanation-','tutorial-')) else 'unclassified'
        inventory.append({'path':rel,'bytes':len(data),'sha256':sha(data),'role':role})
        contents[rel]=text;hashes[sha(data)].append(rel)
        if role=='unclassified': finding('classification-review',rel,'Register document role or plan controlled migration',confidence='candidate')
    candidates=list(root.glob('*.md'))
    docs_root=root/'docs'
    if is_link(docs_root) or not inside(root,docs_root):
        finding('symlink-skipped','docs','Document root is a link; not scanned',severity='ERROR',confidence='not-checked')
        walk=[]
    else:walk=os.walk(docs_root,followlinks=False)
    for folder,dirs,files in walk:
        dirs[:]=[d for d in dirs if d not in SKIP and not is_link(Path(folder)/d)
                 and not inside(reports,Path(folder)/d) and not inside(default_reports,Path(folder)/d)]
        for name in files:
            p=Path(folder)/name
            if inside(reports,p) or inside(default_reports,p): continue
            if p.suffix.lower()=='.md': candidates.append(p)
            elif (name=='record.json' and p.relative_to(root).as_posix().startswith('docs/work/')) or (p.parent.as_posix().endswith('product/modules') and p.suffix=='.json'):
                records.append(p)
        if len(candidates)+len(records)>max_files: break
    if len(candidates)+len(records)>max_files:
        finding('scan-truncated','docs','File limit reached: inventory is incomplete',severity='ERROR',confidence='not-checked')
    for p in sorted(candidates)[:max_files]: read_doc(p)
    for group in hashes.values():
        group.sort()
        if len(group)>1: finding('duplicate-content',group[0],'Identical bytes; preserve source meaning before deciding cleanup',confidence='candidate',related=group[1:])
    for rel,text in contents.items():
        for raw in markdown_links(text):
            link=unquote(raw.strip('<>'))
            if not link or link.startswith(('#','http:','https:','mailto:','data:','plugin:','app:')): continue
            path=link.split('#',1)[0].split('?',1)[0]
            if not path: continue
            target=(root/rel).parent/path
            if not inside(root,target):
                finding('external-local-link',rel,'Local reference leaves project; not followed',confidence='not-checked',related=[redact(path)]);continue
            target_rel=target.resolve().relative_to(root).as_posix();referenced.add(target_rel)
            if not target.exists(): finding('broken-link',rel,'Local target does not exist',related=[target_rel])
    ids={};numbers=defaultdict(list)
    for p in records[:max_files]:
        rel=p.relative_to(root).as_posix()
        try:
            if p.is_symlink() or not inside(root,p) or p.stat().st_size>max_bytes: raise ValueError('unsafe/oversized metadata')
            r=json.loads(p.read_text(encoding='utf-8-sig'))
            if r.get('kind') not in ('work','module') or not re.fullmatch('[a-f0-9]{32}',r.get('uuid','')): raise ValueError('invalid identity/kind')
            if r['uuid'] in ids: finding('duplicate-identity',rel,'UUID conflict',severity='ERROR',related=[ids[r['uuid']]])
            ids[r['uuid']]=rel
            if r.get('number'): numbers[r['number']].append(rel)
            for kind,doc in r.get('documents',{}).items():
                target=root/doc
                referenced.add(doc)
                if not inside(root,target) or not target.is_file(): finding('invalid-binding',rel,'Missing/unsafe '+kind+' document',severity='ERROR',related=[doc])
        except (OSError,ValueError,TypeError,AttributeError) as exc:
            finding('invalid-record',rel,type(exc).__name__+': '+str(exc),severity='ERROR')
    for number,paths in numbers.items():
        if len(paths)>1: finding('ambiguous-number',paths[0],number+' requires UUID disambiguation',severity='ERROR',related=paths[1:])
    # Legacy runtime bindings are checked without calling state(), which could
    # invalidate delivery and write into the supposedly read-only target.
    from .runtime import Project
    project=Project(root)
    states=[]
    if inside(root,root/'.gsp/runs'):
        states=list((root/'.gsp/runs').glob('*/state.json'))
    for p in states[:max_files]:
        rel=p.relative_to(root).as_posix()
        try:
            if p.is_symlink() or not inside(root,p) or p.stat().st_size>max_bytes: raise ValueError('unsafe/oversized state')
            s=read(p)
            for kind in ('design','plan'):
                doc=project.document(s,kind);docrel=doc.relative_to(root).as_posix();referenced.add(docrel)
                if not doc.exists():
                    if kind=='plan' and s.get('status')=='design': continue
                    finding('invalid-binding',rel,'Runtime document missing',severity='ERROR',related=[docrel]);continue
                binding=s.get('design_approval' if kind=='design' else 'plan_seal')
                if binding and binding['sha256']!=digest(doc): finding('stale-approval' if kind=='design' else 'stale-plan',rel,'Document differs from bound version',severity='ERROR',related=[docrel])
        except (WorkflowError,OSError,ValueError,KeyError,TypeError) as exc:
            finding('invalid-runtime-state',rel,type(exc).__name__+': '+str(exc),severity='ERROR')
    for item in inventory:
        if item['path'].startswith('docs/') and item['path'] not in referenced:
            finding('unlinked-document',item['path'],'No incoming inline link/binding in scanned scope; may still be a valid entry',confidence='candidate')
    assets, asset_findings, asset_scan = scan_assets(root, reports, max_entries=min(max_files, 10000))
    for item in asset_findings:
        finding(**item)
    ident=git_identity(root)
    git_health=git_route(root)
    tracked=None;status=None
    try:
        flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        r=subprocess.run(['git','ls-files','-z'],cwd=root,capture_output=True,timeout=20,creationflags=flags)
        if r.returncode==0:
            paths=set(r.stdout.decode('utf-8').split('\0'));tracked=sum(i['path'] in paths for i in inventory)
            for item in assets:
                item['git_state'] = 'tracked' if item['path'] in paths else 'untracked'
            ignored=subprocess.run(['git','check-ignore','--stdin','-z'],cwd=root,
                                   input=b''.join(os.fsencode(a['path'])+b'\0' for a in assets),
                                   capture_output=True,timeout=20,creationflags=flags)
            if ignored.returncode in (0,1):
                asset_scan['git_ignore_check']='CHECKED'
                ignored_paths=set(os.fsdecode(ignored.stdout).split('\0'))
                for item in assets:
                    if item['path'] in ignored_paths: item['git_state']='ignored'
            for item in inventory:
                if item['path'] not in paths: finding('untracked-document',item['path'],'Not tracked in this checkout; backup/commit policy needs review',confidence='candidate')
            r=subprocess.run(['git','status','--porcelain','-z','--untracked-files=no'],cwd=root,capture_output=True,timeout=20,creationflags=flags)
            status={'available':r.returncode==0,'tracked_dirty':bool(r.stdout),'raw_paths_stored':False}
    except (OSError,UnicodeError,subprocess.TimeoutExpired): pass
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid.uuid4().hex
    folder=reports/stamp
    previous=None
    if (reports/'latest.json').is_file():
        try:
            pointer=read(reports/'latest.json');prior=reports/pointer['run']/'report.json'
            if inside(reports,prior): previous=read(prior)
        except (WorkflowError,KeyError): pass
    old={f['id'] for f in previous.get('findings',[])} if previous and previous.get('project')==str(root) else set()
    current={f['id'] for f in findings}
    report={'schema':1,'rule_version':RULE_VERSION,'run':stamp,'at':now(),'project':str(root),'git':ident,'git_health':git_health,'route':git_health,'working_tree':status,
            'method':'deterministic read-only scan; not an AI semantic review',
            'inventory':{'markdown_files':len(inventory),'tracked_markdown':tracked,'records':len(records),'run_states':len(states),'roles':dict(Counter(i['role'] for i in inventory)),
                         'assets':len(assets),'root_materials':sum(a['role']=='root-material' for a in assets)},
            'limits':{'files':max_files,'per_file_bytes':max_bytes},'exclusions':sorted(SKIP)+sorted({str(reports),str(default_reports)}),
            'checks':{'structure':'CHECKED','inline-local-links':'CHECKED','byte-duplicates':'CHECKED','bindings':'CHECKED',
                      'semantic-correctness':'NOT_CHECKED','code-conformance':'NOT_CHECKED','reference-links-html-anchors':'NOT_CHECKED','live-hosts':'NOT_CHECKED',
                      'asset-metadata':'CHECKED','static-generators':'PARTIAL','raw-asset-content':'NOT_CHECKED'},
            'assets':assets,'asset_scan':asset_scan,
            'findings':findings,'files':inventory,'integrity_ok':not any(f['severity']=='ERROR' for f in findings),
            'delta':{'previous_run':previous.get('run') if previous else None,'new':sorted(current-old),'resolved':sorted(old-current),'persisting':sorted(current&old)},
            'report_markdown':str(folder/'report.md'),'report_json':str(folder/'report.json')}
    # Unique mkdir reserves this run; never overwrite earlier evidence.
    folder.mkdir(parents=True,exist_ok=False)
    atomic(folder/'report.json',report)
    route_summary = '状态：`{}`；下一技能：`{}`；需要用户决策：`{}`'.format(git_health['status'], git_health['next_skill'], git_health['user_decision_required'])
    lines=['# 项目文档与资料治理检查报告','',f'项目：`{root}`',f'检查：{stamp}；规则版本：{RULE_VERSION}',
           '', '只读程序检查；未自动修复、移动或删除文档。语义正确性与代码符合性未检查。',
           '', '## Git 下一步路由', '', route_summary,
           '', '## 盘点', '', '```json',json.dumps(report['inventory'],ensure_ascii=False,indent=2),'```','',
           '## 发现', '', '| ID | 级别 / 可信度 | 规则 | 位置 | 说明 |','|---|---|---|---|---|']
    clean=lambda s:str(s).replace('|','\\|').replace('\n',' ')
    for f in findings:
        lines.append('| '+' | '.join(map(clean,[f['id'],f['severity']+' / '+f['confidence'],f['rule'],f['path'],f['detail']+' '+', '.join(f['related'])]))+' |')
    lines += ['', '## 项目资料与工作产物', '',
              '仅元数据盘点；角色来自路径/名称提示，需人工确认用途、正本、来源与保留规则。', '',
              '| 位置 | 字节 | 角色提示 | Git 状态 |', '|---|---|---|---|']
    for item in assets:
        lines.append('| '+' | '.join(map(clean,[item['path'],item['bytes'],item['role'],item['git_state']]))+' |')
    lines += ['', '## 资料检查边界', '', '```json', json.dumps(asset_scan,ensure_ascii=False,indent=2), '```']
    lines+=['','## 差异与边界','',f'新增 {len(current-old)}；本次未再发现 {len(old-current)}；持续 {len(current&old)}。未再发现不等于已人工确认修复，范围变化也会影响差异。',
            '', '检查范围、排除项、文件哈希和未检查项见同目录 report.json。疑似冗余、孤立、未追踪均不等于可以删除。']
    atomic(folder/'report.md','\n'.join(lines)+'\n');atomic(reports/'latest.json',{'run':stamp,'project':str(root)})
    return report
