"""Backward-compatible CLI facade; business services own their implementations.

Legacy monkeypatches are forwarded to the shared service bindings so old
callers/tests keep their behavior while domain imports no longer depend on CLI.
"""
import sys as _sys
from types import ModuleType as _ModuleType
from . import core as _core
from . import git_operations as _git_operations
from . import workflow_locks as _workflow_locks
from . import project_resolution as _project_resolution
from . import workflow_support as _workflow_support
from . import project as _project
from . import command_policies as _command_policies
from . import workflow_operations as _workflow_operations
from . import cli as _cli
from . import gate_rules as _gate_rules

_modules = [_core, _git_operations, _workflow_locks, _project_resolution, _workflow_support, _project, _command_policies, _workflow_operations, _cli, _gate_rules]
_exports = {'ACTION_EPHEMERAL_FIELDS': 4, 'ACTION_TIME_FIELDS': 4, 'Project': 5, 'ProjectLocation': 3, 'SESSION_CONTEXT_ACCEPT_COMMANDS': 6, 'SESSION_CONTEXT_BOUND_READ_COMMANDS': 6, 'SESSION_CONTEXT_DOCUMENT_WRITE_COMMANDS': 6, 'SESSION_CONTEXT_PLAN_COMMANDS': 6, 'SESSION_CONTEXT_PROJECT_COMMANDS': 6, 'SESSION_CONTEXT_READ_COMMANDS': 6, 'SESSION_CONTEXT_REPORT_COMMANDS': 6, 'SESSION_CONTEXT_TODO_WRITE_COMMANDS': 6, 'SESSION_CONTEXT_WRITE_COMMANDS': 6, 'SKIP_DIRS': 4, 'WorkflowError': 0, '_GIT_MUTATIONS': 4, '_WRITER_LOCK_GUARD': 2, '_WRITER_LOCK_OWNERS': 2, '_WRITER_REACQUIRE_SECONDS': 2, '_active_dispatch_worktrees': 7, '_advisory': 2, '_advisory_release': 2, '_assert_execution_control': 4, '_brief_control_root': 4, '_delivery_git_state': 4, '_discovery_ceilings': 1, '_dispatch': 7, '_emit_telemetry': 8, '_evidence_folder': 4, '_evidence_for_uncached': 4, '_execution_control_guard': 4, '_format_brief_state': 4, '_git_identity_uncached': 1, '_git_run': 1, '_github_user_upgrade': 7, '_hygiene_release': 4, '_identity_at': 4, '_independent_review_proof': 4, '_invoke_check_runner': 4, '_ledger_state_at': 4, '_load_cli_binding': 7, '_lock_paths': 2, '_measure_quality_source': 4, '_metadata_owned_by_current_process': 2, '_open_lock': 2, '_path_key': 4, '_preflight_advance': 7, '_preflight_close': 7, '_preflight_command_result': 7, '_preflight_ready': 7, '_preflight_review_gate': 7, '_preflight_session_actor': 7, '_quality_execution_window': 4, '_raise_for_unknown_execution': 4, '_raise_selection_error': 4, '_reacquire_writer_mutex': 2, '_require_binding_capabilities': 7, '_require_cli_write_context': 7, '_require_controller_acceptance': 7, '_require_document_context': 7, '_require_todo_context': 7, '_sequential_parent_group': 4, '_session_context_path': 7, '_stable_action_context': 4, '_stable_action_value': 4, '_stable_report_action_context': 4, '_stable_request_arguments': 4, '_stable_request_value': 4, '_user_update_check': 7, '_validate_delivery_uncached': 4, 'archive_delivery': 4, 'atomic': 0, 'cached_value': 0, 'command_input_hashes': 4, 'commit_number_warnings': 4, 'contract_hash': 4, 'digest': 0, 'dispatch': 7, 'evidence_entry': 4, 'evidence_for': 4, 'explicit_work_number_from_run_id': 4, 'find_project_root': 3, 'fingerprint': 4, 'git_identity': 1, 'head_branch': 1, 'head_observation_change': 4, 'health': 7, 'hook': 7, 'inside': 0, 'inspect_lock': 2, 'install_notice': 8, 'integration_dependencies': 4, 'invalidate_delivery': 4, 'is_ancestor': 1, 'locked': 2, 'main': 8, 'now': 0, 'parallel_required': 4, 'parser': 6, 'preflight_command': 7, 'process_token': 2, 'project_command_effects': 4, 'read': 0, 'recover_lock': 2, 'redact': 0, 'request_action_uid': 4, 'request_binding_context': 4, 'request_report_action_context': 4, 'retry_access': 0, 'revalidation_action_uid': 4, 'run_check': 4, 'select_evidence': 4, 'sha': 0, 'slug': 4, 'snapshot': 4, 'stable_action_uid': 4, 'suspend_writer_lock': 2, 'test_count': 4, 'topology_clean': 7, 'topology_required': 4, 'topology_selected': 4, 'validate_delivery': 4, 'validate_parallel_bindings': 4, 'verify_command_inputs': 4, 'visible_git_marker': 1}
_legacy_names = set(_exports) | {name for module in _modules for name in vars(module)
                               if not name.startswith('__')}
_targets = {name: tuple(module for module in _modules if name in vars(module))
            for name in _legacy_names}


class _CompatibilityModule(_ModuleType):
    def __getattr__(self, name):
        if name in _exports:
            return getattr(_modules[_exports[name]], name)
        for module in reversed(_modules):
            if name in vars(module):
                return vars(module)[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in _targets:
            for module in _targets[name]:
                setattr(module, name, value)
            return
        super().__setattr__(name, value)

    def __delattr__(self, name):
        if name in _targets:
            for module in _targets[name]:
                if name in vars(module):
                    delattr(module, name)
            return
        super().__delattr__(name)


__all__ = [name for name in _exports if not name.startswith('_')]
_sys.modules[__name__].__class__ = _CompatibilityModule

if __name__ == '__main__':
    raise SystemExit(_cli.main())
