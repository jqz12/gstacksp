from dataclasses import replace
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gsp.git_topology import (
    WorkspaceExpectation,
    branch_name,
    probe,
    recommend,
    validate_branch_name,
    verify_registered_native_workspace,
)
from gsp.orchestration_types import RequestRef, WorktreeRequest, digest_value
from gsp.runtime import WorkflowError


class GitTopologyTests(unittest.TestCase):
    def repo(self):
        temp = tempfile.TemporaryDirectory(prefix='gsp-topology-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=root, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Topology Test'], cwd=root, check=True)
        subprocess.run(['git', 'config', 'user.email', 'topology@example.invalid'], cwd=root, check=True)
        (root / 'README.md').write_text('# topology\n', encoding='utf-8')
        subprocess.run(['git', 'add', 'README.md'], cwd=root, check=True)
        subprocess.run(['git', 'commit', '-qm', 'chore: baseline'], cwd=root, check=True)
        return root

    def git(self, root, *args):
        return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True).stdout

    def test_probe_reports_current_branch_worktree_and_clean_status(self):
        root = self.repo()

        result = probe(root)

        self.assertEqual(result['status'], 'clean')
        self.assertEqual(result['current']['branch'], 'main')
        self.assertEqual(result['current']['root'], str(root.resolve()))
        self.assertEqual(len(result['worktrees']), 1)
        self.assertEqual(result['worktrees'][0]['path'], str(root.resolve()))
        self.assertEqual(result['active_tasks'], [])
        self.assertEqual(result['risks'], [])

    def test_probe_reports_dirty_and_untracked_paths(self):
        root = self.repo()
        (root / 'README.md').write_text('changed\n', encoding='utf-8')
        (root / 'new.txt').write_text('new\n', encoding='utf-8')

        result = probe(root)

        self.assertEqual(result['status'], 'needs-decision')
        self.assertIn('GIT_DIRTY', {item['code'] for item in result['risks']})
        self.assertEqual(result['current']['changed_paths'], ['README.md', 'new.txt'])

    def test_probe_preserves_unicode_spaces_and_rename_paths(self):
        root = self.repo()
        self.git(root, 'mv', 'README.md', '中文 文档.md')
        (root / '箭头 → 文件.txt').write_text('new', encoding='utf-8')
        self.assertEqual(probe(root)['current']['changed_paths'],
                         ['README.md', '中文 文档.md', '箭头 → 文件.txt'])

    def test_git_missing_and_worktree_probe_failure_are_explicit(self):
        root = self.repo()
        with patch('gsp.git_topology.subprocess.run', side_effect=FileNotFoundError('git')):
            self.assertIn('GIT_UNAVAILABLE', {x['code'] for x in probe(root)['risks']})
        from gsp import git_topology
        original = git_topology._run
        def fail_worktrees(path, *args):
            if args[:2] == ('worktree', 'list'):
                return subprocess.CompletedProcess(args, 128, '', 'fixture worktree failure')
            return original(path, *args)
        with patch.object(git_topology, '_run', side_effect=fail_worktrees):
            result = probe(root)
        self.assertIn('GIT_PROBE_FAILED', {x['code'] for x in result['risks']})
        self.assertEqual(result['status'], 'blocked')

    def test_historical_states_are_visible_without_claiming_live_workspaces(self):
        root = self.repo()
        result = probe(root, active_tasks=[
            {'id': 'old-a', 'status': 'ready', 'git': {'root': str(root), 'branch': 'main'}},
            {'id': 'old-b', 'status': 'needs-verification', 'git': {'root': str(root), 'branch': 'main'}},
        ])
        self.assertEqual(len(result['active_tasks']), 2)
        self.assertNotIn('GIT_WORKTREE_CONFLICT', {x['code'] for x in result['risks']})

    def test_cross_session_single_in_place_retains_writer_claim(self):
        root = self.repo()
        state = {'id': 'continued', 'tasks': [{'id': 'T01', 'role': 'implementation'}],
                 'git_topology': {'intent': 'cross-session', 'effective_mode': 'in-place',
                                  'branch': 'main', 'workspace_root': str(root)}}
        result = probe(root, active_tasks=[state])
        self.assertTrue(result['active_tasks'][0]['claiming'])

    def test_probe_discovers_child_bindings_and_all_local_branches(self):
        root = self.repo()
        self.git(root, 'branch', 'feature/GSP-000018-中文分支')
        binding = {'effective_mode': 'worktree', 'workspace_root': str(root), 'branch': 'main'}
        result = probe(root, active_tasks=[{'id': 'parent', 'tasks': [
            {'id': 'T01', 'role': 'backend', 'topology': binding},
            {'id': 'T02', 'role': 'frontend', 'topology': binding},
        ]}])
        self.assertIn('GIT_WORKTREE_CONFLICT', {x['code'] for x in result['risks']})
        self.assertIn('feature/GSP-000018-中文分支', [x['name'] for x in result['branches']])

    def test_probe_reports_detached_head_as_blocking(self):
        root = self.repo()
        self.git(root, 'checkout', '--detach', 'HEAD')

        result = probe(root)

        self.assertIsNone(result['current']['branch'])
        finding = next(item for item in result['risks'] if item['code'] == 'GIT_DETACHED_HEAD')
        self.assertEqual(finding['severity'], 'blocking')

    def test_probe_reports_multiple_worktrees_and_bound_conflicts(self):
        root = self.repo()
        other = root.parent / (root.name + '-other')
        self.git(root, 'worktree', 'add', '-b', 'feature/other', str(other))
        self.addCleanup(lambda: subprocess.run(['git', 'worktree', 'remove', '--force', str(other)], cwd=root))

        result = probe(root, active_tasks=[
            {'id': 'run-a', 'git_topology': {'effective_mode': 'worktree', 'branch': 'feature/other', 'workspace_root': str(other)}},
            {'id': 'run-b', 'git_topology': {'effective_mode': 'worktree', 'branch': 'feature/other', 'workspace_root': str(other)}},
        ])

        self.assertEqual(len(result['worktrees']), 2)
        codes = {item['code'] for item in result['risks']}
        self.assertIn('GIT_WORKTREE_CONFLICT', codes)

    def test_registered_native_workspace_accepts_exact_external_common_dir(self):
        control = self.repo()
        project_uid = 'project-native-fixture'
        work_uid = 'work-native-fixture'
        task_uid = 'task-native-fixture'
        owner = 'local'
        owner_adapter = 'codex-native-worktree'
        common_dir = (control / '.git').resolve()
        base = self.git(control, 'rev-parse', 'HEAD').strip()
        branch = 'codex/GSP-000034-T05-fixture'
        source = control.parent / (control.name + '-native')
        self.git(control, 'worktree', 'add', '-b', branch, str(source), base)
        self.addCleanup(
            lambda: subprocess.run(
                ['git', 'worktree', 'remove', '--force', str(source)],
                cwd=control, capture_output=True,
            )
        )
        marker = control / '.gsp' / 'project.json'
        marker.parent.mkdir(parents=True)
        marker.write_text(
            '{"id":"%s","schema":1}\n' % project_uid,
            encoding='utf-8',
        )
        request_ref = RequestRef(
            request_uid='native-request-fixture',
            logical_key='worktree-create-fixture',
            input_digest=digest_value({
                'project_uid': project_uid,
                'work_uid': work_uid,
                'base': base,
                'branch': branch,
            }),
            project_uid=project_uid,
            work_uid=work_uid,
            attempt_uid=None,
            operation_kind='native-worktree-create',
        )
        request = WorktreeRequest(
            request=request_ref,
            owner_adapter=owner_adapter,
            source_root=str(control.resolve()),
            base_sha=base,
            branch=branch,
            path_hint=str(source.resolve()),
        )
        receipt = {
            'schema': 1,
            'kind': 'observed-native-tool-receipts',
            'request_uid': request_ref.request_uid,
            'operation_id': 'native-operation-fixture',
            'creation_status': {
                'status': 'fulfilled',
                'value': {
                    'isError': False,
                    'content': [{
                        'type': 'text',
                        'text': (
                            '{"operationId":"native-operation-fixture",'
                            '"stage":"completed","result":{"type":"created",'
                            '"worktreeGitRoot":"%s",'
                            '"worktreeWorkspaceRoot":"%s",'
                            '"registrationError":null}}'
                        ) % (str(source.resolve()).replace('\\', '\\\\'),
                             str(source.resolve()).replace('\\', '\\\\')),
                    }],
                },
            },
            'attachments_after_creation': {
                'status': 'fulfilled',
                'value': {
                    'isError': False,
                    'content': [{
                        'type': 'text',
                        'text': (
                            '{"attachments":[{"id":"native-attachment-fixture",'
                            '"attachmentType":"worktree","identityKey":"%s",'
                            '"payload":{"hostId":"%s","root":"%s",'
                            '"workspaceRoot":"%s","sourceCwd":"%s"}}]}'
                        ) % (
                            str(source.resolve()).replace('\\', '\\\\'),
                            owner,
                            str(source.resolve()).replace('\\', '\\\\'),
                            str(source.resolve()).replace('\\', '\\\\'),
                            str(control.resolve()).replace('\\', '\\\\'),
                        ),
                    }],
                },
            },
        }
        expected = WorkspaceExpectation(
            project_uid=project_uid,
            work_uid=work_uid,
            task_uid=task_uid,
            owner=owner,
            owner_adapter=owner_adapter,
            base_sha=base,
            head_sha=base,
            branch=branch,
            request_uid=request_ref.request_uid,
            request_digest=digest_value(request.to_dict()),
            receipt_digest=digest_value(receipt),
            control_root=str(control.resolve()),
            source_root=str(source.resolve()),
            common_dir=str(common_dir),
        )

        proof = verify_registered_native_workspace(
            control, source, request, receipt, expected,
        )

        self.assertEqual(proof.project_uid, project_uid)
        self.assertEqual(proof.work_uid, work_uid)
        self.assertEqual(proof.task_uid, task_uid)
        self.assertEqual(proof.owner, owner)
        self.assertEqual(proof.head, base)
        self.assertEqual(proof.branch, branch)
        self.assertTrue(proof.registered)
        self.assertEqual(proof.source_root, str(source.resolve()))
        self.assertEqual(proof.common_dir, str(common_dir))
        self.assertEqual(proof.request_digest, expected.request_digest)
        self.assertEqual(proof.receipt_digest, expected.receipt_digest)

    def native_workspace_fixture(
        self, suffix='fixture', work_uid='work-native-fixture',
        task_uid='task-native-fixture',
    ):
        control = self.repo()
        project_uid = 'project-native-' + suffix
        owner = 'local'
        owner_adapter = 'codex-native-worktree'
        common_dir = (control / '.git').resolve()
        base = self.git(control, 'rev-parse', 'HEAD').strip()
        branch = 'codex/GSP-000034-T05-' + suffix
        source = control.parent / (control.name + '-' + suffix)
        self.git(control, 'worktree', 'add', '-b', branch, str(source), base)
        self.addCleanup(
            lambda: subprocess.run(
                ['git', 'worktree', 'remove', '--force', str(source)],
                cwd=control, capture_output=True,
            )
        )
        marker = control / '.gsp/project.json'
        marker.parent.mkdir(parents=True)
        marker.write_text(
            json.dumps({'id': project_uid, 'schema': 1}),
            encoding='utf-8',
        )
        request_ref = RequestRef(
            request_uid='native-request-' + suffix,
            logical_key='worktree-create-' + suffix,
            input_digest=digest_value({
                'project_uid': project_uid,
                'work_uid': work_uid,
                'base': base,
                'branch': branch,
            }),
            project_uid=project_uid,
            work_uid=work_uid,
            attempt_uid=None,
            operation_kind='native-worktree-create',
        )
        request = WorktreeRequest(
            request=request_ref,
            owner_adapter=owner_adapter,
            source_root=str(control.resolve()),
            base_sha=base,
            branch=branch,
            path_hint=str(source.resolve()),
        )
        operation_id = 'native-operation-' + suffix
        attachment_id = 'native-attachment-' + suffix

        def native_payload(value):
            return {
                'status': 'fulfilled',
                'value': {
                    'isError': False,
                    'content': [{
                        'type': 'text',
                        'text': json.dumps(value),
                    }],
                },
            }

        receipt = {
            'schema': 1,
            'kind': 'observed-native-tool-receipts',
            'request_uid': request_ref.request_uid,
            'operation_id': operation_id,
            'creation_status': native_payload({
                'operationId': operation_id,
                'stage': 'completed',
                'result': {
                    'type': 'created',
                    'worktreeGitRoot': str(source.resolve()),
                    'worktreeWorkspaceRoot': str(source.resolve()),
                    'registrationError': None,
                },
            }),
            'attachments_after_creation': native_payload({
                'attachments': [{
                    'id': attachment_id,
                    'attachmentType': 'worktree',
                    'identityKey': str(source.resolve()),
                    'payload': {
                        'hostId': owner,
                        'root': str(source.resolve()),
                        'workspaceRoot': str(source.resolve()),
                        'sourceCwd': str(control.resolve()),
                    },
                }],
            }),
        }
        expected = WorkspaceExpectation(
            project_uid=project_uid,
            work_uid=work_uid,
            task_uid=task_uid,
            owner=owner,
            owner_adapter=owner_adapter,
            base_sha=base,
            head_sha=base,
            branch=branch,
            request_uid=request_ref.request_uid,
            request_digest=digest_value(request.to_dict()),
            receipt_digest=digest_value(receipt),
            control_root=str(control.resolve()),
            source_root=str(source.resolve()),
            common_dir=str(common_dir),
        )
        return {
            'control': control,
            'source': source,
            'request': request,
            'receipt': receipt,
            'expected': expected,
            'project_uid': project_uid,
            'common_dir': common_dir,
        }

    def test_registered_native_workspace_can_prove_explicit_legacy_identity(self):
        fixture = self.native_workspace_fixture(
            suffix='legacy', work_uid=None, task_uid=None,
        )

        proof = verify_registered_native_workspace(
            fixture['control'], fixture['source'], fixture['request'],
            fixture['receipt'], fixture['expected'],
        )

        self.assertIsNone(proof.work_uid)
        self.assertIsNone(proof.task_uid)

    def test_registered_native_workspace_rejects_copied_marker_without_registration(self):
        fixture = self.native_workspace_fixture(suffix='copied-marker')
        copied = fixture['source'].parent / (fixture['source'].name + '-copy')
        copied.mkdir()
        self.addCleanup(
            lambda: shutil.rmtree(copied) if copied.exists() else None
        )
        (copied / '.gsp').mkdir(parents=True)
        (copied / '.gsp/project.json').write_text(
            json.dumps({'id': fixture['project_uid'], 'schema': 1}),
            encoding='utf-8',
        )
        request = replace(
            fixture['request'], path_hint=str(copied.resolve()),
        )
        expected = replace(
            fixture['expected'],
            source_root=str(copied.resolve()),
            request_digest=digest_value(request.to_dict()),
        )

        with self.assertRaisesRegex(
            WorkflowError, 'NATIVE_WORKSPACE_UNREGISTERED',
        ):
            verify_registered_native_workspace(
                fixture['control'], copied, request,
                fixture['receipt'], expected,
            )

    def test_registered_native_workspace_rejects_other_repository(self):
        fixture = self.native_workspace_fixture(suffix='other-repo')
        foreign_control = self.repo()
        foreign_source = foreign_control.parent / (
            foreign_control.name + '-foreign-linked'
        )
        foreign_branch = 'codex/GSP-000034-T05-foreign'
        foreign_base = self.git(
            foreign_control, 'rev-parse', 'HEAD',
        ).strip()
        self.git(
            foreign_control, 'worktree', 'add', '-b', foreign_branch,
            str(foreign_source), foreign_base,
        )
        self.addCleanup(
            lambda: subprocess.run(
                ['git', 'worktree', 'remove', '--force', str(foreign_source)],
                cwd=foreign_control, capture_output=True,
            )
        )
        foreign_marker = foreign_control / '.gsp/project.json'
        foreign_marker.parent.mkdir(parents=True)
        foreign_marker.write_text(
            json.dumps({'id': fixture['project_uid'], 'schema': 1}),
            encoding='utf-8',
        )
        request = replace(
            fixture['request'],
            path_hint=str(foreign_source.resolve()),
        )
        expected = replace(
            fixture['expected'],
            source_root=str(foreign_source.resolve()),
            request_digest=digest_value(request.to_dict()),
        )

        with self.assertRaisesRegex(
            WorkflowError, 'NATIVE_COMMON_DIR_MISMATCH',
        ):
            verify_registered_native_workspace(
                fixture['control'], foreign_source, request,
                fixture['receipt'], expected,
            )

    def test_registered_native_workspace_rejects_alias_or_escape(self):
        fixture = self.native_workspace_fixture(suffix='alias')
        aliased = fixture['source'] / '..' / fixture['source'].name
        expected = replace(
            fixture['expected'], source_root=str(aliased),
        )

        with self.assertRaisesRegex(
            WorkflowError, 'NATIVE_SOURCE_ROOT_ALIAS_OR_ESCAPE',
        ):
            verify_registered_native_workspace(
                fixture['control'], fixture['source'], fixture['request'],
                fixture['receipt'], expected,
            )

    def test_registered_native_workspace_rejects_dirty_checkout(self):
        fixture = self.native_workspace_fixture(suffix='dirty')
        (fixture['source'] / 'dirty.txt').write_text(
            'dirty\n', encoding='utf-8',
        )

        with self.assertRaisesRegex(
            WorkflowError, 'NATIVE_WORKSPACE_DIRTY',
        ):
            verify_registered_native_workspace(
                fixture['control'], fixture['source'], fixture['request'],
                fixture['receipt'], fixture['expected'],
            )

    def test_registered_native_workspace_can_report_explicit_dirty_facts(self):
        fixture = self.native_workspace_fixture(suffix='dirty-allowed')
        (fixture['source'] / 'dirty.txt').write_text(
            'dirty\n', encoding='utf-8',
        )

        proof = verify_registered_native_workspace(
            fixture['control'], fixture['source'], fixture['request'],
            fixture['receipt'], fixture['expected'], allow_dirty=True,
        )

        self.assertTrue(proof.registered)
        self.assertFalse(proof.clean)
        self.assertEqual(proof.project_uid, fixture['project_uid'])

    def test_registered_native_workspace_allow_dirty_keeps_integrity_checks(self):
        fixture = self.native_workspace_fixture(suffix='dirty-integrity')
        (fixture['source'] / 'dirty.txt').write_text(
            'dirty\n', encoding='utf-8',
        )

        with self.subTest(field='wrong-head'):
            expected = replace(
                fixture['expected'], head_sha='0' * 40,
            )
            with self.assertRaisesRegex(
                WorkflowError, 'NATIVE_WORKSPACE_HEAD_MISMATCH',
            ):
                verify_registered_native_workspace(
                    fixture['control'], fixture['source'],
                    fixture['request'], fixture['receipt'], expected,
                    allow_dirty=True,
                )

        with self.subTest(field='wrong-request'):
            request = replace(
                fixture['request'], branch='codex/foreign-branch',
            )
            with self.assertRaisesRegex(
                WorkflowError, 'NATIVE_REQUEST_CHANGED',
            ):
                verify_registered_native_workspace(
                    fixture['control'], fixture['source'], request,
                    fixture['receipt'], fixture['expected'],
                    allow_dirty=True,
                )

        with self.subTest(field='receipt-request'):
            receipt = json.loads(json.dumps(fixture['receipt']))
            receipt['request_uid'] = 'foreign-request'
            expected = replace(
                fixture['expected'],
                receipt_digest=digest_value(receipt),
            )
            with self.assertRaisesRegex(
                WorkflowError, 'NATIVE_RECEIPT_REQUEST_MISMATCH',
            ):
                verify_registered_native_workspace(
                    fixture['control'], fixture['source'],
                    fixture['request'], receipt, expected,
                    allow_dirty=True,
                )

        with self.subTest(field='receipt-digest'):
            expected = replace(
                fixture['expected'], receipt_digest='0' * 64,
            )
            with self.assertRaisesRegex(
                WorkflowError, 'NATIVE_RECEIPT_DIGEST_MISMATCH',
            ):
                verify_registered_native_workspace(
                    fixture['control'], fixture['source'],
                    fixture['request'], fixture['receipt'], expected,
                    allow_dirty=True,
                )

    def test_registered_native_workspace_allow_dirty_rejects_foreign_owner(self):
        fixture = self.native_workspace_fixture(suffix='dirty-owner')
        (fixture['source'] / 'dirty.txt').write_text(
            'dirty\n', encoding='utf-8',
        )
        receipt = json.loads(json.dumps(fixture['receipt']))
        content = receipt['attachments_after_creation']['value']['content'][0]
        payload = json.loads(content['text'])
        payload['attachments'][0]['payload']['hostId'] = 'foreign-owner'
        content['text'] = json.dumps(payload)
        expected = replace(
            fixture['expected'], receipt_digest=digest_value(receipt),
        )

        with self.assertRaisesRegex(
            WorkflowError, 'NATIVE_WORKSPACE_OWNER_MISMATCH',
        ):
            verify_registered_native_workspace(
                fixture['control'], fixture['source'], fixture['request'],
                receipt, expected, allow_dirty=True,
            )

    def test_registered_native_workspace_allow_dirty_rejects_copied_marker(self):
        fixture = self.native_workspace_fixture(suffix='dirty-copied')
        copied = fixture['source'].parent / (
            fixture['source'].name + '-copy'
        )
        copied.mkdir()
        self.addCleanup(
            lambda: shutil.rmtree(copied) if copied.exists() else None
        )
        (copied / '.gsp').mkdir(parents=True)
        (copied / '.gsp/project.json').write_text(
            json.dumps({'id': fixture['project_uid'], 'schema': 1}),
            encoding='utf-8',
        )
        request = replace(
            fixture['request'], path_hint=str(copied.resolve()),
        )
        expected = replace(
            fixture['expected'],
            source_root=str(copied.resolve()),
            request_digest=digest_value(request.to_dict()),
        )

        with self.assertRaisesRegex(
            WorkflowError, 'NATIVE_WORKSPACE_UNREGISTERED',
        ):
            verify_registered_native_workspace(
                fixture['control'], copied, request, fixture['receipt'],
                expected, allow_dirty=True,
            )

    def test_registered_native_workspace_rejects_non_bool_allow_dirty(self):
        fixture = self.native_workspace_fixture(suffix='dirty-mode-invalid')

        for value in (1, 0, 'true', None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    WorkflowError, 'NATIVE_ALLOW_DIRTY_INVALID',
                ):
                    verify_registered_native_workspace(
                        fixture['control'], fixture['source'],
                        fixture['request'], fixture['receipt'],
                        fixture['expected'], allow_dirty=value,
                    )

    def test_registered_native_workspace_rejects_receipt_owner_and_head_mismatch(self):
        fixture = self.native_workspace_fixture(suffix='mismatch')
        original_receipt = fixture['receipt']

        with self.subTest(field='owner'):
            receipt = json.loads(json.dumps(original_receipt))
            content = receipt[
                'attachments_after_creation'
            ]['value']['content'][0]
            payload = json.loads(content['text'])
            payload['attachments'][0]['payload']['hostId'] = 'foreign-owner'
            content['text'] = json.dumps(payload)
            expected = replace(
                fixture['expected'],
                receipt_digest=digest_value(receipt),
            )
            with self.assertRaisesRegex(
                WorkflowError, 'NATIVE_WORKSPACE_OWNER_MISMATCH',
            ):
                verify_registered_native_workspace(
                    fixture['control'], fixture['source'],
                    fixture['request'], receipt, expected,
                )

        with self.subTest(field='head'):
            expected = replace(
                fixture['expected'], head_sha='0' * 40,
            )
            with self.assertRaisesRegex(
                WorkflowError, 'NATIVE_WORKSPACE_HEAD_MISMATCH',
            ):
                verify_registered_native_workspace(
                    fixture['control'], fixture['source'],
                    fixture['request'], original_receipt, expected,
                )

        with self.subTest(field='receipt_request'):
            receipt = json.loads(json.dumps(original_receipt))
            receipt['request_uid'] = 'foreign-request'
            expected = replace(
                fixture['expected'],
                receipt_digest=digest_value(receipt),
            )
            with self.assertRaisesRegex(
                WorkflowError, 'NATIVE_RECEIPT_REQUEST_MISMATCH',
            ):
                verify_registered_native_workspace(
                    fixture['control'], fixture['source'],
                    fixture['request'], receipt, expected,
                )

    def test_detached_native_checkout_needs_authorized_branch_before_write(self):
        control = self.repo()
        project_uid = 'project-native-detached'
        owner = 'local'
        owner_adapter = 'codex-native-worktree'
        common_dir = (control / '.git').resolve()
        base = self.git(control, 'rev-parse', 'HEAD').strip()
        branch = 'codex/GSP-000034-T05-detached'
        source = control.parent / (control.name + '-detached')
        self.git(control, 'worktree', 'add', '-b', branch, str(source), base)
        self.addCleanup(
            lambda: subprocess.run(
                ['git', 'worktree', 'remove', '--force', str(source)],
                cwd=control, capture_output=True,
            )
        )
        self.git(source, 'checkout', '--detach', base)
        marker = control / '.gsp/project.json'
        marker.parent.mkdir(parents=True)
        marker.write_text(
            '{"id":"%s","schema":1}\n' % project_uid,
            encoding='utf-8',
        )
        request_ref = RequestRef(
            request_uid='native-detached-request',
            logical_key='native-detached',
            input_digest=digest_value({'branch': branch}),
            project_uid=project_uid,
            work_uid='work-native-detached',
            attempt_uid=None,
            operation_kind='native-worktree-create',
        )
        request = WorktreeRequest(
            request=request_ref,
            owner_adapter=owner_adapter,
            source_root=str(control.resolve()),
            base_sha=base,
            branch=branch,
            path_hint=str(source.resolve()),
        )
        receipt = {
            'schema': 1,
            'kind': 'observed-native-tool-receipts',
            'request_uid': request_ref.request_uid,
            'operation_id': 'native-detached-operation',
            'creation_status': {
                'status': 'fulfilled',
                'value': {'isError': False, 'content': []},
            },
            'attachments_after_creation': {
                'status': 'fulfilled',
                'value': {'isError': False, 'content': []},
            },
        }
        expected = WorkspaceExpectation(
            project_uid=project_uid,
            work_uid=request_ref.work_uid,
            task_uid=None,
            owner=owner,
            owner_adapter=owner_adapter,
            base_sha=base,
            head_sha=base,
            branch=branch,
            request_uid=request_ref.request_uid,
            request_digest=digest_value(request.to_dict()),
            receipt_digest=digest_value(receipt),
            control_root=str(control.resolve()),
            source_root=str(source.resolve()),
            common_dir=str(common_dir),
        )

        with self.assertRaisesRegex(
            WorkflowError, 'NATIVE_WORKSPACE_DETACHED',
        ):
            verify_registered_native_workspace(
                control, source, request, receipt, expected,
            )

    def test_pending_inventory_is_not_a_writer_claim(self):
        root = self.repo()
        state = {'id': 'pending', 'git_topology': {'status': 'pending', 'branch': 'main', 'workspace_root': str(root)}}
        self.assertFalse(probe(root, active_tasks=[state])['active_tasks'][0]['claiming'])

    def test_recommendation_chooses_worktree_for_parallel_or_dirty_large_work(self):
        clean = self.repo()
        clean_probe = probe(clean)
        recommendation = recommend(clean_probe, {'number': 'GSP-000018', 'title': '大功能', 'intent': 'parallel-multi-task'})
        self.assertEqual(recommendation['mode'], 'worktree')

        self.assertTrue(any('并行' in reason for reason in recommendation['reasons']))

        (clean / 'dirty.txt').write_text('dirty\n', encoding='utf-8')
        dirty_probe = probe(clean)
        dirty_recommendation = recommend(dirty_probe, {'number': 'GSP-000018', 'title': '小修复', 'intent': 'single-task'})
        self.assertEqual(dirty_recommendation['mode'], 'worktree')
        self.assertTrue(any('脏' in reason for reason in dirty_recommendation['reasons']))

    def test_large_single_task_branch_is_enough_without_independent_verification(self):
        result = recommend(probe(self.repo()), {'number': 'GSP-000018', 'title': '跨模块重构',
                                               'intent': 'single-task', 'size': 'large'})
        self.assertEqual(result['mode'], 'branch')
        self.assertTrue(any('规模' in reason for reason in result['reasons']))

    def test_branch_name_keeps_gsp_and_task_number_and_chinese_topic(self):
        name = branch_name('GSP-000018', 'T02', '后端接口')
        self.assertEqual(name, 'feature/GSP-000018-T02-后端接口')
        validate_branch_name(name)

    def test_branch_name_rejects_unsafe_names(self):
        for invalid in ('feature/GSP 000018', 'feature//GSP-000018', 'feature/GSP-000018\\x', 'feature/GSP-000018\n',
                        'feature/GSP-000018-other/child', 'feature/GSP-000018-bad..name',
                        'feature/GSP-000018-bad?name', 'feature/GSP-0000187-wrong', 'feature/GSP-xyz-title'):
            with self.subTest(invalid=invalid):
                with self.assertRaises(WorkflowError):
                    validate_branch_name(invalid)

    def test_probe_non_repository_returns_explicit_blocking_error(self):
        with tempfile.TemporaryDirectory(prefix='gsp-no-git-') as folder:
            result = probe(Path(folder))
        self.assertEqual(result['status'], 'not-a-repository')
        self.assertEqual(result['risks'][0]['code'], 'GIT_NOT_REPOSITORY')
        self.assertEqual(result['risks'][0]['severity'], 'blocking')


if __name__ == '__main__':
    unittest.main()
