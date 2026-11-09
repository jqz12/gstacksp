"""Deterministic full-text SP preservation; no summarization or external source access."""
import hashlib
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

def integration(name):
    shared = ('## GstackSP adaptation (only the explicitly listed differences)\n\n'
        'Read the [SP adaptation contract](../gsp-workflow/references/sp-adapter.md) first. '
        'The complete upstream body follows, not a summary. Resolve upstream `superpowers:*` '
        'names using the contract; never load a second SP installation. Relative prompts/scripts '
        'are based on the corresponding ORIGINAL.md directory. Resource links below are redirected '
        'to the complete preserved files. Missing host capabilities must be reported honestly.\n\n')
    shared += ('Artifact layout is adapted, not the method: use the paths returned by GSP status/resume. '
               'New tasks live in docs/work/<year>/<stable-folder>/ (spec.md, plan.md and stage artifacts); '
               'existing bindings stay authoritative. Read [document governance](../gsp-workflow/references/document-governance.md) '
               'for saved ideas, module relations, discovery, health reports and controlled migration. '
               'Do not create a second spec or plan at the upstream default path.\n\n')
    if EXTRA.get(name):
        shared += 'Read these complete originals and their dependencies before using the corresponding mode:\n\n'
        for item in EXTRA[name]:
            shared += f'- [{item}](../gsp-workflow/upstream/skills/{item}/ORIGINAL.md)\n'
        shared += '\n'
    return shared

def build(root):
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
        file.write_text(front+integration(name)+BEGIN+'\n'+preserved(root,original)+'\n'+END+'\n',encoding='utf-8',newline='\n')
    file=root/'skills/gsp-workflow/SKILL.md'
    text=file.read_text(encoding='utf-8').split('\n'+BEGIN)[0].rstrip()
    # Router is hand maintained. Its original SP foundation remains fully available.
    raw=(root/'sources/superpowers/skills/using-superpowers/SKILL.md').read_text(encoding='utf-8').split('---',2)[2].lstrip('\n')
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
    text=(root/'skills/gsp-workflow/SKILL.md').read_text(encoding='utf-8')
    assert text.split(BEGIN+'\n',1)[1].rsplit('\n'+END,1)[0]==raw
    return {'version':baseline['version'],'source_files':len(baseline['files']),
            'upstream_skills':sum(p.endswith('/SKILL.md') for p in baseline['files']),
            'shortened_bodies':0,'baseline_and_resources':'PASS','body_changes':'relative Markdown resource links only'}
