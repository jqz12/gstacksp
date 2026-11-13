import hashlib,json
from pathlib import Path
EXCLUDED={'.git','.gsp','.superpowers','__pycache__','node_modules','.venv','venv'}
def build_manifest(root,source,version,commit):
 if not source.startswith('https://github.com/') or not version.startswith('v') or not commit: raise ValueError('invalid source/version/commit')
 root=Path(root); files=[]
 for p in root.rglob('*'):
  if not p.is_file() or any(part in EXCLUDED for part in p.relative_to(root).parts): continue
  files.append(p.relative_to(root).as_posix())
 files.sort(); hashes={f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files}
 result={'schema':1,'source':source,'version':version,'commit':commit,'files':files,'hashes':hashes}
 result['manifest_hash']=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest(); return result
def validate_manifest(m):
 if m.get('schema')!=1 or not m.get('source','').startswith('https://github.com/') or not m.get('version','').startswith('v') or not m.get('commit'): raise ValueError('invalid manifest')
 if m.get('files')!=sorted(m.get('files',[])): raise ValueError('files not sorted')
 return True
