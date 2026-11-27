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
        self.state = {'id':'task','status':'execute','scope':{'mode':'pending','path':None},
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

    def native_workspace(self):
        holder = tempfile.TemporaryDirectory(prefix='gsp-hook-native-discovery-')
        self.addCleanup(holder.cleanup)
        base = Path(holder.name)
        control, native = base / 'control', base / 'native'
        control.mkdir()
        for argv in [
            ['init', '-q', '-b', 'main'],
            ['config', 'user.name', 'Native Hook Test'],
            ['config', 'user.email', 'native-hook@example.invalid'],
        ]:
            result = subprocess.run(['git', *argv], cwd=control,
                                    capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (control / 'README.md').write_text('# native discovery\n', encoding='utf-8')
        for argv in [
            ['add', 'README.md'],
            ['-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture'],
            ['worktree', 'add', '-q', '-b', 'codex/native-hook', str(native)],
        ]:
            result = subprocess.run(['git', *argv], cwd=control,
                                    capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (control / '.gsp').mkdir()
        (control / '.gsp/project.json').write_text(
            json.dumps({'id': 'native-hook-fixture', 'root': str(control)}),
            encoding='utf-8',
        )
        (native / 'src').mkdir()
        return control, native

    def test_native_linked_hook_discovery_uses_verified_control_root_readonly(self):
        import hashlib
        control, native = self.native_workspace()
        paths = [control / '.gsp/project.json', control / '.git/index',
                 control / '.git/HEAD', native / '.git']
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        for cwd in [native, native / 'src']:
            with self.subTest(cwd=cwd):
                self.assertEqual(hooks.locate(str(cwd)), control)
        self.assertEqual(before, {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in paths})
        self.assertEqual(list((control / '.gsp').iterdir()), [control / '.gsp/project.json'])
        self.assertFalse((native / '.gsp').exists())

    def test_native_linked_hook_discovery_respects_explicit_ceiling(self):
        from process_helpers import temporary_env
        _, native = self.native_workspace()
        with temporary_env({'GSP_CEILING_DIRECTORIES': str(native)}):
            self.assertIsNone(hooks.locate(str(native / 'src')))

    def test_copied_native_marker_cannot_bypass_edit_hook_discovery(self):
        _, native = self.native_workspace()
        copied = native.parent / 'copied'
        copied.mkdir()
        (copied / '.git').write_bytes((native / '.git').read_bytes())
        from gsp.runtime import WorkflowError
        with self.assertRaisesRegex(WorkflowError, 'CONTROL_ROOT_TOPOLOGY_'):
            hooks.locate(str(copied))
        result = hooks.handle({
            'cwd': str(copied), 'session_id': 'foreign-agent',
            'hook_event_name': 'PreToolUse', 'tool_name': 'apply_patch',
            'tool_input': {'command': '*** Begin Patch\n*** Add File: source.py\n+x\n*** End Patch'},
        })
        self.assertEqual(result.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
        self.assertIn('GSP metadata invalid', str(result))
        self.assertFalse((copied / 'source.py').exists())
        self.assertFalse((copied / '.gsp').exists())

    def test_split_native_marker_is_rejected_before_hook_root_selection(self):
        from gsp.runtime import Project, WorkflowError, find_project_root
        control, native = self.native_workspace()
        (native / '.gsp').mkdir()
        (native / '.gsp/project.json').write_text(
            json.dumps({'id': 'split-identity', 'root': str(native)}),
            encoding='utf-8',
        )
        paths = [control / '.gsp/project.json', native / '.gsp/project.json',
                 control / '.git/index', control / '.git/HEAD', native / '.git']
        before = {str(path): path.read_bytes() for path in paths}
        with self.assertRaisesRegex(WorkflowError, 'CONTROL_ROOT_SPLIT_AUTHORITY'):
            Project(native)
        for cwd in [native, native / 'src']:
            with self.subTest(cwd=cwd):
                with self.assertRaisesRegex(WorkflowError, 'CONTROL_ROOT_SPLIT_AUTHORITY'):
                    find_project_root(cwd, include_linked=True)
                with self.assertRaisesRegex(WorkflowError, 'CONTROL_ROOT_SPLIT_AUTHORITY'):
                    hooks.locate(str(cwd))
                reply = hooks.handle({
                    'cwd': str(cwd), 'agent_id': 'split-agent',
                    'hook_event_name': 'PreToolUse', 'tool_name': 'apply_patch',
                    'tool_input': {'command': '*** Begin Patch\n*** Add File: unsafe.py\n+x\n*** End Patch'},
                })
                self.assertEqual(reply.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
                self.assertFalse((cwd / 'unsafe.py').exists())
        self.assertEqual(before, {str(path): path.read_bytes() for path in paths})
        self.assertEqual(find_project_root(native), native)  # default legacy lookup is unchanged

    def test_nested_gsp_marker_cannot_hide_native_git_ownership(self):
        from gsp.runtime import Project, WorkflowError
        _, native = self.native_workspace()
        nested = native / 'src'
        (nested / '.gsp').mkdir()
        (nested / '.gsp/project.json').write_text(
            json.dumps({'id': 'nested-identity', 'root': str(nested)}),
            encoding='utf-8',
        )
        with self.assertRaises(WorkflowError):
            Project(nested)
        with self.assertRaises(WorkflowError):
            hooks.locate(str(nested))
        self.assertFalse((native / '.gsp').exists())

    def test_copied_git_and_gsp_markers_cannot_select_foreign_authority(self):
        from gsp.runtime import WorkflowError
        control, native = self.native_workspace()
        copied = native.parent / 'copied-with-identity'
        copied.mkdir()
        (copied / '.git').write_bytes((native / '.git').read_bytes())
        (copied / '.gsp').mkdir()
        (copied / '.gsp/project.json').write_bytes((control / '.gsp/project.json').read_bytes())
        before = (copied / '.gsp/project.json').read_bytes()
        with self.assertRaisesRegex(WorkflowError, 'CONTROL_ROOT_TOPOLOGY_'):
            hooks.locate(str(copied))
        self.assertEqual((copied / '.gsp/project.json').read_bytes(), before)

    def test_native_without_primary_identity_remains_noop_and_readonly(self):
        control, native = self.native_workspace()
        marker = control / '.gsp/project.json'
        marker.unlink()  # owned fixture only; represents a not-yet-initialized primary
        for cwd in [native, native / 'src']:
            with self.subTest(cwd=cwd):
                self.assertIsNone(hooks.locate(str(cwd)))
        self.assertFalse(marker.exists())
        self.assertFalse((native / '.gsp').exists())

    def test_legacy_scope_does_not_gate_owned_patch_paths(self):
        for text in ['*** Begin Patch\n*** Add File: src/a.py\n+x\n*** End Patch',
                     '*** Begin Patch\n*** Delete File: src/a.py\n*** End Patch',
                     '*** Begin Patch\n*** Add File: outside.py\n+x\n*** End Patch']:
            self.assertNotIn('permissionDecision',self.call('PreToolUse',tool_name='apply_patch',tool_input={'command':text}).get('hookSpecificOutput',{}))
        for text in ['*** Begin Patch\n*** Add File: src/../../escape.py\n+x\n*** End Patch',
                     '*** Begin Patch\n*** Update File: .gsp/project.json\n@@\n-x\n+y\n*** End Patch',
                     'garbage']:
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

    def test_hook_actual_agent_explicit_actor_mismatch(self):
        from gsp.identity_store import IdentityStore
        from gsp.runtime import Project, dispatch, parser
        from gsp.session_context import create_binding
        from gsp import sessions

        root = self.root/'identity-project'
        root.mkdir()
        subprocess.run(
            ['git', 'init', '-q', '-b', 'main'], cwd=root,
            check=True, capture_output=True,
        )
        subprocess.run(
            ['git', 'config', 'user.name', 'Hook Identity Test'], cwd=root,
            check=True, capture_output=True,
        )
        subprocess.run(
            ['git', 'config', 'user.email', 'hook-identity@example.invalid'],
            cwd=root, check=True, capture_output=True,
        )
        (root/'README.md').write_text('# hook identity\n', encoding='utf-8')
        subprocess.run(
            ['git', 'add', 'README.md'], cwd=root,
            check=True, capture_output=True,
        )
        subprocess.run(
            ['git', 'commit', '-qm', 'chore: baseline'], cwd=root,
            check=True, capture_output=True,
        )
        project = Project(root)
        project.initialize()
        dispatch(project, parser().parse_args([
            'new', 'work', '--title', 'Hook actor mismatch',
            '--intent', 'cross-session',
        ]))
        state = json.loads(
            (root/'.gsp/runs/work/state.json').read_text(encoding='utf-8')
        )
        state['git_topology'] = {
            'effective_mode': 'in-place',
            'branch': 'main',
            'workspace_root': str(root),
            'control_root': str(root),
            'intent': 'cross-session',
            'enforce': True,
        }
        (root/'.gsp/runs/work/state.json').write_text(
            json.dumps(state), encoding='utf-8',
        )
        binding = create_binding(
            IdentityStore(root), state['work_uuid'], 'main', 'codex',
            'main-thread',
            frozenset({
                'accept', 'brief', 'claim', 'dispatch', 'handoff',
                'plan', 'report', 'write',
            }),
        )
        context = root/binding.handle_path
        sessions.register_session(project, state, parser().parse_args([
            'session-register', 'work', '--role', 'controller',
            '--host', 'codex', '--thread', 'main-thread',
            '--write-mode', 'write', '--path', '.',
            '--context', str(context),
        ]))
        def run_hook(agent_id, host_session_id='parent-thread', explicit_id='S01'):
            return hooks.handle({
                'hook_event_name': 'PreToolUse',
                'tool_name': 'Write',
                'cwd': str(root),
                'session_id': host_session_id,
                'agent_id': agent_id,
                'gsp_session_id': explicit_id,
                'gsp_session_context': str(context),
                'tool_input': {'file_path': 'services/mismatch.py'},
            })

        matching = run_hook('main-thread')
        self.assertNotEqual(
            matching.get('hookSpecificOutput', {}).get('permissionDecision'),
            'deny',
            matching,
        )
        audit_text = ''.join(
            path.read_text(encoding='utf-8')
            for path in (root/'.gsp/audits/codex-hooks').rglob('*.json*')
        )
        self.assertIn('"outcome":"allowed"', audit_text)
        self.assertIn('"coverage":"session-direct-edit-paths"', audit_text)
        result = run_hook('foreign-thread')
        self.assertEqual(
            result.get('hookSpecificOutput', {}).get('permissionDecision'),
            'deny', result,
        )
        self.assertIn(
            'SESSION_ACTOR_MISMATCH',
            result.get('hookSpecificOutput', {}).get(
                'permissionDecisionReason', '',
            ),
        )
        conflicting = run_hook('main-thread', explicit_id='S99')
        self.assertIn(
            'SESSION_ACTOR_MISMATCH',
            conflicting['hookSpecificOutput']['permissionDecisionReason'],
        )
        unverified = hooks.handle({
            'hook_event_name': 'PreToolUse',
            'tool_name': 'Write',
            'cwd': str(root),
            'gsp_session_id': 'S01',
            'gsp_session_context': str(context),
            'tool_input': {'file_path': 'services/unverified.py'},
        })
        self.assertIn(
            'SESSION_ACTOR_UNVERIFIED',
            unverified['hookSpecificOutput']['permissionDecisionReason'],
        )

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
