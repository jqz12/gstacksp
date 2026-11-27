"""Bind existing architecture analysis to task inputs; never an edit allowlist."""
from pathlib import Path

from .architecture import RULE_VERSION, analyze_project
from .core import WorkflowError, digest, inside, read


def _local(root,relative):
    root=Path(root).resolve()
    if not isinstance(relative,str) or Path(relative).is_absolute():
        raise WorkflowError('ARCHITECTURE_PATH_ESCAPE')
    path=root/relative
    if not inside(root,path):
        raise WorkflowError('ARCHITECTURE_PATH_ESCAPE')
    for item in (path,*path.parents):
        if item==root:
            break
        if item.is_symlink() or getattr(item,'is_junction',lambda:False)():
            raise WorkflowError('ARCHITECTURE_PATH_ESCAPE')
    return path


def freeze_architecture_inputs(root,policy):
    if not isinstance(policy,dict) or not isinstance(policy.get('required',False),bool):
        raise WorkflowError('ARCHITECTURE_POLICY_INVALID')
    result=dict(policy)
    if not result.get('required'):
        return result
    baseline_path=_local(root,result.get('baseline'))
    if not baseline_path.is_file():
        raise WorkflowError('ARCHITECTURE_BASELINE_REQUIRED')
    actual=digest(baseline_path)
    if result.get('baseline_digest') not in (None,actual):
        raise WorkflowError('ARCHITECTURE_BASELINE_CHANGED explicit baseline approval required')
    baseline=read(baseline_path)
    if baseline.get('schema')!=1 or baseline.get('rule_version')!=RULE_VERSION:
        raise WorkflowError('ARCHITECTURE_BASELINE_VERSION')
    if not isinstance(baseline.get('modules'),dict) or not isinstance(baseline.get('rules'),dict):
        raise WorkflowError('ARCHITECTURE_BASELINE_INVALID')
    if not isinstance(result.get('scope',[]),list):
        raise WorkflowError('ARCHITECTURE_SCOPE_INVALID')
    result.update(baseline_digest=actual,rule_version=RULE_VERSION,
                  rules_digest=baseline['rules_digest'])
    return result


def validate_architecture_inputs(root,contract,*,source_root=None,actual_paths=(),phase='accept'):
    policy=contract.get('architecture')
    if not policy or not policy.get('required'):
        return {'allowed':True,'validation':'NOT_APPLICABLE','obligations':[]}
    frozen=freeze_architecture_inputs(root,policy)
    if any(policy.get(key)!=frozen.get(key)
           for key in ('baseline_digest','rule_version','rules_digest')):
        raise WorkflowError('ARCHITECTURE_INPUT_BINDING_REQUIRED')
    baseline=read(_local(root,policy['baseline']))
    source=Path(source_root or root).resolve()
    selected=list(policy.get('scope') or baseline['modules'])+list(actual_paths)
    paths=set()
    for relative in selected:
        candidate=_local(source,relative)
        if candidate.is_dir():
            for child in candidate.rglob('*'):
                rel=child.relative_to(source)
                if not {'.git','.gsp','target','build','node_modules','__pycache__'}.intersection(rel.parts) and child.is_file():
                    _local(source,rel.as_posix())
                    paths.add(rel.as_posix())
        else:
            paths.add(candidate.relative_to(source).as_posix())
    report=analyze_project(source,baseline=baseline,changed_paths=sorted(paths),rules=baseline['rules'])
    if not report['baseline_compatible']:
        raise WorkflowError('ARCHITECTURE_BASELINE_VERSION')
    strict=phase in {'accept','landing','close'}
    if report['introduced'] or report['coverage']['errors']:
        if strict:
            raise WorkflowError('ARCHITECTURE_CHANGE_BLOCKED')
        return {'allowed':True,'validation':'PENDING_QUALITY',
                'input_digest':report['input_digest'],'coverage':report['coverage'],
                'obligations':['ARCHITECTURE_CHANGE_BLOCKED_BEFORE_ACCEPTANCE']}
    unknown=set(report['coverage']['unknown']).intersection(report['coverage']['affected'])
    return {'allowed':not unknown or not strict,'validation':'MANUAL_REVIEW_REQUIRED' if unknown else 'CHECKED',
            'input_digest':report['input_digest'],'baseline_digest':policy['baseline_digest'],
            'coverage':report['coverage'],
            'obligations':['ARCHITECTURE_MANUAL_REVIEW_REQUIRED'] if unknown else []}
