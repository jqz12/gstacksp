from pathlib import Path
EXCLUDED={'.git','.gsp','.superpowers','__pycache__','node_modules','.venv','venv'}
def publishable_files(root):
 root=Path(root); out=[]
 for p in root.rglob('*'):
  if p.is_file() and not any(x in EXCLUDED for x in p.relative_to(root).parts): out.append(p.relative_to(root).as_posix())
 return sorted(out)
def main():
 import argparse
 p=argparse.ArgumentParser();p.add_argument('--check',action='store_true');a=p.parse_args(); files=publishable_files(Path('.')); bad=[f for f in files if f.startswith(('docs/gsp/','research/')) and ('WXAssistant' in f or 'report' in f)]
 if bad: print('\n'.join(bad)); return 1
 print(f'publishable files: {len(files)}'); return 0
if __name__=='__main__': raise SystemExit(main())
