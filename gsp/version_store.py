import hashlib,json,shutil
from pathlib import Path
class VersionStore:
 def __init__(self,root): self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True); self.meta=self.root/'versions.json'
 def _load(self): return json.loads(self.meta.read_text(encoding='utf-8')) if self.meta.exists() else {'current':None,'previous':None,'versions':{}}
 def _hash(self,path): return hashlib.sha256('\n'.join(f'{p.relative_to(path).as_posix()}:{hashlib.sha256(p.read_bytes()).hexdigest()}' for p in sorted(path.rglob('*')) if p.is_file()).encode()).hexdigest()
 def switch(self,version,path):
  d=self._load(); d['previous']=d.get('current'); d['current']={'version':version,'path':str(Path(path)),'manifest_hash':self._hash(Path(path))}; d['versions'][version]=d['current']; self.meta.write_text(json.dumps(d,indent=2),encoding='utf-8')
 def current(self): return self._load().get('current')
 def modified(self):
  c=self.current(); return bool(c and self._hash(Path(c['path']))!=c.get('manifest_hash'))
 def rollback(self,version=None):
  d=self._load(); target=d['versions'].get(version) if version else d.get('previous');
  if not target: raise RuntimeError('version unavailable')
  d['current']=target; self.meta.write_text(json.dumps(d,indent=2),encoding='utf-8')
