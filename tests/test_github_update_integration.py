import json,subprocess,sys,tempfile,unittest
from pathlib import Path
from gsp.version_store import VersionStore
class GithubUpdateIntegrationTests(unittest.TestCase):
 def test_two_host_targets_share_version_and_rollback(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t); a=root/'v1'; b=root/'v2'; a.mkdir(); b.mkdir(); (a/'x').write_text('1'); (b/'x').write_text('2')
   for host in ('codex','claude'):
    s=VersionStore(root/host); s.switch('v1',a); s.switch('v2',b); s.rollback('v1'); self.assertEqual(s.current()['version'],'v1')

 def test_cli_resolves_stable_tag_and_reports_store(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t); source=root/'source'; source.mkdir(); self.git(source,'init','-b','main'); (source/'README.md').write_text('v1'); self.git(source,'add','.'); self.git(source,'commit','-m','v1'); self.git(source,'tag','v1.0.0')
   target=root/'versions'/'v1.0.0'; store=root/'store'; project=root/'project'
   cmd=[sys.executable,str(Path(__file__).parents[1]/'scripts/gsp.py'),'--project',str(project),'source-resolve','--source',str(source),'--version','v1.0.0','--target',str(target),'--store',str(store)]
   result=subprocess.run(cmd,capture_output=True,text=True)
   self.assertEqual(result.returncode,0,result.stderr)
   self.assertTrue((target/'README.md').is_file())
   status=subprocess.run([sys.executable,str(Path(__file__).parents[1]/'scripts/gsp.py'),'--project',str(project),'version-status','--store',str(store)],capture_output=True,text=True)
   self.assertEqual(json.loads(status.stdout)['current']['version'],'v1.0.0')

 def git(self,root,*args):
  return subprocess.run(['git','-c','user.name=fixture','-c','user.email=fixture@example.invalid','-C',str(root),*args],capture_output=True,text=True)
if __name__=='__main__': unittest.main()
