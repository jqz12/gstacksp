"""Bounded verification input selection; never hashes build caches by accident."""
import fnmatch
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

from .runtime import WorkflowError, inside, read, sha

BUILD_DIRS={'target','build','dist','out','.next','.cache','.mypy_cache','.ruff_cache'}
HISTORY=('docs/work/','docs/superpowers/','docs/gsp/')


def scan(root, deadline=None, progress=None):
    from .runtime import SKIP_DIRS
    root=Path(root).resolve()
    deadline=deadline if deadline is not None else time.monotonic()+120
    started=time.monotonic();last=started;count=0;total=0
    git_marker=any((p/'.git').exists() for p in [root,*root.parents])
    def tick(force=False):
        nonlocal last
        clock=time.monotonic()
        if clock>=deadline:raise WorkflowError(f'SCAN_TIMEOUT files={count} bytes={total}; no successful evidence produced')
        if force or clock-last>=2:
            if progress:progress({'files':count,'bytes':total,'elapsed_s':round(clock-started,2)})
            last=clock
    def git(*args):
        tick()
        try:
            r=subprocess.run(['git','-C',str(root),*args],capture_output=True,timeout=max(.01,min(30,deadline-time.monotonic())),
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        except subprocess.TimeoutExpired as exc:raise WorkflowError('SCAN_TIMEOUT git enumeration') from exc
        except FileNotFoundError:
            if git_marker:raise WorkflowError('SCAN_GIT_UNAVAILABLE existing repository requires Git')
            return None
        if r.returncode==0:return r.stdout
        if args==('rev-parse','--show-toplevel') and not git_marker and b'not a git repository' in r.stderr.lower():return None
        raise WorkflowError('SCAN_GIT_FAILED cannot safely determine verification inputs')
    policy=root/'.gsp/verification-inputs.json'
    if not inside(root,policy):raise WorkflowError('VERIFICATION_POLICY_ESCAPE')
    data=read(policy) if policy.exists() else {}
    if not isinstance(data,dict):raise WorkflowError('VERIFICATION_POLICY_INVALID')
    include=data.get('include',[]);exclude=data.get('exclude',[])
    for patterns in (include,exclude):
        if not isinstance(patterns,list) or any(not isinstance(p,str) or not p or Path(p).is_absolute() or '..' in Path(p).parts for p in patterns):raise WorkflowError('VERIFICATION_POLICY_INVALID')
    def eligible(rel):
        return not any(part in SKIP_DIRS for part in Path(rel).parts) and not rel.endswith('.pyc') and not rel.startswith(HISTORY)
    def excluded(rel):return any(fnmatch.fnmatchcase(rel,p) for p in exclude)
    top=git('rev-parse','--show-toplevel')
    tracked=set();paths=set()
    if top is not None:
        for args,is_tracked in [(('ls-files','--cached','-z'),True),(('ls-files','--others','--exclude-standard','-z'),False)]:
            output=git(*args)
            if output is None:raise WorkflowError('SCAN_GIT_ENUMERATION_FAILED')
            for raw in output.split(b'\0'):
                if not raw:continue
                rel=os.fsdecode(raw).replace('\\','/')
                if not eligible(rel):continue
                if is_tracked:tracked.add(rel)
                if excluded(rel):
                    if is_tracked:raise WorkflowError('TRACKED_INPUT_EXCLUDED '+rel)
                    continue
                paths.add(rel)
        mode='git-tracked-and-unignored'
    else:
        mode='filesystem-fallback'
        for folder,dirs,files in os.walk(root,followlinks=False):
            tick()
            dirs[:]=[d for d in dirs if d not in SKIP_DIRS|BUILD_DIRS and not Path(folder,d).is_symlink() and not getattr(Path(folder,d),'is_junction',lambda:False)() and eligible(Path(folder,d).relative_to(root).as_posix()+'/') and not excluded(Path(folder,d).relative_to(root).as_posix()+'/')]
            for name in files:
                rel=(Path(folder)/name).relative_to(root).as_posix()
                if eligible(rel) and not excluded(rel):paths.add(rel)
    # Explicitly necessary ignored inputs; bounded traversal rather than root.glob.
    if include:
        for folder,dirs,files in os.walk(root,followlinks=False):
            tick()
            prefix=Path(folder).relative_to(root).as_posix()
            prefix='' if prefix=='.' else prefix+'/'
            def may_match(child):
                for pattern in include:
                    literal=pattern
                    for char in '*?[':literal=literal.split(char,1)[0]
                    if literal.startswith(child) or child.startswith(literal):return True
                return False
            dirs[:]=[d for d in dirs if d!='.git' and not Path(folder,d).is_symlink() and not getattr(Path(folder,d),'is_junction',lambda:False)() and may_match(prefix+d+'/')]
            for name in files:
                rel=prefix+name
                if any(fnmatch.fnmatchcase(rel,p) for p in include):paths.add(rel)
    for rel in ('.gsp/backlog/records.json','.gsp/verification-inputs.json'):
        if (root/rel).is_file():paths.add(rel)
    records={'@gsp-input-schema':'3','@selection':mode}
    for rel in sorted(paths):
        tick();path=root/rel
        if path.is_symlink():records[rel]='link:'+os.readlink(path);continue
        if not inside(root,path):raise WorkflowError('VERIFICATION_INPUT_ESCAPE '+rel)
        if not path.exists():continue  # Tracked deletion is represented by absence.
        if path.is_dir():
            if rel in tracked:raise WorkflowError('SCAN_SUBMODULE_UNSUPPORTED '+rel)
            continue
        try:
            before=path.stat();h=hashlib.sha256()
            with path.open('rb') as stream:
                while True:
                    tick();chunk=stream.read(1024*1024)
                    if not chunk:break
                    h.update(chunk);total+=len(chunk)
            after=path.stat()
            if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise WorkflowError('SCAN_INPUT_CHANGED '+rel)
            records[rel]=h.hexdigest();count+=1
        except OSError as exc:raise WorkflowError('FINGERPRINT_FAILED '+rel) from exc
    tick(True)
    return records
