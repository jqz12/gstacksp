"""Native Codex protocol adapter. Observations never update workflow state."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

from .runtime import (Project, WorkflowError, atomic, find_project_root, inside,
                      now, read, slug)

INPUT_LIMIT = 1024 * 1024
SESSION_LIMIT = 32 * 1024 * 1024
SHARD_LIMIT = 4 * 1024 * 1024
REPORT_SESSIONS = 1000
REPORT_SHARDS = 10
WAIT_GUARD_LIMIT = 2
WAIT_GUARD_TTL = 3600
WAIT_GUARD_ENTRIES = 128
WAIT_ALIASES = {'Bash', 'exec_command'}
EVENTS = {'SessionStart','UserPromptSubmit','PreToolUse','PostToolUse','Stop','Interrupt','SessionEnd'}


def key(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:24]


def safe_file(root, path):
    """Refuse filesystem redirections for audit and metadata writes."""
    path = Path(path)
    if not inside(root,path):
        raise WorkflowError('HOOK_PATH_ESCAPE')
    for item in [path, *path.parents]:
        if item == root: break
        if item.is_symlink() or getattr(item,'is_junction',lambda:False)():
            raise WorkflowError('HOOK_LINK_REFUSED')
    return path


def load(path):
    if path.stat().st_size > INPUT_LIMIT:
        raise WorkflowError('HOOK_METADATA_TOO_LARGE')
    value=read(path)
    if not isinstance(value,dict): raise WorkflowError('HOOK_METADATA_INVALID')
    return value


@contextmanager
def audit_lock(path):
    """OS lock released on process exit; never steal stale writer.lock."""
    with path.open('a+b') as stream:
        deadline=time.monotonic()+1.0
        while True:
            try:
                stream.seek(0)
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic()>=deadline: raise WorkflowError('HOOK_AUDIT_BUSY')
                time.sleep(.005)
        try: yield
        finally:
            stream.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def audit(root, payload, task, outcome, coverage, stage=None):
    # Only metadata; do not hash/store prompt/command/result contents either.
    session=key(payload.get('session_id','missing'))
    folder=safe_file(root,root/'.gsp/audits/codex-hooks'/session)
    folder.mkdir(parents=True,exist_ok=True)
    from . import __version__
    row={'schema':1,'event_id':uuid.uuid4().hex,'at':now(),'host':'codex','gsp_version':__version__,
         'session':session,'turn':key(payload.get('turn_id','missing')),
         'agent':key(payload.get('agent_id','unknown')),'task':task,'stage':stage,
         'event':payload['hook_event_name'],'tool':re.sub(r'[^A-Za-z0-9_.:-]','?',str(payload.get('tool_name','')))[:100],
         'call':key(payload.get('tool_use_id','missing')),'call_known':isinstance(payload.get('tool_use_id'),str) and bool(payload['tool_use_id']),
         'outcome':outcome,'coverage':coverage}
    data=(json.dumps(row,ensure_ascii=True,separators=(',',':'))+'\n').encode()
    with audit_lock(safe_file(root,folder/'audit.lock')):
        status_path=safe_file(root,folder/'coverage.json')
        status=load(status_path) if status_path.exists() else {'schema':1,'bytes':0,'shard':1,'truncated':False}
        # Recover accounting if the process died after append but before metadata.
        shards=sorted(folder.glob('events-*.jsonl'))
        if len(shards)>10:raise WorkflowError('HOOK_AUDIT_SHARD_LIMIT')
        actual=sum(safe_file(root,p).stat().st_size for p in shards)
        if actual!=status['bytes']:status['accounting_recovered']=True
        status['bytes']=actual
        if shards:
            status['shard']=max(int(p.stem.split('-')[1]) for p in shards)
        if status['bytes']+len(data)>SESSION_LIMIT:
            status['truncated']=True
        else:
            file=safe_file(root,folder/f'events-{status["shard"]:06d}.jsonl')
            if file.exists() and file.stat().st_size+len(data)>SHARD_LIMIT:
                status['shard']+=1
                file=safe_file(root,folder/f'events-{status["shard"]:06d}.jsonl')
            with file.open('ab') as stream:
                stream.write(data);stream.flush()
            status['bytes']+=len(data)
        status['last_event']=payload['hook_event_name']
        status['coverage']='partial-native-events; shell-not-enforced; missing-result=unknown'
        atomic(status_path,status)
    return status['truncated']


def patch_paths(command):
    if not isinstance(command,str) or len(command.encode('utf-8'))>INPUT_LIMIT:
        raise WorkflowError('HOOK_PATCH_INVALID')
    lines=command.strip().splitlines()
    if len(lines)<3 or lines[0]!='*** Begin Patch' or lines[-1]!='*** End Patch':
        raise WorkflowError('HOOK_PATCH_INVALID')
    paths=[]; operation=None
    for line in lines[1:-1]:
        match=re.fullmatch(r'\*\*\* (Add File|Update File|Delete File|Move to): (.+)',line)
        if match:
            kind,path=match.groups()
            if kind=='Move to' and operation!='Update File': raise WorkflowError('HOOK_PATCH_INVALID_MOVE')
            if not path or '\x00' in path or '\r' in path: raise WorkflowError('HOOK_PATCH_INVALID_PATH')
            paths.append(path)
            if kind!='Move to': operation=kind
        elif line.startswith('*** ') and line!='*** End of File':
            raise WorkflowError('HOOK_PATCH_UNKNOWN_DIRECTIVE')
    if not paths: raise WorkflowError('HOOK_PATCH_NO_TARGETS')
    return paths


def locate(cwd):
    if not isinstance(cwd,str) or not Path(cwd).is_absolute(): return None
    root=find_project_root(Path(cwd))
    if root is not None:
        safe_file(root,root/'.gsp/project.json')
    return root


def deny(reason):
    return {'hookSpecificOutput':{'hookEventName':'PreToolUse','permissionDecision':'deny','permissionDecisionReason':reason}}


def edit_targets(root, cwd, tool, payload):
    data=payload.get('tool_input')
    if not isinstance(data,dict): raise WorkflowError('HOOK_EDIT_INPUT_INVALID')
    if tool=='apply_patch': raw=patch_paths(data.get('command'))
    else: raw=[data.get('file_path') or data.get('notebook_path')]
    targets=[]
    for path in raw:
        if not isinstance(path,str) or not path: raise WorkflowError('HOOK_EDIT_PATH_REQUIRED')
        targets.append(str((Path(cwd)/path).resolve()))
    return targets


def edit_task(root, cwd, targets, fallback):
    from .git_topology import bound_task_for_path
    owners={bound_task_for_path(root,Path(path)) for path in targets}
    owners.discard(None)
    if len(owners)>1: raise WorkflowError('GIT_EDIT_TARGET_CONFLICT multiple tasks claim patch targets')
    return next(iter(owners),None) or fallback


def wait_input(payload):
    """Return a tool_input mapping without trusting a host-specific wrapper."""
    data=payload.get('tool_input')
    if isinstance(data,str):
        try:
            data=json.loads(data)
        except (TypeError,ValueError):
            return None
    return data if isinstance(data,dict) else None


def wait_mode(tool,payload):
    """Classify a PreToolUse payload as empty wait, input, or other.

    Some Codex hosts expose the write_stdin tool as Bash or exec_command while
    preserving the wait fields. Only wait-shaped alias payloads count; a real
    shell command remains an observation and resets the streak.
    """
    data=wait_input(payload)
    if not isinstance(data,dict):
        return 'invalid',None
    session=data.get('session_id')
    valid_session=(isinstance(session,(str,int)) and not isinstance(session,bool)
                   and str(session)!='')
    if tool=='write_stdin':
        if not valid_session:
            return 'invalid',None
        return ('wait' if data.get('chars','')=='' else 'input'),str(session)
    if tool in WAIT_ALIASES:
        if 'command' in data or 'cmd' in data:
            return 'other',None
        if not valid_session or 'chars' not in data:
            return 'other',None
        if not any(key in data for key in ('yield_time_ms','max_output_tokens')):
            return 'other',None
        return ('wait' if data.get('chars')=='' else 'input'),str(session)
    return 'other',None


def wait_guard(root, payload, event, tool):
    session=key(payload.get('session_id','missing'))
    folder=safe_file(root,root/'.gsp/audits/codex-hooks'/session)
    folder.mkdir(parents=True,exist_ok=True)
    path=safe_file(root,folder/'wait-guard.json')
    lock=safe_file(root,folder/'audit.lock')
    now_value=time.time()
    degraded=False
    blocked=False
    with audit_lock(lock):
        try:
            state=load(path) if path.exists() else {'schema':1,'entries':{}}
            if state.get('schema')!=1 or not isinstance(state.get('entries'),dict):
                raise WorkflowError('WAIT_GUARD_STATE_INVALID')
            entries={k:v for k,v in state['entries'].items()
                     if isinstance(v,dict) and isinstance(v.get('last_seen'),(int,float))
                     and now_value-float(v['last_seen'])<=WAIT_GUARD_TTL}
            if event=='PreToolUse':
                mode,tool_session=wait_mode(tool,payload)
                if mode=='wait' and tool_session:
                    tool_key=key(tool_session)
                    previous=entries.get(tool_key,{})
                    count=int(previous.get('streak',0))+1
                    entries[tool_key]={'streak':count,'last_seen':now_value}
                    blocked=count>WAIT_GUARD_LIMIT
                elif mode in ('input','other'):
                    entries={}
            if len(entries)>WAIT_GUARD_ENTRIES:
                entries=dict(sorted(entries.items(),key=lambda item:item[1].get('last_seen',0),reverse=True)[:WAIT_GUARD_ENTRIES])
            atomic(path,{'schema':1,'entries':entries,'updated':now_value})
        except (OSError,ValueError,TypeError,KeyError,WorkflowError):
            degraded=True
    return blocked,degraded


def handle(payload):
    if not isinstance(payload,dict) or payload.get('hook_event_name') not in EVENTS: return {}
    event=payload['hook_event_name']; tool=payload.get('tool_name')
    writing=event=='PreToolUse' and tool in ('apply_patch','Edit','Write','NotebookEdit')
    try:
        root=locate(payload.get('cwd'))
        if root is None: return {}
        identity=load(root/'.gsp/project.json')
        if identity.get('root',str(root))!=str(root): raise WorkflowError('HOOK_PROJECT_MOVED')
        state=None;task=None;absolute_paths=[]
        active=safe_file(root,root/'.gsp/active.json')
        from .git_topology import bound_task_for_path
        task = bound_task_for_path(root, Path(payload['cwd']))
        if writing:
            absolute_paths=edit_targets(root,payload['cwd'],tool,payload)
            task=edit_task(root,payload['cwd'],absolute_paths,task)
        if task or active.exists():
            task=task or load(active).get('task');slug(task)
            state=load(safe_file(root,root/'.gsp/runs'/task/'state.json'))
            if state.get('id')!=task: raise WorkflowError('HOOK_TASK_MISMATCH')
        result={};outcome='observed';coverage='metadata-only'
        guard_blocked,guard_degraded=wait_guard(root,payload,event,tool)
        if guard_degraded:
            result['systemMessage']='GSP wait guard state unavailable; wait coverage NOT_CHECKED.'
        if guard_blocked:
            result.update(deny('GSP wait guard blocked the third empty wait for this tool session. Inspect the log, process, exit code or artifact before waiting again.'))
            outcome='denied';coverage='wait-guard'
        if event=='SessionStart':
            context='GstackSP: read gsp-workflow and shared contract. '
            if state:
                context+=f'Active task {task}, status {state.get("status")}. Inspect status/resume before editing; do not silently overwrite interrupted work. '
                context+='Bound documents: '+json.dumps({k:str(Project(root).document(state,k).relative_to(root)) for k in ('design','plan')},ensure_ascii=True)
            else: context+='No active task. Discuss design before implementation.'
            result={'hookSpecificOutput':{'hookEventName':event,'additionalContext':context[:1800]}}
        if not guard_blocked and writing and state and (state.get('scope',{}).get('mode')=='frozen' or
                                  (state.get('git_topology') or {}).get('enforce') or
                                  (state.get('git_topology') or {}).get('intent') == 'cross-session'):
            try:
                data=payload.get('tool_input')
                if not isinstance(data,dict): raise WorkflowError('HOOK_EDIT_INPUT_INVALID')
                project=Project(root)
                for absolute in absolute_paths: project.scope_path(state, absolute)
                if (state.get('git_topology') or {}).get('intent') == 'cross-session':
                    from .sessions import authorize_edit
                    token=(payload.get('gsp_session_id') or os.environ.get('GSP_SESSION_ID')
                           or payload.get('session_id'))
                    authorize_edit(project, state, token, absolute_paths)
                    coverage='session-direct-edit-paths'
                else:
                    coverage='direct-edit-paths'
                outcome='allowed'
            except (WorkflowError,ValueError,TypeError,OSError) as exc:
                outcome='denied';coverage='direct-edit-paths'
                reason=str(exc)
                if reason.startswith(('SESSION_', 'CONTROLLER_', 'WORKSPACE_')):
                    result=deny('GSP session edit refused: '+reason)
                else:
                    result=deny('GSP frozen edit refused: outside scope or unparseable edit. Inspect gsp scope/check-path; no scope was changed.')
        elif not guard_blocked and tool in ('Bash','exec_command','write_stdin'):
            coverage='shell-not-enforced'
        try:
            stage=state.get('status') if state else None
            if stage not in ('design','plan','execute','review','ready','blocked','needs-verification'):stage='unknown'
            truncated=audit(root,payload,task,outcome,coverage,stage)
            if truncated: result.setdefault('systemMessage','GSP audit storage limit reached; process record is incomplete.')
        except (OSError,WorkflowError,ValueError,KeyError,TypeError):
            result.setdefault('systemMessage','GSP audit unavailable; process coverage is degraded. Task state unchanged.')
        return result
    except (WorkflowError,ValueError,TypeError,KeyError,OSError,AttributeError):
        return deny('GSP metadata invalid; inspect project state before editing.') if writing else {'systemMessage':'GSP hook could not inspect project; coverage NOT_CHECKED.'}


def run():
    try:
        raw=sys.stdin.buffer.read(INPUT_LIMIT+1)
        if len(raw)>INPUT_LIMIT:
            return {'systemMessage':'GSP hook input too large; project and edit coverage NOT_CHECKED.'}
        return handle(json.loads(raw))
    except (ValueError,UnicodeError,OSError,RecursionError):
        return {'systemMessage':'GSP hook input invalid; project and edit coverage NOT_CHECKED.'}


def report(root):
    """Bounded offline metadata assessment, never an assertion of task success."""
    root=Path(root).resolve()
    folder=safe_file(root,root/'.gsp/audits/codex-hooks')
    result={'schema':1,'audit_events_observed':False,'native_host_trigger':'NOT_CHECKED','coverage_state':'not_observed','sessions':0,'sessions_skipped':0,'shards_skipped':0,'events':0,'attempts':0,'results_observed':0,
            'unknown_attempts':0,'denied':0,'wait_guard_denied':0,'replayed_events':0,'incomplete':False,
            'assessment':'metadata only; does not prove skill compliance or business correctness',
            'coverage_gaps':['shell-not-enforced','specialized/hosted tool paths','missing-result=unknown']}
    if not folder.exists():return result
    sessions=[p for p in sorted(folder.iterdir()) if p.is_dir() and re.fullmatch('[a-f0-9]{24}',p.name)]
    result['sessions_skipped']=max(0,len(sessions)-REPORT_SESSIONS)
    result['incomplete']=bool(result['sessions_skipped'])
    for session in sessions[:REPORT_SESSIONS]:
        safe_file(root,session)
        result['sessions']+=1
        seen=set();attempts={};responses=set();size=0
        coverage=safe_file(root,session/'coverage.json')
        try:
            result['incomplete'] |= load(coverage).get('truncated',False)
            shards=sorted(session.glob('events-*.jsonl'))
            skipped=max(0,len(shards)-REPORT_SHARDS)
            result['shards_skipped']+=skipped
            result['incomplete'] |= bool(skipped)
            for path in shards[:REPORT_SHARDS]:
                safe_file(root,path)
                size+=path.stat().st_size
                if size>SESSION_LIMIT:result['incomplete']=True;break
                with path.open(encoding='utf-8') as stream:
                    for line in stream:
                        row=json.loads(line)
                        if not isinstance(row,dict):raise ValueError('invalid event')
                        event=row.get('event');call=(row.get('turn'),row.get('agent'),row.get('call'))
                        if not row.get('call_known') and event in ('PreToolUse','PostToolUse'):
                            call=(row.get('turn'),row.get('agent'),row.get('event_id'))
                            result['incomplete']=True
                        ident=(event,*call)
                        if event in ('PreToolUse','PostToolUse'):
                            if ident in seen:result['replayed_events']+=1;continue
                            seen.add(ident)
                        result['events']+=1
                        if row.get('coverage')=='wait-guard' and row.get('outcome')=='denied':
                            result['wait_guard_denied']+=1
                        if event=='PreToolUse':
                            attempts[call]=row.get('outcome')
                            result['denied']+=row.get('outcome')=='denied'
                        elif event=='PostToolUse':responses.add(call)
            result['attempts']+=len(attempts)
            result['results_observed']+=len(responses & attempts.keys())
            result['unknown_attempts']+=sum(c not in responses and outcome!='denied' for c,outcome in attempts.items())
        except (OSError,ValueError,KeyError,TypeError,WorkflowError):result['incomplete']=True
    result['audit_events_observed']=result['events']>0
    result['coverage_state']='partial' if result['events'] else 'not_observed'
    return result
