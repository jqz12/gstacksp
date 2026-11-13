from pathlib import Path
EXCLUDED={'.git','.gsp','.superpowers','__pycache__','node_modules','.venv','venv'}
PRIVATE_DIRS={'docs/gsp','docs/work','docs/superpowers','docs/audits','research','dist'}
PRIVATE_FILES={'AGENTS.md','CLAUDE.md','TODOS.md'}
def publishable_files(root):
 root=Path(root); out=[]
 for p in root.rglob('*'):
  rel=p.relative_to(root).as_posix()
  if p.is_file() and rel not in PRIVATE_FILES and not any(x in EXCLUDED for x in p.relative_to(root).parts) and not any(rel==d or rel.startswith(d+'/') for d in PRIVATE_DIRS): out.append(rel)
 return sorted(out)
def main():
 import argparse
 p=argparse.ArgumentParser();p.add_argument('--check',action='store_true');a=p.parse_args(); files=publishable_files(Path('.'))
 print(f'publishable files: {len(files)}'); return 0
if __name__=='__main__': raise SystemExit(main())
