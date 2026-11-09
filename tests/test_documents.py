import argparse
import json
from pathlib import Path
import tempfile
import unittest
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from gsp.runtime import Project, dispatch, parser, read, atomic, WorkflowError

class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='gsp-docs-')
        self.root=Path(self.tmp.name)
        self.project=Project(self.root)
        self.project.initialize()
    def tearDown(self): self.tmp.cleanup()
    def cmd(self,*argv):
        values=list(argv)
        cut=values.index('--') if '--' in values else len(values)
        args=parser().parse_args(values[:cut])
        if cut < len(values): args.argv=values[cut+1:]
        return dispatch(self.project,args)[0]
    def create(self,id='login'):
        self.cmd('new',id,'--title','Login','--legacy-layout')
        return read(self.root/'.gsp/runs'/id/'state.json')
    def test_default_new_uses_fixed_work_archive_and_defers_plan(self):
        self.cmd('new','fresh','--title','Fresh task')
        state=read(self.root/'.gsp/runs/fresh/state.json')
        spec=self.project.document(state,'design')
        self.assertIn('docs/work/',state['documents']['design'])
        self.assertEqual(spec.name,'spec.md')
        self.assertFalse(self.project.document(state,'plan').exists())
        spec.write_text('# A real specification with acceptance criteria',encoding='utf-8')
        self.cmd('approve','fresh','--quote','approved')
        self.assertTrue(self.project.document(state,'plan').exists())
        self.assertTrue((spec.parent/'record.json').exists())

    def test_existing_project_document_can_be_bound_in_place(self):
        a=self.create()
        spec=self.root/'docs/old-spec.md'; plan=self.root/'docs/old-plan.md'
        spec.write_text('# Existing specification',encoding='utf-8');plan.write_text('# Existing implementation plan',encoding='utf-8')
        self.cmd('documents-adopt','login','--spec','docs/old-spec.md','--plan','docs/old-plan.md','--reason','existing project source')
        state=read(self.root/'.gsp/runs/login/state.json')
        self.assertEqual(self.project.document(state,'design'),spec)

    def test_interrupted_new_can_continue_same_orphan_run(self):
        from gsp.documents import Governance
        b=Governance(self.root);r=b.create('Saved idea','goal','user',run_id='interrupted')
        r=b.stage(r['uuid'],'design','discussion')
        (self.root/'.gsp/runs/interrupted').mkdir(parents=True)
        atomic(self.root/'.gsp/active.json',{'task':'interrupted'})
        self.cmd('new','interrupted','--title','Saved idea','--work',r['uuid'])
        self.assertEqual(read(self.root/'.gsp/runs/interrupted/state.json')['work_uuid'],r['uuid'])
    def test_classified_unique_documents_and_plan_link(self):
        a=self.create();b=self.create('payment')
        spec=self.project.document(a,'design');plan=self.project.document(a,'plan')
        self.assertEqual(spec.parent,self.root/'docs/superpowers/specs')
        self.assertEqual(plan.parent,self.root/'docs/superpowers/plans')
        self.assertIn('../specs/'+spec.name,plan.read_text(encoding='utf-8'))
        self.assertNotEqual(spec,self.project.document(b,'design'))
        self.assertFalse((self.root/'docs/gsp/tasks/login/design.md').exists())
        self.assertIn(spec.name,(self.root/'docs/gsp/README.md').read_text(encoding='utf-8'))
    def test_legacy_task_keeps_original_documents(self):
        a=self.create();run,doc=self.project.paths('login')
        a.pop('documents');atomic(run/'state.json',a)
        (doc/'design.md').write_text('# Legacy approved specification',encoding='utf-8')
        (doc/'plan.md').write_text('# Legacy plan',encoding='utf-8')
        self.assertEqual(self.project.document(a,'design'),doc/'design.md')
        self.cmd('approve','login','--quote','approved legacy')
        self.cmd('task-add','login','T1','--title','Legacy','--acceptance','verified')
        self.cmd('seal-plan','login')
        self.cmd('scope','login','--off','--quote','fixture')
        self.cmd('task-start','login','T1')
        self.cmd('run','login','--task','T1','--',sys.executable,'-c',"print('Ran 1 test')")
        self.cmd('task-done','login','T1')
        self.cmd('run','login','--task','all','--',sys.executable,'-c',"print('Ran 1 test')")
        (doc/'review.md').write_text('# Review\nLegacy behavior checked; no blockers.',encoding='utf-8')
        self.cmd('review','login','--reviewer','self','--blocking','0')
        self.cmd('ready','login');self.cmd('save','login');self.cmd('resume','login')
        self.assertIn('(design.md)',(doc/'delivery.md').read_text(encoding='utf-8'))

    def test_external_binding_cannot_escape_or_alias_another_task(self):
        a=self.create();b=self.create('payment')
        with self.assertRaises(WorkflowError):
            self.cmd('documents-adopt','login','--spec','../outside.md','--plan',b['documents']['plan'],'--reason','test')
        with self.assertRaises(WorkflowError):
            self.cmd('documents-adopt','login','--spec',b['documents']['design'],'--plan',b['documents']['plan'],'--reason','test')
    def test_adoption_revokes_approval_and_retains_audit_history(self):
        a=self.create();self.project.document(a,'design').write_text('# Approved specification for login',encoding='utf-8')
        self.cmd('approve','login','--quote','approved')
        self.project.document(a,'plan').write_text('# Original plan for T1',encoding='utf-8')
        self.cmd('task-add','login','T1','--title','Login','--acceptance','verified')
        self.cmd('seal-plan','login');self.cmd('scope','login','--off','--quote','fixture')
        self.cmd('task-start','login','T1')
        self.cmd('run','login','--task','T1','--',sys.executable,'-c',"print('Ran 1 test')")
        self.cmd('task-done','login','T1')
        spec=self.root/'docs/superpowers/specs/2026-11-08-login-v2-design.md'
        plan=self.root/'docs/superpowers/plans/2026-11-08-login-v2.md'
        spec.write_text('# Revised specification for login',encoding='utf-8')
        plan.write_text('# Revised plan\nSpec: ../specs/'+spec.name,encoding='utf-8')
        self.cmd('documents-adopt','login','--spec',str(spec.relative_to(self.root)),'--plan',str(plan.relative_to(self.root)),'--reason','explicit classified migration')
        state=read(self.root/'.gsp/runs/login/state.json')
        self.assertIsNone(state['design_approval']);self.assertIsNone(state['plan_seal'])
        self.assertEqual(len(state['document_history']),1)
        self.assertEqual(state['tasks'][0]['status'],'needs-verification')
        self.assertTrue(state['tasks'][0]['history'][0]['evidence'])
        self.cmd('approve','login','--quote','approved new specification')
        self.cmd('seal-plan','login');self.cmd('task-start','login','T1')
        self.assertTrue((self.root/state['document_history'][0]['documents']['design']).is_file())
    def test_preexisting_target_not_overwritten(self):
        from datetime import datetime
        target=self.root/'docs/superpowers/specs'/f'{datetime.now().astimezone().date()}-login-design.md'
        target.parent.mkdir(parents=True);target.write_text('user content',encoding='utf-8')
        with self.assertRaises(WorkflowError): self.create()
        self.assertEqual(target.read_text(encoding='utf-8'),'user content')
