import subprocess
import sys
import tempfile
from pathlib import Path
import unittest

from gsp.document_health import audit, git_route


class GitHealthRouteTests(unittest.TestCase):
    def repo(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root=Path(tmp.name); subprocess.run(['git','init','-q'],cwd=root,check=True)
        subprocess.run(['git','config','user.email','test@example.com'],cwd=root,check=True)
        subprocess.run(['git','config','user.name','Test'],cwd=root,check=True)
        (root/'README.md').write_text('# test',encoding='utf-8')
        subprocess.run(['git','add','README.md'],cwd=root,check=True)
        subprocess.run(['git','commit','-qm','chore: baseline'],cwd=root,check=True)
        return root

    def test_clean_routes_to_brainstorming_without_decision(self):
        r=git_route(self.repo()); self.assertEqual(r['status'],'clean'); self.assertEqual(r['next_skill'],'gsp-brainstorming'); self.assertFalse(r['user_decision_required'])

    def test_dirty_and_untracked_routes_to_gsp_git(self):
        root=self.repo(); (root/'README.md').write_text('changed',encoding='utf-8'); (root/'new.txt').write_text('new',encoding='utf-8')
        r=git_route(root); self.assertEqual(r['next_skill'],'gsp-ship'); self.assertTrue(r['user_decision_required']); self.assertEqual(r['decision_kind'],'worktree')

    def test_non_repository_is_explicit_decision(self):
        with tempfile.TemporaryDirectory() as d:
            r=git_route(Path(d)); self.assertEqual(r['status'],'not-a-repository'); self.assertEqual(r['next_skill'],'gsp-ship'); self.assertTrue(r['blocking'])

    def test_strict_health_returns_nonzero_for_detached_route(self):
        root=self.repo(); subprocess.run(['git','checkout','--quiet','--detach','HEAD'],cwd=root,check=True)
        cli=Path(__file__).resolve().parents[1]/'scripts/gsp.py'
        result=subprocess.run([sys.executable,str(cli),'--project',str(root),'health','--documents-only','--strict'],capture_output=True,text=True)
        self.assertEqual(result.returncode,2,result.stderr)

    def test_audit_exposes_compact_route_and_keeps_report_read_only(self):
        root=self.repo(); before=sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()); report=audit(root,Path(tempfile.mkdtemp()))
        self.assertIn('route',report); self.assertIn('next_skill',report['route']); after=sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()); self.assertEqual(before,after)
