import copy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gsp.core import WorkflowError
from gsp.session_context import _legacy_controller_recovery
from gsp.session_context import issue_cli_binding


class LegacyControllerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.registry = {
            'controller_session': 'S01',
            'sessions': [{'id': 'S01', 'role': 'controller', 'status': 'active'}],
            'workspace_leases': {'workspace': 'S01'},
        }

    def test_old_controller_with_lease_is_recoverable_without_mutation(self):
        original = copy.deepcopy(self.registry)
        result = _legacy_controller_recovery(self.registry, None, 'main_chat', 'resume')
        self.assertEqual(result['id'], 'S01')
        self.assertEqual(self.registry, original)

    def test_rejects_authoritative_owner_and_projected_binding(self):
        for owner in ({'binding_uid': 'real'}, None):
            with self.subTest(owner=owner):
                if owner is None:
                    self.registry['sessions'][0]['binding_uid'] = 'real'
                with self.assertRaisesRegex(WorkflowError, 'RECOVERY_BOUND'):
                    _legacy_controller_recovery(self.registry, owner, 'main_chat', 'resume')

    def test_rejects_other_active_session_or_lease(self):
        for other in ('session', 'lease'):
            registry = copy.deepcopy(self.registry)
            if other == 'session':
                registry['sessions'].append({'id': 'S02', 'status': 'active'})
            else:
                registry['workspace_leases']['other'] = 'S02'
            with self.assertRaisesRegex(WorkflowError, 'RECOVERY_OTHER_SESSION'):
                _legacy_controller_recovery(registry, None, 'main_chat', 'resume')

    def test_requires_reason_controller_kind_and_existing_controller(self):
        for kind, reason in [('helper', 'resume'), ('main_chat', '')]:
            with self.assertRaises(WorkflowError):
                _legacy_controller_recovery(self.registry, None, kind, reason)
        self.registry['controller_session'] = None
        with self.assertRaisesRegex(WorkflowError, 'RECOVERY_NOT_LEGACY'):
            _legacy_controller_recovery(self.registry, None, 'main_chat', 'resume')

    def test_registry_save_failure_cannot_publish_new_owner(self):
        project = SimpleNamespace(root=Path.cwd())
        args = SimpleNamespace(kind='main_chat', work_uid='work', host='codex',
                               thread=None, context_out=str(Path.cwd() / 'context.json'),
                               recover_legacy_controller=True, reason='resume')
        with patch('gsp.session_context._state_for_work', return_value={'work_uuid': 'work'}), \
             patch('gsp.sessions.load_registry', return_value=self.registry), \
             patch('gsp.session_context.IdentityStore') as store, \
             patch('gsp.sessions._save', side_effect=OSError('injected save failure')), \
             patch('gsp.session_context._create_binding') as create:
            store.return_value.read_work_controller.return_value = None
            with self.assertRaisesRegex(OSError, 'injected'):
                issue_cli_binding(project, args)
            create.assert_not_called()

    def test_signing_failure_leaves_retryable_projection_and_original_snapshot(self):
        original = copy.deepcopy(self.registry['sessions'][0])
        project = SimpleNamespace(root=Path.cwd())
        args = SimpleNamespace(kind='main_chat', work_uid='work', host='codex',
                               thread=None, context_out=str(Path.cwd() / 'context.json'),
                               recover_legacy_controller=True, reason='resume')
        def interrupted_signing(*unused, **kwargs):
            self.assertEqual(self.registry['workspace_leases'], {})
            self.assertEqual(self.registry['sessions'][0]['context_mode'], 'legacy-read-only')
            raise OSError('injected signing interruption')
        with patch('gsp.session_context._state_for_work', return_value={'work_uuid': 'work'}), \
             patch('gsp.sessions.load_registry', return_value=self.registry), \
             patch('gsp.session_context.IdentityStore') as store, \
             patch('gsp.sessions._save'), \
             patch('gsp.session_context._create_binding', side_effect=interrupted_signing):
            store.return_value.read_work_controller.return_value = None
            for attempt in range(2):
                with self.assertRaisesRegex(OSError, 'injected signing'):
                    issue_cli_binding(project, args)
            self.assertEqual(self.registry['sessions'][0]['legacy_recovery_snapshot'], original)
