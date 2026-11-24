from pathlib import Path
import json
import subprocess
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

    def test_direct_edit_uses_patch_target_worktree(self):
        root=self.root/'topology'
        parent=root/'parent-worktree'
        child=root/'child-worktree'
        (root/'.gsp/runs/old').mkdir(parents=True)
        (root/'.gsp/runs/task').mkdir(parents=True)
        parent.mkdir(); child.mkdir()
        subprocess.run(['git','init','-b','feature/task'],cwd=parent,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['git','init','-b','feature/task-T01'],cwd=child,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        (root/'.gsp/project.json').write_text(json.dumps({'id':'topology-fixture','root':str(root)}))
        (root/'.gsp/active.json').write_text(json.dumps({'task':'task'}))
        old={
            'id':'old','root':str(root),'status':'ready','scope':{'mode':'frozen','path':str(root)},
            'documents':{'design':'docs/old-spec.md','plan':'docs/old-plan.md'},
            'git_topology':{'effective_mode':'in-place','branch':'main','workspace_root':str(root),
                            'control_root':str(root),'intent':'single-task','enforce':True}
        }
        task={
            'id':'task','root':str(root),'status':'execute','scope':{'mode':'frozen','path':str(parent)},
            'documents':{'design':'docs/work/task/spec.md','plan':'docs/work/task/plan.md'},'doc_dir':'docs/work/task',
            'git_topology':{'effective_mode':'worktree','branch':'feature/task','workspace_root':str(parent),
                            'control_root':str(root),'intent':'parallel-multi-task','enforce':True},
            'tasks':[{'id':'T01','status':'running','role':'implementation','topology':{
                'effective_mode':'worktree','branch':'feature/task-T01','workspace_root':str(child)}}]
        }
        (root/'.gsp/runs/old/state.json').write_text(json.dumps(old))
        (root/'.gsp/runs/task/state.json').write_text(json.dumps(task))
        command='*** Begin Patch\n*** Add File: child-worktree/source.txt\n+x\n*** End Patch'
        result=hooks.handle({'cwd':str(root),'session_id':'topology-session','hook_event_name':'PreToolUse',
                             'tool_name':'apply_patch','tool_input':{'command':command}})
        self.assertNotEqual(result.get('hookSpecificOutput',{}).get('permissionDecision'),'deny')

    def test_direct_edit_rejects_foreign_patch_target(self):
        root=self.root/'topology'
        (root/'.gsp/runs/task').mkdir(parents=True)
        (root/'.gsp/runs/foreign').mkdir(parents=True)
        local=root/'local'; foreign=root/'foreign'
        local.mkdir(); foreign.mkdir()
        (root/'.gsp/project.json').write_text(json.dumps({'id':'topology-fixture','root':str(root)}))
        (root/'.gsp/active.json').write_text(json.dumps({'task':'task'}))
        state={
            'id':'task','root':str(root),'status':'execute','scope':{'mode':'frozen','path':str(local)},
            'documents':{'design':'docs/spec.md','plan':'docs/plan.md'},'doc_dir':'docs/work/task',
            'git_topology':{'effective_mode':'worktree','branch':'feature/task','workspace_root':str(local),
                            'control_root':str(root),'intent':'parallel-multi-task'},
            'tasks':[{'id':'T01','status':'running','role':'implementation','topology':{
                'effective_mode':'worktree','branch':'feature/task-T01','workspace_root':str(local)}},
                     {'id':'T02','status':'pending','role':'implementation','topology':{
                         'effective_mode':'worktree','branch':'feature/task-T02','workspace_root':str(foreign)}}]
        }
        (root/'.gsp/runs/task/state.json').write_text(json.dumps(state))
        result=hooks.handle({'cwd':str(root),'session_id':'topology-session','hook_event_name':'PreToolUse',
                             'tool_name':'apply_patch','tool_input':{
                                 'command':'*** Begin Patch\n*** Add File: foreign/source.txt\n+x\n*** End Patch'}})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')

    def test_wait_guard_blocks_third_empty_wait(self):
        def wait(tool_session='exec-1', **options):
            data={'chars':'','session_id':tool_session,'yield_time_ms':30000,'max_output_tokens':20000}
            data.update(options)
            return self.call('PreToolUse',tool_name='write_stdin',tool_input=data,tool_use_id=tool_session)

        self.assertNotEqual(wait().get('hookSpecificOutput',{}).get('permissionDecision'),'deny')
        self.assertNotEqual(wait().get('hookSpecificOutput',{}).get('permissionDecision'),'deny')
        result=wait()
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')
        reason=result['hookSpecificOutput']['permissionDecisionReason'].lower()
        self.assertTrue(any(word in reason for word in ('log','process','exit','artifact')))

    def test_wait_guard_counts_same_tool_session_across_wait_parameter_changes(self):
        def wait(**options):
            data={'chars':'','session_id':'exec-parameters','yield_time_ms':30000,'max_output_tokens':20000}
            data.update(options)
            return self.call('PreToolUse',tool_name='write_stdin',tool_input=data)

        wait()
        wait(yield_time_ms=10000)
        result=wait(max_output_tokens=1000)
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')

    def test_wait_guard_accepts_numeric_session_and_bash_alias(self):
        def wait(tool_name, session_id):
            return self.call('PreToolUse',tool_name=tool_name,tool_input={
                'session_id':session_id,'chars':'','yield_time_ms':1000,'max_output_tokens':2000})

        for tool_name,session_id in (('Bash',74439),('exec_command',74440)):
            with self.subTest(tool=tool_name):
                self.assertNotIn('permissionDecision',wait(tool_name,session_id).get('hookSpecificOutput',{}))
                self.assertNotIn('permissionDecision',wait(tool_name,session_id).get('hookSpecificOutput',{}))
                result=wait(tool_name,session_id)
                self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')

    def test_wait_guard_shares_streak_between_write_stdin_and_alias(self):
        self.assertNotIn('permissionDecision',self.call(
            'PreToolUse',tool_name='write_stdin',tool_input={
                'session_id':74439,'chars':'','yield_time_ms':1000}).get('hookSpecificOutput',{}))
        self.assertNotIn('permissionDecision',self.call(
            'PreToolUse',tool_name='Bash',tool_input={
                'session_id':'74439','chars':'','yield_time_ms':1000}).get('hookSpecificOutput',{}))
        result=self.call('PreToolUse',tool_name='Bash',tool_input={
            'session_id':74439,'chars':'','yield_time_ms':1000})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')

    def test_real_bash_command_resets_alias_wait_streak(self):
        def wait():
            return self.call('PreToolUse',tool_name='Bash',tool_input={
                'session_id':74439,'chars':'','yield_time_ms':1000})

        wait(); wait()
        self.assertNotIn('permissionDecision',self.call(
            'PreToolUse',tool_name='Bash',tool_input={
                'command':'Get-Content log.txt','session_id':74439,'chars':''}).get('hookSpecificOutput',{}))
        self.assertNotIn('permissionDecision',wait().get('hookSpecificOutput',{}))

    def test_wait_guard_accepts_json_string_tool_input(self):
        payload=json.dumps({'session_id':74439,'chars':'','yield_time_ms':1000,'max_output_tokens':2000})
        for _ in range(2):
            self.assertNotIn('permissionDecision',self.call(
                'PreToolUse',tool_name='Bash',tool_input=payload).get('hookSpecificOutput',{}))
        result=self.call('PreToolUse',tool_name='Bash',tool_input=payload)
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')

    def test_wait_guard_isolates_sessions_and_resets_after_observation_or_input(self):
        def wait(tool_session, chars=''):
            return self.call('PreToolUse',tool_name='write_stdin',tool_input={
                'chars':chars,'session_id':tool_session,'yield_time_ms':30000})

        wait('exec-a'); wait('exec-a')
        self.assertNotEqual(wait('exec-b').get('hookSpecificOutput',{}).get('permissionDecision'),'deny')
        self.assertNotEqual(wait('exec-b').get('hookSpecificOutput',{}).get('permissionDecision'),'deny')
        self.assertEqual(wait('exec-b')['hookSpecificOutput']['permissionDecision'],'deny')
        self.call('PreToolUse',tool_name='exec_command',tool_input={'cmd':'Get-Content log.txt'})
        self.assertNotEqual(wait('exec-b').get('hookSpecificOutput',{}).get('permissionDecision'),'deny')
        wait('exec-b',chars='\x03')
        self.assertNotEqual(wait('exec-b').get('hookSpecificOutput',{}).get('permissionDecision'),'deny')

    def test_wait_guard_serializes_concurrent_empty_waits(self):
        def wait(_):
            return self.call('PreToolUse',tool_name='write_stdin',tool_input={
                'chars':'','session_id':'concurrent-tool','yield_time_ms':30000})

        with ThreadPoolExecutor(max_workers=3) as pool:
            results=list(pool.map(wait,range(3)))
        decisions=[result.get('hookSpecificOutput',{}).get('permissionDecision') for result in results]
        self.assertEqual(decisions.count('deny'),1)
        self.assertEqual(decisions.count(None),2)

    def test_wait_guard_report_is_redacted_and_counts_denials(self):
        payload={'chars':'','session_id':'raw-tool-session-99842','yield_time_ms':30000}
        for _ in range(3):
            self.call('PreToolUse',tool_name='write_stdin',tool_input=payload)
        report=hooks.report(self.root)
        self.assertEqual(report['wait_guard_denied'],1)
        text=''.join(p.read_text() for p in (self.root/'.gsp/audits/codex-hooks').rglob('*') if p.is_file())
        self.assertNotIn('raw-tool-session-99842',text)

    def test_wait_guard_report_counts_bash_alias_denial(self):
        payload={'chars':'','session_id':74439,'yield_time_ms':1000,'max_output_tokens':2000}
        for _ in range(3):
            self.call('PreToolUse',tool_name='Bash',tool_input=payload)
        report=hooks.report(self.root)
        self.assertEqual(report['wait_guard_denied'],1)

    def test_wait_guard_storage_failure_degrades_without_denial(self):
        from unittest.mock import patch
        real=hooks.atomic
        def fail_guard(path,data):
            if Path(path).name=='wait-guard.json': raise OSError('synthetic wait state failure')
            return real(path,data)
        with patch.object(hooks,'atomic',side_effect=fail_guard):
            result=self.call('PreToolUse',tool_name='write_stdin',tool_input={
                'chars':'','session_id':'storage-failure','yield_time_ms':30000})
        self.assertNotEqual(result.get('hookSpecificOutput',{}).get('permissionDecision'),'deny')
        self.assertIn('NOT_CHECKED',result.get('systemMessage',''))

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
