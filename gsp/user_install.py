"""Explicit user-profile maintenance with ownership, backups and rollback journal."""
from pathlib import Path
import os
import re
import tempfile
import uuid

from .runtime import WorkflowError, atomic, digest, inside, locked, now, read, sha


def publish(path, data):
    if data is None:
        Path(path).unlink(missing_ok=True)
    else:
        atomic(path, data.decode('utf-8'))


class UserInstall:
    def __init__(self, host, profile=None):
        if host not in ('codex','claude'): raise WorkflowError('USER_HOST_REQUIRED choose one host')
        self.host = host
        self.root = Path(profile or Path.home()/('.codex' if host == 'codex' else '.claude')).resolve()
        self.manifest = self.root/'gstacksp/install.json'
        self.journal = self.root/'gstacksp/transaction.json'
        self.instruction = self.root/('AGENTS.md' if host == 'codex' else 'CLAUDE.md')
        self.scope = host+'-user'

    def path(self, rel):
        path = self.root/rel
        if not isinstance(rel, str) or Path(rel).is_absolute() or not inside(self.root, path) or '..' in Path(rel).parts:
            raise WorkflowError('INSTALL_ESCAPE ' + str(rel))
        if any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)() for p in [path, *path.parents] if p != self.root and inside(self.root,p)):
            raise WorkflowError('INSTALL_LINK_REFUSED ' + rel)
        for parent in path.parents:
            if parent == self.root: break
            if parent.exists() and not parent.is_dir(): raise WorkflowError('INSTALL_CONFLICT parent: ' + str(parent))
        if path.exists() and not path.is_file(): raise WorkflowError('INSTALL_CONFLICT not a file: '+rel)
        return path

    def skill_path(self, rel):
        if not isinstance(rel, str) or not re.fullmatch(r'skills/gsp-[A-Za-z0-9_-]+/.+', rel):
            raise WorkflowError('INSTALL_MANIFEST_INVALID ' + str(rel))
        return self.path(rel)

    def routing(self, previous, remove=False):
        # Preserve the user's BOM and CRLF bytes outside the owned marker block.
        existing = self.instruction.read_bytes().decode('utf-8') if self.instruction.exists() else ''
        begin, end = f'<!-- gstacksp:{self.scope}:begin -->', f'<!-- gstacksp:{self.scope}:end -->'
        if begin in existing or end in existing:
            if existing.count(begin) != 1 or existing.count(end) != 1 or existing.index(end) < existing.index(begin):
                raise WorkflowError('INSTRUCTION_MARKERS_INVALID')
            a, b = existing.index(begin), existing.index(end)+len(end)
            block = existing[a:b]
            expected = previous.get('instruction_block_sha256')
            # Compatibility with the previously hand-managed Codex manifest.
            valid = sha(block.encode()) == expected if expected else digest(self.instruction) == previous.get('instruction_sha256')
            if not valid: raise WorkflowError('INSTALL_CONFLICT edited or unowned instruction block')
            return (existing[:a]+existing[b:] if remove else existing), sha(block.encode())
        if previous: raise WorkflowError('INSTALL_CONFLICT owned instruction block missing')
        block = (begin+'\n## GstackSP\n\nFor software development use GstackSP by default. First read `'+str(self.root/'skills/gsp-workflow/SKILL.md')+'` and its shared contract. Load only relevant stages. User instructions take precedence. Do not alter unrelated host configuration.\n'+end)
        return existing+ ('\n\n' if existing and not existing.endswith('\n\n') else '')+block+'\n', sha(block.encode())

    def payload(self, optional, previous):
        from .install import install
        source = Path(__file__).resolve().parents[1]
        if not (source/'skills').is_dir(): raise WorkflowError('SOURCE_PACKAGE_REQUIRED upgrade/install from source package')
        catalog = read(source/'catalog.json')
        previous_optional = [s['name'] for s in catalog['skills'] if s['pack']=='optional' and s['name'] in previous.get('skills',[])]
        with tempfile.TemporaryDirectory(prefix='gsp-user-stage-') as folder:
            stage = Path(folder)
            install(stage, self.host, list(set(optional or []) | set(previous_optional)))
            base = stage/('.agents/skills' if self.host=='codex' else '.claude/skills')
            files = {'skills/'+p.relative_to(base).as_posix():p.read_bytes() for p in base.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
            skills = sorted(p.name for p in base.iterdir() if p.is_dir())
        return files, skills, catalog['version']

    def apply(self, action, optional=None, dry_run=False):
        if action not in ('install','upgrade','uninstall'): raise WorkflowError('INSTALL_ACTION_INVALID')
        self.path('gstacksp/install.json'); self.path('gstacksp/transaction.json')
        if self.journal.exists(): raise WorkflowError('INSTALL_RECOVERY_REQUIRED run install-recover for this profile')
        if self.root.exists() and not self.root.is_dir(): raise WorkflowError('PROFILE_NOT_DIRECTORY')
        previous = read(self.manifest) if self.manifest.exists() else {}
        if action != 'install' and not previous: raise WorkflowError('USER_INSTALL_MISSING')
        old = previous.get('files', {})
        if previous and (previous.get('scope') != self.scope or not isinstance(old,dict) or not old): raise WorkflowError('INSTALL_MANIFEST_INVALID')
        for rel, expected in old.items():
            file = self.skill_path(rel)
            if not file.is_file() or digest(file) != expected: raise WorkflowError('INSTALL_CONFLICT edited or missing file: '+rel)
        self.path(self.instruction.name)
        self.path('gstacksp/install.json'); self.path('gstacksp/transaction.json')
        instruction_before = self.instruction.read_bytes() if self.instruction.exists() else None
        routing, block_hash = self.routing(previous, remove=action=='uninstall')
        files, skills, version = ({}, [], previous.get('version')) if action=='uninstall' else self.payload(optional,previous)
        for rel in files:
            file = self.skill_path(rel)
            if file.exists() and rel not in old: raise WorkflowError('INSTALL_CONFLICT unowned destination: '+rel)
        changes = {rel:files.get(rel) for rel in set(old)|set(files)}
        changes[self.instruction.name] = routing.encode('utf-8')
        manifest_rel = 'gstacksp/install.json'
        import json
        manifest = {'schema':2,'scope':self.scope,'host':self.host,'source':str(Path(__file__).resolve().parents[1]),'version':version,'installed_at':previous.get('installed_at',now()),'updated_at':now(),'skills':skills,'files':{rel:sha(data) for rel,data in files.items()},'instruction_block_sha256':block_hash,'live_discovery':'NOT_CHECKED'}
        changes[manifest_rel] = None if action=='uninstall' else (json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode()
        result = {'action':action,'scope':self.scope,'profile':str(self.root),'version':version,'files':len(files),'dry_run':dry_run,'paths':sorted(changes),'live_discovery':'NOT_CHECKED'}
        if self.host == 'codex' and action in ('install','upgrade'):
            from .codex_hook_install import HookInstall
            result['hooks'] = HookInstall(self.root).status()
        if dry_run: return result
        self.root.mkdir(parents=True, exist_ok=True)
        with locked(self.root):
            # Preflight again under the profile writer lock before publication.
            if self.journal.exists(): raise WorkflowError('INSTALL_RECOVERY_REQUIRED')
            if (read(self.manifest) if self.manifest.exists() else {}) != previous: raise WorkflowError('INSTALL_CHANGED_RETRY')
            if (self.instruction.read_bytes() if self.instruction.exists() else None) != instruction_before:
                raise WorkflowError('INSTALL_CHANGED_RETRY instruction changed during preflight')
            for rel, expected in old.items():
                if not self.skill_path(rel).is_file() or digest(self.skill_path(rel)) != expected: raise WorkflowError('INSTALL_CHANGED_RETRY')
            for rel in files.keys()-old.keys():
                if self.skill_path(rel).exists(): raise WorkflowError('INSTALL_CHANGED_RETRY new destination appeared')
            backup_rel = 'gstacksp/backups/'+uuid.uuid4().hex
            backup = self.path(backup_rel+'/operation.json').parent
            if not inside(self.root,backup): raise WorkflowError('INSTALL_ESCAPE backup')
            records = []
            for rel,data in changes.items():
                target = self.path(rel)
                before = target.read_bytes() if target.exists() else None
                if before == data: continue
                index = str(len(records))
                if before is not None:
                    backup.mkdir(parents=True, exist_ok=True)
                    (backup/index).write_bytes(before)
                records.append({'path':rel,'before':sha(before) if before is not None else None,'after':sha(data) if data is not None else None,'backup':index})
            from .hosts import identity
            journal = {'schema':1,'scope':self.scope,'profile':str(self.root),'action':action,'backup':backup_rel,'changes':records,'at':now(),'owner':identity(os.getpid())}
            # Backup mapping must exist before any target changes. A crash after
            # the commit point must not turn a complete install into a failure.
            atomic(backup/'operation.json',journal)
            atomic(self.journal,journal)
            try:
                for row in records: publish(self.path(row['path']),changes[row['path']])
                self.journal.unlink()
            except Exception:
                self._rollback(journal)
                raise
            return {**result,'backup':str(backup)}

    def _rollback(self, journal, apply=True):
        if journal.get('schema') != 1 or journal.get('profile') != str(self.root) or journal.get('scope') != self.scope or not isinstance(journal.get('changes'),list): raise WorkflowError('INSTALL_RECOVERY_INVALID')
        rel = journal.get('backup','')
        if not re.fullmatch(r'gstacksp/backups/[a-f0-9]{32}',rel): raise WorkflowError('INSTALL_RECOVERY_INVALID')
        restore, seen = [], set()
        for row in journal['changes']:
            if not isinstance(row,dict) or not all(k in row for k in ('path','before','after','backup')):
                raise WorkflowError('INSTALL_RECOVERY_INVALID')
            if not isinstance(row['path'],str) or row['path'] in seen or any(v is not None and (not isinstance(v,str) or not re.fullmatch(r'[a-f0-9]{64}',v)) for v in (row['before'],row['after'])):
                raise WorkflowError('INSTALL_RECOVERY_INVALID')
            path_rel = row['path']
            seen.add(path_rel)
            if path_rel not in (self.instruction.name,'gstacksp/install.json'): self.skill_path(path_rel)
            path = self.path(path_rel)
            current = digest(path) if path.exists() else None
            if current not in (row['before'],row['after']): raise WorkflowError('INSTALL_RECOVERY_CONFLICT user changed '+path_rel)
            if not str(row['backup']).isdigit(): raise WorkflowError('INSTALL_RECOVERY_INVALID')
            copy = self.path(rel+'/'+row['backup'])
            data = copy.read_bytes() if row['before'] is not None else None
            if data is not None and sha(data) != row['before']: raise WorkflowError('INSTALL_BACKUP_CORRUPT')
            restore.append((path,data))
        if apply:
            for path,data in reversed(restore): publish(path,data)
            self.journal.unlink()

    def recover(self, dry_run=False):
        self.path('gstacksp/transaction.json')
        if not self.journal.is_file(): raise WorkflowError('INSTALL_NO_TRANSACTION')
        journal=read(self.journal)
        self._rollback(journal, apply=False)
        if dry_run: return {'recovered':False,'dry_run':True,'profile':str(self.root),'action':'rollback'}
        from .hosts import process_token
        lock = self.path('.gsp/writer.lock')
        owner = journal.get('owner')
        if lock.exists():
            if not owner or read(lock).get('pid') != owner.get('pid') or process_token(owner['pid']) is not None:
                raise WorkflowError('INSTALL_RECOVERY_OWNER_ACTIVE_OR_UNKNOWN')
            lock.unlink()  # Only the confirmed-dead transaction's exact lock.
        with locked(self.root):
            journal=read(self.journal)
            self._rollback(journal)
        return {'recovered':not dry_run,'dry_run':dry_run,'profile':str(self.root),'action':'rollback'}
