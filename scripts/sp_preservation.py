"""Deterministic full-text SP preservation; no summarization or external source access."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re

PRIMARY = {
    'brainstorming': 'brainstorming', 'writing-plans': 'writing-plans',
    'execute': 'executing-plans', 'test-driven-development': 'test-driven-development',
    'systematic-debugging': 'systematic-debugging', 'review': 'requesting-code-review',
    'verify': 'verification-before-completion', 'ship': 'finishing-a-development-branch',
    'writing-skills': 'writing-skills', 'diagnosing-superpowers': 'diagnosing-superpowers',
}
EXTRA = {'execute': ['subagent-driven-development', 'dispatching-parallel-agents', 'using-git-worktrees'],
         'review': ['receiving-code-review']}
ALIASES = {'using-superpowers': 'workflow', **{v:k for k,v in PRIMARY.items()},
           'subagent-driven-development':'execute', 'dispatching-parallel-agents':'execute',
           'using-git-worktrees':'execute', 'receiving-code-review':'review'}
BEGIN = '<!-- sp-preserved-body:begin -->'
END = '<!-- sp-preserved-body:end -->'
PLATFORM_REFERENCE_NAMES = (
    'claude-code-tools.md', 'codex-tools.md', 'pi-tools.md',
    'antigravity-tools.md', 'hermes-tools.md', 'muse-tools.md',
)

def sha(data): return hashlib.sha256(data).hexdigest()

def resource(rel):
    p = Path(rel)
    return p.with_name('ORIGINAL.md') if p.name == 'SKILL.md' else p

def preserved(root, name):
    original = root/'sources/superpowers/skills'/name/'SKILL.md'
    body = original.read_text(encoding='utf-8').split('---',2)[2].lstrip('\n')
    target = root/'skills'/('gsp-'+ALIASES[name])
    def link(match):
        value = match.group(1)
        if value.startswith(('https://','http://','#')): return match.group(0)
        actual = original.parent/value
        assert actual.is_file(), (original,value)
        rel = actual.relative_to(root/'sources/superpowers')
        dest = root/'skills/gsp-workflow/upstream'/resource(rel)
        return ']('+os.path.relpath(dest,target).replace('\\','/')+')'
    return re.sub(r'\]\(([^)]+)\)',link,body)

def integration(name, lifecycle_notes=None):
    shared = ('## GstackSP adaptation (only the explicitly listed differences)\n\n'
        'Read the [SP adaptation contract](../gsp-workflow/references/sp-adapter.md) first. '
        'The complete upstream body follows, not a summary. Resolve upstream `superpowers:*` '
        'names using the contract; never load a second SP installation. Relative prompts/scripts '
        'are based on the corresponding ORIGINAL.md directory. Resource links below are redirected '
        'to the complete preserved files. Missing host capabilities must be reported honestly.\n\n')
    shared += ('Artifact layout is adapted, not the method: use the paths returned by GSP status/resume. '
               'New tasks live in docs/work/<year>/<month>/<stable-folder>/ (spec.md, plan.md and stage artifacts); '
               'existing bindings stay authoritative. Read [document governance](../gsp-workflow/references/document-governance.md) '
               'for saved ideas, module relations, discovery, health reports and controlled migration. '
               'Do not create a second spec or plan at the upstream default path.\n\n')
    shared += ('Use the [output and handoff contract](../gsp-workflow/references/output-handoff.md) '
               'for actual results, honestly attributed skills used, evidence, next action and owner. '
               'Return nested methods to the current task; continue authorized work without a redundant approval.\n\n')
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
            shared += f'- [{item}](../gsp-workflow/upstream/skills/{item}/ORIGINAL.md)\n'
        shared += '\n'
    if name in ('brainstorming', 'writing-plans', 'review', 'verify', 'ship'):
        shared += ('For document onboarding or governance, compare the original user objective with '
                   'the spec, plan and delivered artifacts; a narrower spec does not silently supersede that objective. '
                   'Use the [governance execution rules](../gsp-workflow/references/document-governance-execution.md). '
                   'Inventory or migration preview is not completed migration or acceptance. '
                   'Required unfinished work remains blocking unless the user actually changes the scope.\n\n')
    if name in ('brainstorming', 'writing-plans', 'execute', 'review', 'verify', 'ship'):
        shared += ('Before handing this stage onward, apply the [artifact fidelity checks]'
                   '(../gsp-workflow/references/artifact-fidelity.md) to the actual bound artifacts. '
                   'Read the complete upstream method below; this integration is not its replacement. '
                   'Plan authors use plan-check then seal-plan --check-structure after the upstream self-review. '
                   'Structural success, preserved source text and a skills-used table do not prove semantic compliance.\n\n')
        shared += ('Use the same artifact contract to route applicable engineering responsibilities from '
                   'design to tasks, evidence and current documentation: data/recovery, configuration/dependencies, '
                   'integration, project materials, delivery and maintenance. Expand only the topics triggered '
                   'by this change; no extra mandatory table, document set or skill sequence. '
                   'Headless plan workers return self-reviewed content; the controller owns registration and sealing.\n\n')
    if name in ('brainstorming', 'freeze', 'writing-plans', 'execute', 'review', 'verify', 'ship'):
        shared += ('Stage transitions use the unified controller: preview with `<gsp> handoff <id> --to <stage>` '
                   'and advance with `<gsp> advance <id> --to <stage>`. Read `next_skill` and pending gates; '
                   'do not treat chat text as a handoff or mutate workflow state around a failed advance.\n\n')
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
    baseline=json.loads((root/'sources/superpowers/baseline.json').read_text(encoding='utf-8'))
    for rel, expected in baseline['files'].items():
        data=(root/'sources/superpowers'/rel).read_bytes()
        assert sha(data)==expected, ('BASELINE_CHANGED',rel)
        dest=root/'skills/gsp-workflow/upstream'/resource(rel)
        dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data)
    (root/'skills/gsp-workflow/upstream/baseline.json').write_bytes((root/'sources/superpowers/baseline.json').read_bytes())
    for name, original in PRIMARY.items():
        file=root/'skills'/('gsp-'+name)/'SKILL.md'
        text=file.read_text(encoding='utf-8')
        front='---'+text.split('---',2)[1]+'---\n\n'
        file.write_text(front+integration(name,lifecycle_notes)+BEGIN+'\n'+preserved(root,original)+'\n'+END+'\n',encoding='utf-8',newline='\n')
    file=root/'skills/gsp-workflow/SKILL.md'
    text=file.read_text(encoding='utf-8').split('\n'+BEGIN)[0].rstrip()
    # Router is hand maintained. Its original SP foundation remains fully available.
    raw=(root/'sources/superpowers/skills/using-superpowers/SKILL.md').read_text(encoding='utf-8').split('---',2)[2].lstrip('\n')
    raw=adapt_workflow_platform_paths(root, raw)
    file.write_text(text+'\n'+BEGIN+'\n'+raw+'\n'+END+'\n',encoding='utf-8',newline='\n')
    return verify(root)

def verify(root):
    root=Path(root)
    baseline=json.loads((root/'sources/superpowers/baseline.json').read_text(encoding='utf-8'))
    for rel, expected in baseline['files'].items():
        assert sha((root/'sources/superpowers'/rel).read_bytes())==expected, ('BASELINE_CHANGED',rel)
        assert sha((root/'skills/gsp-workflow/upstream'/resource(rel)).read_bytes())==expected, ('RESOURCE_CHANGED',rel)
    for name, original in PRIMARY.items():
        text=(root/'skills'/('gsp-'+name)/'SKILL.md').read_text(encoding='utf-8')
        body=text.split(BEGIN+'\n',1)[1].rsplit('\n'+END,1)[0]
        assert body==preserved(root,original), ('BODY_CHANGED',name)
    raw=(root/'sources/superpowers/skills/using-superpowers/SKILL.md').read_text(encoding='utf-8').split('---',2)[2].lstrip('\n')
    raw=adapt_workflow_platform_paths(root, raw)
    text=(root/'skills/gsp-workflow/SKILL.md').read_text(encoding='utf-8')
    assert text.split(BEGIN+'\n',1)[1].rsplit('\n'+END,1)[0]==raw
    return {'version':baseline['version'],'source_files':len(baseline['files']),
            'upstream_skills':sum(p.endswith('/SKILL.md') for p in baseline['files']),
            'shortened_bodies':0,'baseline_and_resources':'PASS','body_changes':'relative Markdown resource links only'}
