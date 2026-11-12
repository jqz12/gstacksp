from pathlib import Path
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from gsp import codex_hooks as hooks


class CodexHooksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'.gsp/runs/task').mkdir(parents=True)
        (self.root/'src').mkdir()
        (self.root/'.gsp/project.json').write_text('{"id":"fixture"}')
        (self.root/'.gsp/active.json').write_text('{"task":"task"}')
        self.state = {'id':'task','status':'execute','scope':{'mode':'frozen','path':str(self.root/'src')},
                      'documents':{'design':'docs/spec.md','plan':'docs/plan.md'},'doc_dir':'docs/work/task'}
        (self.root/'.gsp/runs/task/state.json').write_text(json.dumps(self.state))

    def call(self, event, **kw):
        return hooks.handle({'cwd':str(self.root),'session_id':'session','turn_id':'turn',
                             'hook_event_name':event, **kw})

    def test_non_gsp_is_noop_and_does_not_create_files(self):
        root=self.root/'unrelated';root.mkdir()
        self.assertEqual(hooks.handle({'cwd':str(root),'hook_event_name':'Stop','session_id':'x'}), {})
        # Parent is a GSP project, so use an independent root instead.
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(hooks.handle({'cwd':folder,'hook_event_name':'SessionStart'}),{})
            self.assertEqual(list(Path(folder).iterdir()),[])

    def test_frozen_patch_all_paths_including_rename(self):
        for text in ['*** Begin Patch\n*** Add File: src/a.py\n+x\n*** End Patch',
                     '*** Begin Patch\n*** Delete File: src/a.py\n*** End Patch']:
            self.assertNotIn('permissionDecision',self.call('PreToolUse',tool_name='apply_patch',tool_input={'command':text}).get('hookSpecificOutput',{}))
        for text in ['*** Begin Patch\n*** Add File: outside.py\n+x\n*** End Patch',
                     '*** Begin Patch\n*** Update File: src/a.py\n*** Move to: outside.py\n@@\n-x\n+y\n*** End Patch',
                     '*** Begin Patch\n*** Add File: src/../../escape.py\n+x\n*** End Patch','garbage']:
            result=self.call('PreToolUse',tool_name='apply_patch',tool_input={'command':text})
            self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny',text)

    def test_lifecycle_never_continues_or_marks_complete(self):
        before=(self.root/'.gsp/runs/task/state.json').read_bytes()
        self.assertIn('additionalContext',self.call('SessionStart')['hookSpecificOutput'])
        for event in ['Stop','Interrupt','SessionEnd','UserPromptSubmit']:
            self.assertNotIn('decision',self.call(event,prompt='SECRET',last_assistant_message='SECRET'))
        self.assertEqual(before,(self.root/'.gsp/runs/task/state.json').read_bytes())

    def test_audit_does_not_store_sensitive_input_and_declares_gaps(self):
        self.call('PreToolUse',tool_name='Bash',tool_input={'command':'echo SECRET'},tool_use_id='a')
        self.call('PostToolUse',tool_name='Bash',tool_response={'stdout':'SECRET'},tool_use_id='a')
        files=list((self.root/'.gsp/audits/codex-hooks').rglob('*.json*'))
        text=''.join(p.read_text() for p in files)
        self.assertNotIn('SECRET',text)
        self.assertIn('shell-not-enforced',text)
        self.assertNotIn('transcript_path',text)

    def test_concurrent_appends_are_whole_json_lines(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i:self.call('PostToolUse',tool_name='Bash',tool_use_id=str(i)), range(20)))
        rows=[json.loads(line) for p in (self.root/'.gsp/audits/codex-hooks').rglob('events-*.jsonl') for line in p.read_text().splitlines()]
        self.assertEqual(len(rows),20)

    def test_storage_limit_is_explicit(self):
        from unittest.mock import patch
        with patch.object(hooks,'SESSION_LIMIT',80):
            self.call('Stop')
        files=list((self.root/'.gsp/audits/codex-hooks').rglob('coverage.json'))
        self.assertTrue(json.loads(files[0].read_text())['truncated'])

    def test_invalid_active_identifier_cannot_escape(self):
        (self.root/'.gsp/active.json').write_text('{"task":"../../elsewhere"}')
        result=self.call('PreToolUse',tool_name='apply_patch',tool_input={'command':'x'})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')

    def test_report_deduplicates_replay_and_preserves_unknown_attempts(self):
        self.call('PreToolUse',tool_name='Bash',tool_use_id='one')
        self.call('PreToolUse',tool_name='Bash',tool_use_id='one')
        self.call('PostToolUse',tool_name='Bash',tool_use_id='one')
        self.call('PreToolUse',tool_name='Bash',tool_use_id='two')
        report=hooks.report(self.root)
        self.assertEqual(report['attempts'],2)
        self.assertEqual(report['results_observed'],1)
        self.assertEqual(report['unknown_attempts'],1)
        self.assertEqual(report['replayed_events'],1)
        self.assertTrue(report['audit_events_observed'])
        self.assertEqual(report['native_host_trigger'],'NOT_CHECKED')

    def test_report_marks_session_and_shard_caps_incomplete(self):
        from unittest.mock import patch
        self.call('Stop')
        hooks.handle({'cwd':str(self.root),'session_id':'second','hook_event_name':'Stop'})
        with patch.object(hooks,'REPORT_SESSIONS',1):
            report=hooks.report(self.root)
        self.assertTrue(report['incomplete'])
        self.assertEqual(report['sessions_skipped'],1)
        folder=next((self.root/'.gsp/audits/codex-hooks').iterdir())
        (folder/'events-000002.jsonl').write_bytes((folder/'events-000001.jsonl').read_bytes())
        with patch.object(hooks,'REPORT_SHARDS',1):
            report=hooks.report(self.root)
        self.assertTrue(report['incomplete'])
        self.assertEqual(report['shards_skipped'],1)

    def test_audit_recovers_byte_counter_after_interrupted_coverage_write(self):
        from unittest.mock import patch
        self.call('Stop')
        status=next((self.root/'.gsp/audits/codex-hooks').rglob('coverage.json'))
        d=json.loads(status.read_text());size=d['bytes'];d['bytes']=0
        status.write_text(json.dumps(d))
        with patch.object(hooks,'SESSION_LIMIT',size+1):self.call('Stop')
        self.assertTrue(json.loads(status.read_text())['truncated'])
