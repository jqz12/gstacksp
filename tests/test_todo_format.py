"""Human Markdown is authoritative; execution records never replace its prose."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_todos
from gsp.runtime import atomic


class HumanTodoTests(unittest.TestCase):
    setUp = test_todos.TodoTests.setUp
    cli = test_todos.TodoTests.cli
    add = test_todos.TodoTests.add
    work = test_todos.TodoTests.work
    finish_task = test_todos.TodoTests.finish_task

    def records(self):
        return json.loads((self.root/'.gsp/backlog/records.json').read_text(encoding='utf-8'))

    def test_human_document_has_no_execution_fields(self):
        self.cli('init'); item=self.add()['item']
        text=(self.root/'TODOS.md').read_text(encoding='utf-8')
        for field in ('Origin','Blocking','Status','Evidence','Resolution','History','Source'):
            self.assertNotIn('**'+field+':**',text)
        record=self.records()['items'][item['id']]
        self.assertEqual(record['source'],'fixture/review#1')
        self.assertNotIn('what',record)
        self.assertNotIn('title',record)

    def test_human_multiline_changes_are_read_without_cached_prose(self):
        self.cli('init'); self.add()
        file=self.root/'TODOS.md'
        text=file.read_text(encoding='utf-8').replace('Observed in fixture','First paragraph.\n\nSecond paragraph with a concrete starting point.\n- Reproduce missing configuration.')
        file.write_text(text,encoding='utf-8')
        item=self.cli('todo-list')['items'][0]
        self.assertIn('Second paragraph',item['context'])
        self.assertIn('\n- Reproduce',item['context'])
        self.assertTrue(self.cli('todo-list')['document_changed'])

    def test_components_priorities_and_dependencies_are_visible(self):
        self.cli('init')
        self.add(title='Later',what='Later fix',source='review/later',component='Runtime',priority='P3')
        self.add(title='First',what='First fix',source='review/first',component='Runtime',priority='P0',depends_on='Configuration contract')
        text=(self.root/'TODOS.md').read_text(encoding='utf-8')
        self.assertIn('## Runtime',text)
        self.assertLess(text.index('— First'),text.index('— Later'))
        self.assertIn('**Depends on:** Configuration contract',text)

    def test_legacy_migration_preserves_proof_and_creates_backup(self):
        self.cli('init')
        file=self.root/'TODOS.md'
        legacy=('# TODOS\n\nHuman note stays.\n\n## Runtime\n\n<!-- gsp-todo:GSP-0001 -->\n'
                '### GSP-0001 — Existing issue\n\n**What:** Existing work\n**Why:** Existing reason\n'
                '**Context:** Existing starting point\n**Priority:** P1\n**Effort:** M\n'
                '**Source:** old/review\n**Origin:** older\n**Blocking:** no\n**Status:** done\n'
                '**Evidence:** .gsp/runs/older/evidence/proof/receipt.json\n**Resolution:** Checked actual behavior\n'
                '**History:** 2026-11-07 completed by older/T1\n<!-- /gsp-todo:GSP-0001 -->\n')
        file.write_text(legacy,encoding='utf-8')
        result=self.cli('todo-migrate')
        self.assertEqual(Path(result['backup']).read_text(encoding='utf-8'),legacy)
        item=self.cli('todo-list')['items'][0]
        self.assertEqual(item['status'],'done')
        self.assertEqual(item['evidence'],'.gsp/runs/older/evidence/proof/receipt.json')
        self.assertIn('Human note stays.',file.read_text(encoding='utf-8'))
        self.assertIn('**Completed:**',file.read_text(encoding='utf-8'))
        self.assertFalse(self.cli('todo-migrate')['migrated'])

    def test_manual_completed_move_does_not_forge_execution_proof(self):
        self.cli('init'); self.work()
        item=self.add(origin='work')['item']
        file=self.root/'TODOS.md'; text=file.read_text(encoding='utf-8')
        start=text.index('<!-- gsp-todo:'); end=text.index('<!-- /gsp-todo:')
        end=text.index('-->',end)+3
        block=text[start:end]
        text=text[:start]+text[end:]
        text=text.rstrip()+'\n\n'+block+'\n'
        file.write_text(text,encoding='utf-8')
        row=self.cli('todo-list')['items'][0]
        self.assertEqual(row['status'],'open')
        self.assertTrue(row['completion_unverified'])
        self.assertEqual(row['id'],item['id'])

    def test_completion_annotation_and_edited_completed_content(self):
        self.cli('init'); self.work(); item=self.add()['item']; self.finish_task()
        self.cli('todo-done',item['id'],'--run','work','--task','T1','--reason','Verified fixture behavior','--version','1.2.3')
        file=self.root/'TODOS.md'
        text=file.read_text(encoding='utf-8')
        self.assertIn('**Completed:** 1.2.3 (',text)
        self.assertNotIn('**Evidence:**',text)
        file.write_text(text.replace('Handle absent configuration','Handle another configuration failure'),encoding='utf-8')
        row=self.cli('todo-list')['items'][0]
        self.assertEqual(row['status'],'open')
        self.assertTrue(row['completion_unverified'])

    def test_missing_execution_records_are_not_assumed_empty(self):
        self.cli('init'); self.add()
        (self.root/'.gsp/backlog/records.json').unlink()
        self.assertIn('BACKLOG_RECORDS_MISSING',self.cli('todo-list',ok=False))

    def test_interrupted_two_file_write_requires_explicit_recovery(self):
        self.cli('init'); self.add()
        def fail_record(path,value):
            if Path(path)==self.root/'.gsp/backlog/records.json':
                raise OSError('SYNTHETIC record publication failure')
            return atomic(path,value)
        with patch('gsp.todos.atomic',side_effect=fail_record):
            self.assertIn('SYNTHETIC',self.cli('todo-add','--title','Another','--what','Another issue',
                '--why','Another reason','--context','Another starting point','--source','review/second',ok=False))
        self.assertIn('BACKLOG_RECOVERY_REQUIRED',self.cli('todo-list',ok=False))
        self.cli('todo-recover','--reason','SYNTHETIC inspected exact intended document/record hashes')
        self.assertEqual(len(self.cli('todo-list')['items']),2)

    def test_recovery_does_not_overwrite_intervening_human_changes(self):
        self.test_interrupted_two_file_write_requires_explicit_recovery()
        # Create another interrupted publication, then edit the canonical prose.
        def fail_record(path,value):
            if Path(path)==self.root/'.gsp/backlog/records.json': raise OSError('SYNTHETIC')
            return atomic(path,value)
        with patch('gsp.todos.atomic',side_effect=fail_record):
            self.cli('todo-add','--title','Third','--what','Third issue','--why','Reason','--context','Context','--source','review/third',ok=False)
        file=self.root/'TODOS.md'; file.write_text(file.read_text(encoding='utf-8')+'\nMy new human note.\n',encoding='utf-8')
        self.assertIn('BACKLOG_RECOVERY_DIVERGED',self.cli('todo-recover','--reason','Observed human change',ok=False))
        self.assertIn('My new human note.',file.read_text(encoding='utf-8'))

    def test_plan_association_is_recorded_without_closing_the_issue(self):
        self.cli('init'); self.work(); item=self.add()['item']
        self.cli('todo-link',item['id'],'--run','work','--task','T1')
        row=self.cli('todo-list')['items'][0]
        self.assertEqual(row['links'][0]['task'],'T1')
        self.assertEqual(row['status'],'open')
        self.assertNotIn('**Links:**',(self.root/'TODOS.md').read_text(encoding='utf-8'))

    def test_human_extra_notes_survive_another_item_being_added(self):
        self.cli('init'); self.add()
        file=self.root/'TODOS.md'
        text=file.read_text(encoding='utf-8').replace('— Keep follow-up\n','— Keep follow-up\n\nMy hand-written explanation.\n')
        text=text.replace('<!-- /gsp-todo:', '**User note:** Keep this extra context.\n\n<!-- /gsp-todo:')
        file.write_text(text,encoding='utf-8')
        self.add(what='Another issue',source='review/new')
        text=file.read_text(encoding='utf-8')
        self.assertIn('My hand-written explanation.',text)
        self.assertIn('**User note:** Keep this extra context.',text)

    def test_removed_id_is_not_reused_for_another_problem(self):
        self.cli('init'); first=self.add()['item']
        file=self.root/'TODOS.md'; text=file.read_text(encoding='utf-8')
        import re
        text=re.sub(r'<!-- gsp-todo:.*?<!-- /gsp-todo:.*?-->','',text,flags=re.S)
        file.write_text(text,encoding='utf-8')
        second=self.add(what='Another issue',source='review/new')['item']
        self.assertNotEqual(first['id'],second['id'])

    def test_execution_records_participate_in_saved_input_integrity(self):
        self.cli('init'); self.add()
        from gsp.runtime import fingerprint
        before=fingerprint(self.root)
        file=self.root/'.gsp/backlog/records.json'
        record=self.records(); record['items']['GSP-0001']['blocking']='yes'
        file.write_text(json.dumps(record),encoding='utf-8')
        self.assertNotEqual(before,fingerprint(self.root))

    def test_format_keeps_human_components_and_normalizes_priority(self):
        self.cli('init'); self.add(what='Low priority',source='review/low',priority='P3',component='Runtime')
        self.add(what='High priority',source='review/high',priority='P0',component='Runtime')
        file=self.root/'TODOS.md'; text=file.read_text(encoding='utf-8')
        text=text.replace('## Runtime','## Configuration')
        file.write_text(text,encoding='utf-8')
        self.cli('todo-format')
        self.assertIn('## Configuration',file.read_text(encoding='utf-8'))
        self.assertEqual(self.cli('todo-list')['items'][0]['component'],'Configuration')

    def test_deleting_a_required_entry_does_not_bypass_ready(self):
        self.cli('init'); self.work()
        self.cli('todo-add','--title','Required','--what','Required issue','--why','Acceptance gap',
                 '--context','Reproduce this gap','--source','review/required','--origin','work','--blocking')
        file=self.root/'TODOS.md'; text=file.read_text(encoding='utf-8')
        import re
        file.write_text(re.sub(r'<!-- gsp-todo:.*?<!-- /gsp-todo:.*?-->','',text,flags=re.S),encoding='utf-8')
        self.assertIn('BACKLOG_BLOCKING',self.cli('ready','work',ok=False))

    def test_pasting_legacy_fields_into_new_body_cannot_forge_completion(self):
        self.cli('init'); self.add()
        file=self.root/'TODOS.md'
        fake=('**Source:** manual note\n**Origin:** work\n**Blocking:** no\n**Status:** done\n'
              '**Evidence:** nonexistent.json\n**Resolution:** not verified\n**History:** not verified\n')
        text=file.read_text(encoding='utf-8').replace('<!-- /gsp-todo:',fake+'<!-- /gsp-todo:')
        file.write_text(text,encoding='utf-8')
        self.assertEqual(self.cli('todo-list')['items'][0]['status'],'open')


if __name__=='__main__':
    unittest.main()
