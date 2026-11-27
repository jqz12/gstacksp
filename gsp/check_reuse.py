"""Match a final run to the same explicitly closed quality requirement."""
from pathlib import Path

from .code_quality import (bound_quality_phase, load_stack, quality_contract,
                           quality_identity)
from .core import digest


def declared_run_requirement(project, state, argv, source_root, environment,
                             source_fingerprint, *, task_id, kind):
    tasks = state.get('tasks') or []
    if task_id != 'all' or not tasks or any(row.get('status') != 'done' for row in tasks):
        return None
    source = Path(source_root).resolve()
    stack = load_stack(project.root, source_root=source)
    if stack['source'] == 'missing':
        return None
    for check in stack['checks']:
        if (not check['reuse_evidence'] or not check['inputs']
                or 'release' not in check['modes'] or check['argv'] != list(argv)
                or (source / check['cwd']).resolve() != source
                or (kind == 'tests') != (check['kind'] == 'test')):
            continue
        identity = quality_identity(check, source_root=source,
            source_fingerprint=source_fingerprint, contract=quality_contract(project, state),
            phase=bound_quality_phase(state), environment=environment,
            configuration_digest=digest(Path(project.root) / stack['source']))
        if identity is not None:
            return {'identity': identity, 'check': check, 'timeout_s': check['timeout_s']}
    return None
