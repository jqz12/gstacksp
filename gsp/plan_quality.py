"""Bounded structural checks, deliberately not semantic plan approval."""
import re

from .runtime import WorkflowError, read

MAX_PLAN_BYTES = 1024 * 1024


def _prose(text):
    text = re.sub(r'<!--.*?(?:-->|\Z)', '', text, flags=re.S)
    lines = []
    fence = None
    for line in text.splitlines():
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not line[marker.end():].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
            continue
        lines.append(line.replace('**', '').replace('`', ''))
    return '\n'.join(lines)


def _meaningful(text):
    value = text.strip().strip('-').strip()
    return bool(value) and value.casefold() not in {'tbd', 'todo', '...', '待补充', '待定', 'n/a', '无'}


def _field(text, names):
    pattern = r'^[ \t]*(?:-[ \t]+)?(?:\[[ xX]\][ \t]+)?(?:' + '|'.join(names) + r')[ \t]*[:：][ \t]*([^\n]*)'
    return any(_meaningful(match[1]) for match in re.finditer(pattern, text, re.M | re.I))


def _section(text, names):
    pattern = r'^(#{1,6})[ \t]+(?:' + '|'.join(names) + r')[ \t]*[:：]?[ \t]*$'
    for match in re.finditer(pattern, text, re.M | re.I):
        rest = text[match.end():]
        end = re.search(r'^#{1,' + str(len(match[1])) + r'}[ \t]+', rest, re.M)
        body = rest[:end.start()] if end else rest
        body = re.sub(r'^#{1,6}[ \t]+[^\n]*$', '', body, flags=re.M)
        body = re.sub(r'^[ \t]*(?:[-*_][ \t]*){3,}$', '', body, flags=re.M)
        if _meaningful(body):
            return True
    return False


def check_plan(text: str, task_ids: list[str]) -> dict:
    """Check explicit SP-style structure; no claims about signatures or coverage."""
    text = _prose(text)
    headings = list(re.finditer(r'^(#{2,6})[ \t]+(?:(?:Task|任务)[ \t]+)?([\w-]+)(?:[ \t]*[:：]|[ \t]+)[^\n]*$', text, re.M | re.I))
    task_headings = [m for m in headings if m[2] in task_ids]
    header = text[:task_headings[0].start()] if task_headings else text
    findings = []
    def missing(code, detail, task=None):
        findings.append({'code': code, 'task': task, 'detail': detail})
    for code, labels in [('GOAL', ['Goal', '目标']), ('ARCHITECTURE', ['Architecture', '架构']),
                         ('TECH_STACK', ['Tech Stack', '技术栈']), ('SPEC', ['Spec', '规格'])]:
        if not _field(header, labels):
            missing(code + '_MISSING', 'Missing nonempty field: ' + labels[0])
    for code, labels in [('GLOBAL_CONSTRAINTS', ['Global Constraints', '全局约束']),
                         ('REVIEW_FOCUS', ['Review Focus', '审查重点']),
                         ('SELF_REVIEW', ['Self-Review', '自查'])]:
        if not _section(text, labels):
            missing(code + '_MISSING', 'Missing nonempty section: ' + labels[0])
    if not task_ids:
        missing('TASKS_MISSING', 'No registered tasks to compare')
    for task in task_ids:
        matches = [m for m in headings if m[2] == task]
        if not matches:
            missing('TASK_MISSING', 'No heading for registered task', task)
            continue
        if len(matches) != 1:
            missing('TASK_DUPLICATE', 'More than one heading for registered task', task)
        match = matches[0]
        rest = text[match.end():]
        end = re.search(r'^#{1,' + str(len(match[1])) + r'}[ \t]+', rest, re.M)
        boundaries = [len(rest)] + ([end.start()] if end else [])
        boundaries += [other.start() - match.end() for other in task_headings if other.start() > match.start()]
        block = rest[:min(boundaries)]
        for code, labels in [('FILES', ['Files', '文件']), ('CONSUMES', ['Consumes', '消费']),
                             ('PRODUCES', ['Produces', '产出']), ('RUN', ['Run', '运行']),
                             ('EXPECTED', ['Expected', '预期'])]:
            if not _field(block, labels):
                # Files are commonly a label followed by a Markdown list.
                files_list = code == 'FILES' and re.search(r'^(?:Files|文件)[ \t]*[:：][ \t]*\n[ \t]*-[ \t]+\S', block, re.M | re.I)
                if not files_list:
                    missing(code + '_MISSING', 'Missing task field: ' + labels[0], task)
        if not re.search(r'^(?:Interfaces|接口)[ \t]*[:：]', block, re.M | re.I):
            missing('INTERFACES_MISSING', 'Missing task Interfaces label', task)
        if not re.search(r'^[ \t]*-[ \t]+\[[ xX]\][ \t]+\S', block, re.M):
            missing('STEPS_MISSING', 'Missing checkbox steps', task)
    return {'schema': 1, 'structural_ok': not findings, 'findings': findings,
            'semantic_review': 'NOT_CHECKED'}


def inspect_plan(project, task_id: str) -> dict:
    """Read a bound plan without Project.state's invalidation or dashboard writes."""
    project.identity()
    run, _ = project.paths(task_id)
    state = read(run / 'state.json')
    path = project.document(state, 'plan')
    with path.open('rb') as stream:
        raw = stream.read(MAX_PLAN_BYTES + 1)
    if len(raw) > MAX_PLAN_BYTES:
        raise WorkflowError('PLAN_TOO_LARGE limit is 1 MiB')
    result = check_plan(raw.decode('utf-8-sig'), [task['id'] for task in state['tasks']])
    return {**result, 'task': task_id, 'plan': path.relative_to(project.root).as_posix()}
