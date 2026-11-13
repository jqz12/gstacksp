import os,shutil,subprocess,tempfile
from pathlib import Path

def resolve_source(source,version,target,timeout=120):
 if not version.startswith('v'): raise ValueError('stable tag required')
 target=Path(target)
 if target.exists(): raise RuntimeError('target already exists')
 target.parent.mkdir(parents=True,exist_ok=True)
 tmp=Path(tempfile.mkdtemp(prefix='gsp-source-',dir=target.parent))
 try:
  probe=subprocess.run(['git','ls-remote','--exit-code','--tags',source,f'refs/tags/{version}'],capture_output=True,text=True,timeout=timeout)
  if probe.returncode!=0: raise RuntimeError('tag unavailable')
  commit=probe.stdout.split()[0]
  clone=subprocess.run(['git','clone','--depth','1','--branch',version,source,str(tmp/'checkout')],capture_output=True,text=True,timeout=timeout)
  if clone.returncode!=0: raise RuntimeError('source download failed')
  os.replace(tmp/'checkout',target)
  return {'source':source,'version':version,'commit':commit,'path':str(target)}
 finally:
  shutil.rmtree(tmp,ignore_errors=True)
