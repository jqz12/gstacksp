"""Ownership-aware, self-contained Codex user hook deployment and rollback."""
import copy
import json
import os
from pathlib import Path
import re
import sys
import uuid

from .core import (WorkflowError, atomic, digest, now, read, sha)
from .workflow_locks import (locked)
from .user_install import UserInstall, publish
from .codex_hooks import EVENTS


class HookInstall:
    def __init__(self, profile=None):
        raw=Path(profile or Path.home()/'.codex')
        if raw.is_symlink() or getattr(raw,'is_junction',lambda:False)(): raise WorkflowError('HOOK_PROFILE_LINK')
        self.root=raw.resolve()
        self.files=UserInstall('codex',self.root)
        self.manifest=self.path('gstacksp/hooks-install.json')
        self.journal=self.path('gstacksp/hooks-transaction.json')

    def path(self, rel):
        return self.files.path(rel)

    def config(self):
        path=self.path('hooks.json')
        try: value=read(path) if path.exists() else {}
        except (OSError,ValueError) as exc: raise WorkflowError('HOOK_CONFIG_INVALID') from exc
        if not isinstance(value,dict) or not isinstance(value.get('hooks',{}),dict):raise WorkflowError('HOOK_CONFIG_INVALID')
        for groups in value.get('hooks',{}).values():
            if not isinstance(groups,list) or any(not isinstance(g,dict) or not isinstance(g.get('hooks'),list) for g in groups):raise WorkflowError('HOOK_CONFIG_INVALID')
        return value

    def previous(self):
        if not self.manifest.exists():return {}
        value=read(self.manifest)
        if not isinstance(value,dict) or value.get('schema')!=1 or value.get('profile')!=str(self.root) or not isinstance(value.get('files'),dict) or not isinstance(value.get('groups'),dict) or not isinstance(value.get('version'),str) or not value['version']:
            raise WorkflowError('HOOK_MANIFEST_INVALID')
        for rel,expected in value['files'].items():
            if not isinstance(rel,str) or not rel.startswith('gstacksp/hooks/') or not re.fullmatch('[a-f0-9]{64}',str(expected)):
                raise WorkflowError('HOOK_MANIFEST_INVALID')
            path=self.path(rel)
            if not path.is_file() or digest(path)!=expected:raise WorkflowError('HOOK_OWNED_FILE_CHANGED '+rel)
        return value

    def payload(self):
        source=Path(__file__).resolve().parents[1]
        entry = source/'scripts/gsp.py'
        resources = source
        if not entry.is_file():
            entry = source/'gsp.py'
            resources = source.parent
        if not entry.is_file():raise WorkflowError('HOOK_SOURCE_PACKAGE_REQUIRED')
        files={'gstacksp/hooks/scripts/gsp.py':entry.read_bytes()}
        files.update({'gstacksp/hooks/gsp/'+p.name:p.read_bytes() for p in (source/'gsp').glob('*.py')})
        for name in ('LICENSE','THIRD_PARTY_NOTICES.md'):
            if (resources/name).is_file(): files['gstacksp/hooks/'+name]=(resources/name).read_bytes()
        # JSON command syntax uses quoted absolute paths; no shell substitutions.
        interpreter=str(Path(sys.executable).resolve())
        script=str(self.root/'gstacksp/hooks/scripts/gsp.py')
        if any(c in interpreter+script for c in ['"','\n','\r','%','!']):raise WorkflowError('HOOK_UNSAFE_COMMAND_PATH')
        if os.name=='nt':
            launcher=self.root/'gstacksp/hooks/launch.ps1'
            escaped_python=interpreter.replace("'","''")
            escaped_script=script.replace("'","''")
            files['gstacksp/hooks/launch.ps1']=(f"& '{escaped_python}' '{escaped_script}' codex-hook\nexit $LASTEXITCODE\n").encode('utf-8-sig')
            command=f'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{launcher}"'
        else:
            import shlex
            command=f'{shlex.quote(interpreter)} {shlex.quote(script)} codex-hook'
        groups={event:{'hooks':[{'type':'command','command':command,'timeout':3 if event=='Interrupt' else 5}]} for event in sorted(EVENTS)}
        from . import __version__
        return files,groups,__version__

    def merge(self, config, previous, groups):
        result=copy.deepcopy(config)
        hooks=result.setdefault('hooks',{})
        for event,old in previous.get('groups',{}).items():
            entries=hooks.get(event,[])
            if entries.count(old)!=1:raise WorkflowError('HOOK_REGISTRATION_CHANGED '+event)
            entries.remove(old)
            if not entries:hooks.pop(event,None)
        # Refuse unowned registrations pointing at our deployment path.
        marker=str(self.root/'gstacksp/hooks/scripts/gsp.py')
        for entries in hooks.values():
            for group in entries:
                if any(any(m in str(h.get('command','')) for m in (marker,str(self.root/'gstacksp/hooks/launch.ps1'))) for h in group['hooks'] if isinstance(h,dict)):
                    raise WorkflowError('HOOK_UNOWNED_REGISTRATION')
        for event,group in groups.items():hooks.setdefault(event,[]).append(group)
        if not hooks and not config.get('hooks'):result.pop('hooks',None)
        return result

    def maintenance(self, previous):
        """Compare owned payloads without changing installation or host trust."""
        command = 'hooks-upgrade'
        try:
            files, groups, version = self.payload()
            old = previous['files']
            changed = sorted(rel for rel, data in files.items() if old.get(rel) != sha(data))
            removed = sorted(old.keys() - files.keys())
            registrations_changed = previous['groups'] != groups
            state = 'current'
            if changed or removed:
                state = 'runtime-update'
                init = 'gstacksp/hooks/gsp/__init__.py'
                if changed == [init] and not removed:
                    normalize = lambda data: re.sub(rb'(?m)^__version__\s*=.*$', b'', data)
                    if normalize(self.path(init).read_bytes()) == normalize(files[init]):
                        state = 'version-label-update'
            elif previous['version'] != version:
                state = 'version-label-update'
            if registrations_changed:
                state = 'registration-update'
            return {'state':state, 'source_version':version, 'changed_files':changed,
                    'removed_files':removed, 'registrations_changed':registrations_changed,
                    'next_command':None if state == 'current' else command,
                    'activation':'NOT_CHECKED: native host verification is separate'}
        except (WorkflowError, OSError, ValueError) as exc:
            return {'state':'source-unavailable', 'error':str(exc),
                    'next_command':'run hooks-status from the current source package'}

    def status(self):
        try:
            previous=self.previous()
            if not previous:return {'installed':False,'integrity_ok':False,'live_host':'NOT_CHECKED','profile':str(self.root),
                                    'maintenance':{'state':'not-installed','next_command':'hooks-install (explicit authorization required)'}}
            config=self.config();self.merge(config,previous,{})
            maintenance = self.maintenance(previous)
            if self.journal.exists():
                maintenance.update(state='recovery-required', next_command='hooks-recover')
            return {'installed':True,'integrity_ok':True,'version':previous['version'],'profile':str(self.root),
                    'events':sorted(previous['groups']),'live_host':'NOT_CHECKED','trust':'inspect /hooks in native Codex',
                    'pending_recovery':self.journal.exists(),
                    'maintenance':maintenance,
                    'execution_support':{'local_orchestration':'requires native hook trust and actual event probe',
                                         'cloud_orchestration':'UNSUPPORTED: local command hooks are not dispatched'},
                    'native_events':'NOT_CHECKED: inspect project hooks-report; integrity is not trigger proof'}
        except (WorkflowError,OSError,ValueError) as exc:
            return {'installed':self.manifest.exists(),'integrity_ok':False,'error':str(exc),'live_host':'NOT_CHECKED',
                    'maintenance':{'state':'repair-required','next_command':'inspect ownership/configuration; hooks-recover if a transaction is pending'}}

    def apply(self, action, dry_run=False):
        if action not in ('install','upgrade','uninstall'):raise WorkflowError('HOOK_ACTION_INVALID')
        if self.journal.exists():raise WorkflowError('HOOK_RECOVERY_REQUIRED')
        config=self.config();previous=self.previous()
        if action!='install' and not previous:raise WorkflowError('HOOK_INSTALL_MISSING')
        files,groups,version=({}, {}, previous.get('version')) if action=='uninstall' else self.payload()
        merged=self.merge(config,previous,groups)
        if action=='uninstall' and not merged.get('hooks') and not previous.get('original_hooks_key',True):
            merged.pop('hooks',None)
        old=previous.get('files',{})
        for rel in files:
            if self.path(rel).exists() and rel not in old:raise WorkflowError('HOOK_UNOWNED_FILE '+rel)
        changes={rel:files.get(rel) for rel in set(old)|set(files)}
        original_config=self.path('hooks.json').read_bytes() if self.path('hooks.json').exists() else None
        changes['hooks.json']=(json.dumps(merged,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
        if action=='uninstall' and not merged and not previous.get('original_config_exists',True):
            changes['hooks.json']=None
        # Avoid rewriting an unchanged config (including user formatting).
        if merged==config:changes['hooks.json']=original_config
        record={'schema':1,'profile':str(self.root),'version':version,'at':now(),'groups':groups,'files':{r:sha(b) for r,b in files.items()},
                'original_config_exists':previous.get('original_config_exists',original_config is not None),
                'original_hooks_key':previous.get('original_hooks_key','hooks' in config)}
        changes['gstacksp/hooks-install.json']=None if action=='uninstall' else (json.dumps(record,ensure_ascii=False,indent=2)+'\n').encode()
        before={rel:self.path(rel).read_bytes() if self.path(rel).exists() else None for rel in changes}
        result={'action':action,'scope':'codex-user','profile':str(self.root),'version':version,'events':sorted(groups),
                'dry_run':dry_run,'trust':'user must review new/changed hooks in native Codex /hooks','live_host':'NOT_CHECKED'}
        if dry_run:return result
        self.root.mkdir(parents=True,exist_ok=True)
        with locked(self.root):
            if self.journal.exists():raise WorkflowError('HOOK_RECOVERY_REQUIRED')
            if self.config()!=config or self.previous()!=previous:raise WorkflowError('HOOK_CHANGED_RETRY')
            for rel,data in before.items():
                if (self.path(rel).read_bytes() if self.path(rel).exists() else None)!=data:raise WorkflowError('HOOK_CHANGED_RETRY')
            backup='gstacksp/hook-backups/'+uuid.uuid4().hex
            rows=[]
            for rel in sorted(changes,key=lambda r:r=='gstacksp/hooks-install.json'):
                if before[rel]==changes[rel]:continue
                n=str(len(rows))
                if before[rel] is not None:
                    target=self.path(backup+'/'+n);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(before[rel])
                rows.append({'path':rel,'before':sha(before[rel]) if before[rel] is not None else None,
                             'after':sha(changes[rel]) if changes[rel] is not None else None,'backup':n})
            from .hosts import identity
            journal={'schema':1,'profile':str(self.root),'backup':backup,'rows':rows,'at':now(),'owner':identity(os.getpid())}
            atomic(self.path(backup+'/operation.json'),journal)
            atomic(self.journal,journal)
            for row in rows:
                path=self.path(row['path'])
                if (digest(path) if path.exists() else None)!=row['before']:raise WorkflowError('HOOK_PUBLICATION_CONFLICT recover before retry')
                publish(path,changes[row['path']])
            self.journal.unlink()
        return {**result,'backup':str(self.root/backup)}

    def recover(self,dry_run=False):
        if not self.journal.exists():raise WorkflowError('HOOK_NO_TRANSACTION')
        def prepare():
            j=read(self.journal)
            if not isinstance(j,dict) or j.get('schema')!=1 or j.get('profile')!=str(self.root) or not re.fullmatch('gstacksp/hook-backups/[a-f0-9]{32}',str(j.get('backup',''))) or not isinstance(j.get('rows'),list):
                raise WorkflowError('HOOK_RECOVERY_INVALID')
            rows=[];seen=set()
            for row in j['rows']:
                rel=row.get('path')
                if not isinstance(rel,str) or rel in seen or not (rel in ('hooks.json','gstacksp/hooks-install.json') or rel.startswith('gstacksp/hooks/')):raise WorkflowError('HOOK_RECOVERY_INVALID')
                seen.add(rel)
                if any(v is not None and not re.fullmatch('[a-f0-9]{64}',str(v)) for v in (row.get('before'),row.get('after'))):raise WorkflowError('HOOK_RECOVERY_INVALID')
                path=self.path(rel);current=digest(path) if path.exists() else None
                if current not in (row['before'],row['after']):raise WorkflowError('HOOK_RECOVERY_CONFLICT '+rel)
                if not re.fullmatch('[0-9]+',str(row.get('backup'))):raise WorkflowError('HOOK_RECOVERY_INVALID')
                data=self.path(j['backup']+'/'+row['backup']).read_bytes() if row['before'] is not None else None
                if data is not None and sha(data)!=row['before']:raise WorkflowError('HOOK_BACKUP_CORRUPT')
                rows.append((path,data,current))
            return rows
        if dry_run:prepare();return {'dry_run':True,'recovered':False}
        prepare()
        lock=self.path('.gsp/writer.lock')
        if lock.exists():
            from .hosts import process_token
            owner=read(self.journal).get('owner',{})
            if not isinstance(owner.get('pid'),int) or read(lock).get('pid')!=owner['pid'] or process_token(owner['pid']) is not None:
                raise WorkflowError('HOOK_RECOVERY_OWNER_ACTIVE_OR_UNKNOWN')
            lock.unlink()
        with locked(self.root):
            rows=prepare()
            for path,data,expected in reversed(rows):
                if (digest(path) if path.exists() else None)!=expected:raise WorkflowError('HOOK_RECOVERY_CONFLICT')
                publish(path,data)
            self.journal.unlink()
        return {'recovered':True,'action':'rollback','live_host':'NOT_CHECKED'}
