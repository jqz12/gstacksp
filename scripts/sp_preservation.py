"""Deterministic full-text SP preservation; no summarization or external source access."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re

from gsp import skill_catalog

PRIMARY = {
    'brainstorming': 'brainstorming', 'writing-plans': 'writing-plans',
    'execute': 'executing-plans', 'test-driven-development': 'test-driven-development',
    'systematic-debugging': 'systematic-debugging', 'review': 'requesting-code-review',
    'verify': 'verification-before-completion', 'ship': 'finishing-a-development-branch',
    'writing-skills': 'writing-skills', 'diagnosing-superpowers': 'diagnosing-superpowers',
}
EXTRA = {'execute': ['subagent-driven-development', 'dispatching-parallel-agents', 'using-git-worktrees'],
         'review': ['receiving-code-review']}
DISCOVERABLE_PRIMARY = {
    'brainstorming', 'writing-plans', 'execute', 'review', 'ship',
    'writing-skills', 'diagnosing-superpowers',
}
INTERNAL_PRIMARY = {'test-driven-development', 'systematic-debugging', 'verify'}
ALIASES = {'using-superpowers': 'workflow', **{v:k for k,v in PRIMARY.items()},
           'subagent-driven-development':'execute', 'dispatching-parallel-agents':'execute',
           'using-git-worktrees':'execute', 'receiving-code-review':'review'}
BEGIN = '<!-- sp-preserved-body:begin -->'
END = '<!-- sp-preserved-body:end -->'
PLATFORM_REFERENCE_NAMES = (
    'claude-code-tools.md', 'codex-tools.md', 'pi-tools.md',
    'antigravity-tools.md', 'hermes-tools.md', 'muse-tools.md',
)
CONVERGENCE_STAGE_REFERENCE = {
    'writing-plans': (
        '计划收敛遵循 [收敛与检查点契约](../gsp-workflow/references/convergence.md)：'
        '单一可验收 outcome，跨边界工作按有序切片组织。'
    ),
    'execute': (
        '执行收敛遵循 [收敛与检查点契约](../gsp-workflow/references/convergence.md)：'
        '45 分钟 checkpoint；90 分钟无 accepted slice 时停止扩展并由 Sol 裁决。'
    ),
    'review': (
        '审查收敛遵循 [收敛与检查点契约](../gsp-workflow/references/convergence.md)：'
        '固定当前 candidate/contract digest，并按阻断、后续归属、可选改善三分类；'
        'review.md 证据字段按该契约填写。'
    ),
}

def sha(data): return hashlib.sha256(data).hexdigest()

def resource(rel):
    p = Path(rel)
    return p.with_name('ORIGINAL.md') if p.name == 'SKILL.md' else p

def target(root, name, manifest=None):
    root=Path(root)
    if manifest is None:
        manifest=skill_catalog.load_manifest(root)
    if name in INTERNAL_PRIMARY:
        return root/skill_catalog.entry_for(manifest,name).internal_method_path
    if name in DISCOVERABLE_PRIMARY:
        return root/'skills'/('gsp-'+name)/'SKILL.md'
    raise KeyError(name)

def reference_prefix(file):
    return '../../' if 'methods' in Path(file).parts else '../gsp-workflow/'

def preserved(root, name, destination):
    original = root/'sources/superpowers/skills'/name/'SKILL.md'
    body = original.read_text(encoding='utf-8').split('---',2)[2].lstrip('\n')
    def link(match):
        value = match.group(1)
        if value.startswith(('https://','http://','#')): return match.group(0)
        actual = original.parent/value
        assert actual.is_file(), (original,value)
        rel = actual.relative_to(root/'sources/superpowers')
        dest = root/'skills/gsp-workflow/upstream'/resource(rel)
        return ']('+os.path.relpath(dest,Path(destination).parent).replace('\\','/')+')'
    return re.sub(r'\]\(([^)]+)\)',link,body)

def integration(name, destination, lifecycle_notes=None, manifest=None):
    prefix=reference_prefix(destination)
    shared = ''
    if name in INTERNAL_PRIMARY:
        full=skill_catalog.full_name(name)
        if manifest is None:
            raise ValueError('manifest is required for internal method integration')
        owner=skill_catalog.entry_for(manifest,name).stage_membership[0]
        shared += f'<!-- internal-method: {full} -->\n# {full}\n\n'
        shared += f'Owner: `{skill_catalog.full_name(owner)}`\n\n'
        shared += ('This is a conditional internal method resource, not a discoverable skill. '
                   'Load the complete method only when the trigger applies.\n\n')
    shared += ('## GstackSP adaptation (only the explicitly listed differences)\n\n'
        f'Read the [SP adaptation contract]({prefix}references/sp-adapter.md) first. '
        'The complete upstream body follows, not a summary. Resolve upstream `superpowers:*` '
        'names using the contract; never load a second SP installation. Relative prompts/scripts '
        'are based on the corresponding ORIGINAL.md directory. Resource links below are redirected '
        'to the complete preserved files. Missing host capabilities must be reported honestly.\n\n')
    shared += ('Artifact layout is adapted, not the method: use the paths returned by GSP status/resume. '
               'New tasks live in docs/work/<year>/<month>/<stable-folder>/ (spec.md, plan.md and stage artifacts); '
               f'existing bindings stay authoritative. Read [document governance]({prefix}references/document-governance.md) '
               'for saved ideas, module relations, discovery, health reports and controlled migration. '
               'Do not create a second spec or plan at the upstream default path.\n\n')
    shared += (f'Use the [output and handoff contract]({prefix}references/output-handoff.md) '
               'for actual results, honestly attributed skills used, evidence, next action and owner. '
               'Return nested methods to the current task; continue authorized work without a redundant approval.\n\n')
    shared += (f'Conditional methods are indexed in [methods]({prefix}references/methods.md). '
               'Only when the actual 触发条件 applies, read the complete method; do not load every method by default.\n\n')
    if lifecycle_notes and name in lifecycle_notes:
        shared += '### GSP stage integration\n\n' + lifecycle_notes[name] + '\n\n'
    if name in ('writing-plans', 'ship'):
        shared += ('Previously confirmed content and explicit execution/integration authorization take precedence '
                   'over the upstream repeated plan confirmation or fixed completion menu. '
                   'A substantive design change or a new external action still needs the applicable authorization.\n\n')
    if name == 'ship':
        shared += ('Before claiming an independent development/remediation task complete, actually check its current '
                   'status/resume and finish the final all regression, current review, verification and ready. Do not substitute a raw saved '
                   'ready flag, health report or a manual addition to an old delivery.md. Nested methods return '
                   'to the parent task; read-only reports do not require an artificial development task.\n\n')
        shared += ('Final task report: follow the gsp-ship final-summary section of the '
                   '[output and handoff contract](../gsp-workflow/references/output-handoff.md). '
                   'Use the exact heading 本轮技能执行顺序 and the fixed four-column table in that contract for both stage and final reports. '
                   'Include an ordered table of skills actually used, their actions and results, '
                   'including repair/review/reverification loops; distinguish reference-only material. '
                   'Show the ordered summary in the final reply and retain details in the existing delivery.md.\n\n')
    if EXTRA.get(name):
        shared += 'Read these complete originals and their dependencies before using the corresponding mode:\n\n'
        for item in EXTRA[name]:
            shared += f'- [{item}]({prefix}upstream/skills/{item}/ORIGINAL.md)\n'
        shared += '\n'
    if name in ('brainstorming', 'writing-plans', 'review', 'verify', 'ship'):
        shared += ('For document onboarding or governance, compare the original user objective with '
                   'the spec, plan and delivered artifacts; a narrower spec does not silently supersede that objective. '
                   f'Use the [governance execution rules]({prefix}references/document-governance-execution.md). '
                   'Inventory or migration preview is not completed migration or acceptance. '
                   'Required unfinished work remains blocking unless the user actually changes the scope.\n\n')
    if name in ('brainstorming', 'writing-plans', 'execute', 'review', 'verify', 'ship'):
        shared += ('Before handing this stage onward, apply the [artifact fidelity checks]'
                   f'({prefix}references/artifact-fidelity.md) to the actual bound artifacts. '
                   'Read the complete upstream method below; this integration is not its replacement. '
                   'Plan authors use plan-check then seal-plan --check-structure after the upstream self-review. '
                   'Structural success, preserved source text and a skills-used table do not prove semantic compliance.\n\n')
        shared += ('Use the same artifact contract to route applicable engineering responsibilities from '
                   'design to tasks, evidence and current documentation: data/recovery, configuration/dependencies, '
                   'integration, project materials, delivery and maintenance. Expand only the topics triggered '
                   'by this change; no extra mandatory table, document set or skill sequence. '
                   'Headless plan workers return self-reviewed content; the controller owns registration and sealing.\n\n')
    shared += (f'Stage transitions follow [stage-transitions]({prefix}references/stage-transitions.md); '
               'a routine advance is one business operation and active preview is optional.\n\n')
    shared += (f'Cross-session and recovery follow [cross-session]({prefix}references/cross-session.md). '
               'Read it only when that mode or recovery is active.\n\n')
    if name in CONVERGENCE_STAGE_REFERENCE:
        shared += CONVERGENCE_STAGE_REFERENCE[name] + '\n\n'
    return shared


def adapt_workflow_platform_paths(root, raw):
    """Make preserved platform references resolve from gsp-workflow/SKILL.md."""
    for name in PLATFORM_REFERENCE_NAMES:
        raw = raw.replace(f'`references/{name}`', f'`upstream/skills/using-superpowers/references/{name}`')
    return raw

def build(root, lifecycle_notes=None):
    if lifecycle_notes is None:
        # Standalone preservation builds must use the same maintained notes as
        # build_skills, including when root is a temporary source snapshot.
        spec = importlib.util.spec_from_file_location('gsp_skill_definitions', Path(__file__).with_name('build_skills.py'))
        definitions = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(definitions)
        lifecycle_notes = definitions.LIFECYCLE_NOTES
    root=Path(root)
    manifest=skill_catalog.load_manifest(root)
    baseline=json.loads((root/'sources/superpowers/baseline.json').read_text(encoding='utf-8'))
    for rel, expected in baseline['files'].items():
        data=(root/'sources/superpowers'/rel).read_bytes()
        assert sha(data)==expected, ('BASELINE_CHANGED',rel)
        dest=root/'skills/gsp-workflow/upstream'/resource(rel)
        dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data)
    (root/'skills/gsp-workflow/upstream/baseline.json').write_bytes((root/'sources/superpowers/baseline.json').read_bytes())
    for name, original in PRIMARY.items():
        file=target(root,name,manifest)
        file.parent.mkdir(parents=True,exist_ok=True)
        if name in DISCOVERABLE_PRIMARY:
            text=file.read_text(encoding='utf-8')
            front='---'+text.split('---',2)[1]+'---\n\n'
        else:
            front=''
        file.write_text(front+integration(name,file,lifecycle_notes,manifest)+BEGIN+'\n'+preserved(root,original,file)+'\n'+END+'\n',encoding='utf-8',newline='\n')
    file=root/'skills/gsp-workflow/SKILL.md'
    text=file.read_text(encoding='utf-8').split('\n'+BEGIN)[0].rstrip()
    # Router is hand maintained. Its original SP foundation remains fully available.
    raw=(root/'sources/superpowers/skills/using-superpowers/SKILL.md').read_text(encoding='utf-8').split('---',2)[2].lstrip('\n')
    raw=adapt_workflow_platform_paths(root, raw)
    file.write_text(text+'\n'+BEGIN+'\n'+raw+'\n'+END+'\n',encoding='utf-8',newline='\n')
    return verify(root)

def verify(root):
    root=Path(root)
    manifest=skill_catalog.load_manifest(root)
    baseline=json.loads((root/'sources/superpowers/baseline.json').read_text(encoding='utf-8'))
    for rel, expected in baseline['files'].items():
        assert sha((root/'sources/superpowers'/rel).read_bytes())==expected, ('BASELINE_CHANGED',rel)
        assert sha((root/'skills/gsp-workflow/upstream'/resource(rel)).read_bytes())==expected, ('RESOURCE_CHANGED',rel)
    for name, original in PRIMARY.items():
        file=target(root,name,manifest)
        text=file.read_text(encoding='utf-8')
        body=text.split(BEGIN+'\n',1)[1].rsplit('\n'+END,1)[0]
        assert body==preserved(root,original,file), ('BODY_CHANGED',name)
        if name in INTERNAL_PRIMARY:
            full=skill_catalog.full_name(name)
            assert f'<!-- internal-method: {full} -->' in text, ('METHOD_IDENTITY_CHANGED',name)
    raw=(root/'sources/superpowers/skills/using-superpowers/SKILL.md').read_text(encoding='utf-8').split('---',2)[2].lstrip('\n')
    raw=adapt_workflow_platform_paths(root, raw)
    text=(root/'skills/gsp-workflow/SKILL.md').read_text(encoding='utf-8')
    assert text.split(BEGIN+'\n',1)[1].rsplit('\n'+END,1)[0]==raw
    return {'version':baseline['version'],'source_files':len(baseline['files']),
            'upstream_skills':sum(p.endswith('/SKILL.md') for p in baseline['files']),
            'shortened_bodies':0,'baseline_and_resources':'PASS','body_changes':'relative Markdown resource links only'}
