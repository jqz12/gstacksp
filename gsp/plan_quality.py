"""Bounded structural checks, deliberately not semantic plan approval."""
import re

from .core import WorkflowError, read, sha
from .task_contracts import mask_fences

MAX_PLAN_BYTES = 1024 * 1024


def _prose(text):
    text = re.sub(r'<!--.*?(?:-->|\Z)', '', text, flags=re.S)
    text = mask_fences(text).replace('\r\n', '\n').replace('\r', '\n')
    return text.replace('**', '').replace('`', '')


def _meaningful(text):
    value = text.strip().strip('-').strip()
    return bool(value) and value.casefold() not in {'tbd', 'todo', '...', '待补充', '待定', 'n/a', '无'}


def _field(text, names):
    pattern = r'^[ \t]*(?:-[ \t]+)?(?:\[[ xX]\][ \t]+)?(?:' + '|'.join(names) + r')[ \t]*[:：][ \t]*([^\n]*)'
    return any(_meaningful(match[1]) for match in re.finditer(pattern, text, re.M | re.I))


def _section_body(text, names):
    pattern = r'^(#{1,6})[ \t]+(?:' + '|'.join(names) + r')[ \t]*[:：]?[ \t]*$'
    for match in re.finditer(pattern, text, re.M | re.I):
        rest = text[match.end():]
        end = re.search(r'^#{1,' + str(len(match[1])) + r'}[ \t]+', rest, re.M)
        body = rest[:end.start()] if end else rest
        body = re.sub(r'^#{1,6}[ \t]+[^\n]*$', '', body, flags=re.M)
        body = re.sub(r'^[ \t]*(?:[-*_][ \t]*){3,}$', '', body, flags=re.M)
        if _meaningful(body):
            return body
    return None


def _section(text, names):
    return _section_body(text, names) is not None


HYGIENE_TRIGGER = re.compile(
    r'(?i)\b(?:tests?|pytest|unittest|build|target|temp|temporary|worktree|incremental)\b'
    r'|CARGO_TARGET_DIR|临时目录|临时文件|构建产物|工作树|缓存')
HYGIENE_SECTION_NAMES = ['产物与临时文件生命周期', 'Product & Lifecycle',
                         'Artifacts and Lifecycle']
HYGIENE_FIELDS = [
    ['产物位置', '位置', 'Artifact location', 'Location', 'Output location'],
    ['回收责任', '清理责任', '回收', 'Cleanup owner', 'Owner', 'Ownership',
     'Reclaim responsibility', 'Cleanup'],
    ['保留与上限', '保留上限', '上限', 'Retention limits', 'Retention & limits',
     'Retention', 'Limits', 'Budget'],
    ['验收方式', '验收', 'Verification', 'Acceptance', 'Checks'],
]
STRUCTURED_WRITES = re.compile(
    r'^\s*(?:\*\*)?Writes(?:\*\*)?\s*:\s*(?:\*\*)?\s*\{[^\n]+\}',
    re.M | re.I,
)


def hygiene_needed(text: str) -> bool:
    """Plans that can create build or temporary output need a lifecycle section."""
    return bool(HYGIENE_TRIGGER.search(_prose(text)))


def hygiene_section(text: str) -> bool:
    """Require a real lifecycle section with all four declared fields."""
    body = _section_body(_prose(text), HYGIENE_SECTION_NAMES)
    if body is None:
        return False
    return all(_field(body, names) for names in HYGIENE_FIELDS)


def check_plan(
    text: str,
    task_ids: list[str],
    hygiene_blocking: bool = False,
    convergence_schema: int | None = None,
    *,
    state: dict | None = None,
) -> dict:
    """Check explicit SP-style structure; no claims about signatures or coverage."""
    from .convergence import plan_policies
    policy_error = None
    policies = {}
    try:
        policies = plan_policies(text)
    except ValueError as exc:
        policy_error = str(exc)
    text = _prose(text)
    headings = list(re.finditer(r'^(#{2,6})[ \t]+(?:(?:Task|任务)[ \t]+)?([\w-]+)(?:[ \t]*[:：]|[ \t]+)[^\n]*$', text, re.M | re.I))
    task_headings = [m for m in headings if m[2] in task_ids]
    header = text[:task_headings[0].start()] if task_headings else text
    findings = []
    def missing(code, detail, task=None, severity='blocking'):
        findings.append({'code': code, 'task': task, 'detail': detail,
                         'severity': severity})
    if policy_error:
        missing('TASK_SLICES_INVALID', policy_error)
    elif convergence_schema == 1:
        for task in task_ids:
            if task not in policies:
                missing(
                    'CONVERGENCE_TRIAGE_REQUIRED',
                    'new ledgers require ordered slices or an explicit single/small outcome',
                    task,
                )
    elif convergence_schema not in (None, 1):
        missing('CONVERGENCE_SCHEMA_UNSUPPORTED', str(convergence_schema))
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
    write_coverage = {
        'status': 'NOT_CHECKED',
        'reason': 'TASK_CONTRACT_CONTEXT_REQUIRED',
        'declared_tasks': [],
        'missing_tasks': list(task_ids),
        'edit_allowlist': False,
        'parallel_verified': False,
    }
    write_conflict_rows = []
    if state is not None and task_ids and all(
        any(row[2] == task for row in headings) for task in task_ids
    ):
        from .task_contracts import compile_task_contract, write_conflicts
        task_map = {
            row.get('id'): row
            for row in state.get('tasks', [])
            if isinstance(row, dict) and row.get('id')
        }
        try:
            contracts = [
                compile_task_contract(state, task, text)
                for task in task_ids
            ]
        except ValueError as exc:
            code = str(exc).split(' ', 1)[0] or 'TASK_CONTRACT_INVALID'
            missing(code, str(exc))
            write_coverage.update({
                'status': 'INCOMPLETE',
                'reason': str(exc),
            })
        else:
            legacy = all(row.get('legacy_plan') for row in contracts)
            declared = []
            missing_writes = []
            for contract in contracts:
                task = contract['task_id']
                metadata = task_map.get(task) or {}
                has_metadata = bool(
                    metadata.get('writes') or metadata.get('claimed_paths')
                )
                if STRUCTURED_WRITES.search(contract.get('task_body') or ''):
                    declared.append(task)
                elif has_metadata and contract.get('writes'):
                    declared.append(task)
                else:
                    missing_writes.append(task)
            if legacy:
                write_coverage.update({
                    'status': 'NOT_CHECKED',
                    'reason': 'LEGACY_PLAN_WRITE_COVERAGE_UNKNOWN',
                    'declared_tasks': [],
                    'missing_tasks': list(task_ids),
                    'parallel_verified': False,
                })
            else:
                try:
                    write_conflict_rows = write_conflicts(state, text)
                except ValueError as exc:
                    code = str(exc).split(' ', 1)[0] or 'WRITE_PATH_INVALID'
                    missing(code, str(exc))
                    write_coverage.update({
                        'status': 'INCOMPLETE',
                        'reason': str(exc),
                    })
                else:
                    for conflict in write_conflict_rows:
                        missing(
                            'WRITE_CONFLICT',
                            'unordered tasks claim overlapping writes: '
                            + ' <> '.join(conflict['paths']),
                            conflict['tasks'][0],
                        )
                    write_coverage.update({
                        'status': (
                            'COMPLETE' if not missing_writes else 'INCOMPLETE'
                        ),
                        'reason': (
                            'EXPLICIT_OR_METADATA_WRITES'
                            if not missing_writes
                            else 'WRITE_SET_MISSING_FOR_TASKS'
                        ),
                        'declared_tasks': declared,
                        'missing_tasks': missing_writes,
                        'parallel_verified': (
                            not missing_writes and not write_conflict_rows
                        ),
                    })
                    if missing_writes:
                        tasks_by_id = {
                            row['id']: row for row in state.get('tasks', [])
                            if isinstance(row, dict) and row.get('id')
                        }
                        def ancestors(task):
                            seen, pending = set(), list(
                                tasks_by_id.get(task, {}).get('depends', [])
                            )
                            while pending:
                                dep = pending.pop()
                                if dep in seen or dep not in tasks_by_id:
                                    continue
                                seen.add(dep)
                                pending.extend(
                                    tasks_by_id[dep].get('depends', [])
                                )
                            return seen
                        dependency_cache = {
                            task: ancestors(task) for task in task_ids
                        }
                        parallel_risk = any(
                            (
                                left in missing_writes
                                or right in missing_writes
                            )
                            and left not in dependency_cache[right]
                            and right not in dependency_cache[left]
                            for index, left in enumerate(task_ids)
                            for right in task_ids[index + 1:]
                        )
                        missing(
                            'WRITES_COVERAGE_INCOMPLETE',
                            'Tasks without explicit Writes or metadata write sets '
                            'cannot participate in a complete parallel-write check',
                            severity='blocking' if parallel_risk else 'warning',
                        )
    if hygiene_needed(text) and not hygiene_section(text):
        missing('HYGIENE_SECTION_MISSING',
                'Testing, build or worktree plans need 产物位置/回收责任/保留与上限/验收方式',
                severity='blocking' if hygiene_blocking else 'warning')
    return {'schema': 1,
            'structural_ok': not any(row['severity'] == 'blocking' for row in findings),
            'findings': findings,
            'semantic_review': 'NOT_CHECKED',
            'write_coverage': write_coverage,
            'write_conflicts': write_conflict_rows}


def write_plan_gate(state, text, *, parallel=False):
    """The same write facts serve seal, pre-dispatch and plan inspection."""
    size = (state.get('git_topology') or {}).get('size') or state.get('size')
    result = check_plan(text, [task['id'] for task in state.get('tasks', [])],
                        hygiene_blocking=size == 'large',
                        convergence_schema=state.get('convergence_schema'), state=state)
    prefixes = ('WRITE', 'WRITES', 'STRUCTURED_WRITES', 'TASK_CONTRACT')
    blockers = [row for row in result['findings'] if row['severity'] == 'blocking'
                and row['code'].startswith(prefixes)]
    coverage = result['write_coverage']
    if not parallel:
        # Missing declarations can still be processed serially; known overlap
        # or malformed declarations cannot be turned into parallel safety.
        blockers = [row for row in blockers if row['code'] != 'WRITES_COVERAGE_INCOMPLETE']
    elif not coverage.get('parallel_verified'):
        blockers.append({'code': 'WRITES_PARALLEL_UNVERIFIED', 'severity': 'blocking',
                         'detail': 'declare complete Writes and explicit ordering before parallel dispatch'})
    return {'blockers': blockers, 'coverage': coverage,
            'conflicts': result['write_conflicts'], 'analysis': result}


def read_plan_source(path):
    """Bound the source before decoding; return the exact bytes' revision."""
    with path.open('rb') as stream:
        raw = stream.read(MAX_PLAN_BYTES + 1)
    if len(raw) > MAX_PLAN_BYTES:
        raise WorkflowError('PLAN_TOO_LARGE limit is 1 MiB')
    return raw.decode('utf-8-sig'), sha(raw)


def inspect_plan(project, task_id: str) -> dict:
    """Read a bound plan without Project.state's invalidation or dashboard writes."""
    project.identity()
    run, _ = project.paths(task_id)
    state = read(run / 'state.json')
    path = project.document(state, 'plan')
    text, _ = read_plan_source(path)
    size = (state.get('git_topology') or {}).get('size') or state.get('size')
    result = check_plan(text, [task['id'] for task in state['tasks']],
                        hygiene_blocking=size == 'large',
                        convergence_schema=state.get('convergence_schema'),
                        state=state)
    return {**result, 'task': task_id, 'plan': path.relative_to(project.root).as_posix()}
