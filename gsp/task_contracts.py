"""One self-contained task contract; writes describe scheduling, not an edit fence."""
import hashlib
import json
import re


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _text_digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def mask_fences(text):
    """Blank fenced code lines while preserving their line boundaries."""
    lines, fence = [], None
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        line_break = line[len(line.rstrip('\r\n')):]
        if fence is not None:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not line[marker.end():].strip()
            ):
                fence = None
            lines.append(line_break)
            continue
        if marker:
            fence = marker[1]
            lines.append(line_break)
            continue
        lines.append(line)
    return ''.join(lines)


_CONTENT_KEYS = (
    'schema',
    'project_uid',
    'work_uid',
    'task_id',
    'task_body',
    'global_constraints',
    'specification',
    'acceptance',
    'dependencies',
    'writes',
    'legacy_plan',
    'model_alias',
    'effort',
)


def _content_payload(contract):
    result={key: contract.get(key) for key in _CONTENT_KEYS}
    if 'architecture' in contract:
        result['architecture']=contract['architecture']
    return result


def _sections(plan):
    """Preserve original text while ignoring headings inside fenced examples."""
    headings, offset, fence = [], 0, None
    for line in plan.splitlines(keepends=True):
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
        elif fence is None:
            heading = re.match(r'^(#{1,6})\s+', line)
            match = re.match(r'^#{2,6}\s+(?:(?:Task|任务)\s+)?(T\d+)\s*[:：\s]', line)
            if heading:
                headings.append((offset, len(heading[1]), match[1] if match else None))
        offset += len(line)
    sections, globals_, current = {}, [], None
    global_start = 0
    for offset, level, task in headings:
        if current is not None and (task is not None or level <= current[2]):
            sections[current[0]] = plan[current[1]:offset].strip()
            current = None
            global_start = offset
        if task is not None:
            if task in sections or current is not None and task == current[0]:
                raise ValueError('TASK_CONTRACT_AMBIGUOUS ' + task)
            globals_.append(plan[global_start:offset])
            current = (task, offset, level)
    if current is not None:
        sections[current[0]] = plan[current[1]:].strip()
    else:
        globals_.append(plan[global_start:])
    return '\n'.join(part.strip() for part in globals_ if part.strip()), sections


def canonical_write_path(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('WRITE_PATH_INVALID')
    path = value.replace('\\', '/').strip()
    parts = path.split('/')
    if (path.startswith('/') or ':' in path or '\0' in path or
            any(part == '..' or part.endswith((' ', '.')) and part != '.' for part in parts)):
        raise ValueError('WRITE_PATH_INVALID ' + value)
    path = '/'.join(part for part in parts if part not in ('', '.'))
    if not path:
        raise ValueError('WRITE_PATH_INVALID ' + value)
    return path


def _writes(body, task):
    match = re.search(
        r'^\s*(?:\*\*)?Writes(?:\*\*)?\s*:\s*(?:\*\*)?\s*(\{[^\n]+\})',
        mask_fences(body),
        re.M | re.I,
    )
    if match:
        try:
            value = json.loads(match[1])
        except ValueError as exc:
            raise ValueError('STRUCTURED_WRITES_INVALID') from exc
        paths = value.get('paths')
    else:
        paths = task.get('writes', task.get('claimed_paths', []))
    if not isinstance(paths, (list, tuple)):
        raise ValueError('STRUCTURED_WRITES_INVALID')
    return sorted(set(canonical_write_path(path) for path in paths))


def compile_task_contract(state, task_id, plan, spec='', source_paths=None):
    task = next((row for row in state.get('tasks', []) if row['id'] == task_id), None)
    if task is None:
        raise ValueError('TASK_CONTRACT_TASK_UNKNOWN ' + task_id)
    global_text, sections = _sections(plan)
    if sections and task_id not in sections:
        raise ValueError('TASK_CONTRACT_TASK_MISSING ' + task_id)
    body = sections.get(task_id) or ('# ' + task_id + ': ' + task.get('title', '') +
                                    '\n\n' + str(task.get('acceptance', '')))
    dependencies = {}
    task_map = {row['id']: row for row in state.get('tasks', [])}
    pending = list(task.get('depends', []))
    while pending:
        dep = pending.pop()
        if dep in dependencies:
            continue
        if dep == task_id or dep not in task_map:
            raise ValueError('TASK_CONTRACT_DEPENDENCY_INVALID ' + dep)
        row = task_map[dep]
        dependencies[dep] = {'acceptance': row.get('acceptance'),
                             'interfaces': row.get('interfaces') or sections.get(dep, '')}
        pending.extend(row.get('depends', []))
    payload = {
        'schema': 1, 'project_uid': state.get('project_id'),
        'work_uid': state.get('work_uuid') or state.get('id'), 'task_id': task_id,
        'task_body': body, 'global_constraints': global_text, 'specification': spec,
        'acceptance': task.get('acceptance'), 'dependencies': dependencies,
        'source_refs': {
            key: {'path': str(path), 'digest': _text_digest(
                spec if key == 'design' else plan
            )}
            for key, path in sorted((source_paths or {}).items())
            if key in {'design', 'plan'}
        },
        'writes': _writes(body, task), 'legacy_plan': not bool(sections),
        'model_alias': 'group/deepseek-2', 'effort': 'max',
    }
    if state.get('architecture_policy') is not None:
        payload['architecture']=json.loads(json.dumps(state['architecture_policy']))
    content_digest = _digest(_content_payload(payload))
    contract_uid = _digest({**payload, 'content_digest': content_digest})
    return {
        **payload,
        'contract_uid': contract_uid,
        'content_digest': content_digest,
    }


def render_task_contract(contract, state, task, dispatch):
    """Render the sole worker-facing brief, prompt and complete contract."""
    dependencies = json.dumps(
        contract.get('dependencies') or {}, ensure_ascii=False,
        indent=2, sort_keys=True,
    )
    sources = json.dumps(
        contract.get('source_refs') or {}, ensure_ascii=False,
        indent=2, sort_keys=True,
    )
    lines = [
        f"# {contract['task_id']} 完整任务契约", '',
        '本文件同时是完整任务契约、子会话 brief 和启动 prompt；不要生成或查找第二份正文。', '',
        '## 契约身份', '',
        f"- GSP：{state.get('number') or state['id']}",
        f"- work_uid：{contract.get('work_uid')}",
        f"- task：{contract['task_id']} {task.get('title') or ''}".rstrip(),
        f"- contract_uid：{contract.get('contract_uid')}",
        f"- content_digest：{contract.get('content_digest')}",
        f"- role=worker；model_alias={contract.get('model_alias')}；effort={contract.get('effort')}",
        f"- provisional session：{dispatch.get('session_hint') or '由主会话登记'}",
        f"- platform={dispatch.get('platform')}；dispatch_mode={dispatch.get('dispatch_mode')}", '',
        '## 本任务正文', '', contract.get('task_body') or '—', '',
        '## 适用全局约束', '',
        contract.get('global_constraints') or '无额外全局约束。', '',
        '## 完整适用规格', '',
        contract.get('specification') or '来源规格未提供。', '',
        '## 全部依赖', '', '```json', dependencies, '```', '',
        '## 来源引用与摘要', '', '```json', sources, '```', '',
        '## 调度写集', '',
        'Writes 仅用于并行冲突调度和归属核验，不是目录编辑白名单。',
        *[f'- {path}' for path in contract.get('writes') or []], '',
        '## 架构边界与验证义务', '', '```json',
        json.dumps(contract.get('architecture') or {'required':False},ensure_ascii=False,indent=2),
        '```', '',
        '## 派发上下文', '',
        f"- branch：{dispatch.get('branch') or '由宿主绑定'}",
        f"- worktree：{dispatch.get('worktree') or '由宿主绑定'}",
        f"- build_root：{dispatch.get('build_root') or '—'}",
        *[f'- claimed_path：{path}' for path in dispatch.get('claimed_paths') or []], '',
        '## 工作规则', '',
        '- 本契约已包含本任务正文、适用全局约束、依赖和来源摘要；默认无需重复读取整个总体 plan。',
        '- 如需追溯原文，按上方来源引用读取；来源摘要不一致时停止并返回 CHANGE_REQUEST。',
        '- 不修改总体 plan、父状态或其他任务的文件。',
        '- 公共接口、范围、基线、安全或外部动作发生变化时返回 CHANGE_REQUEST。', '',
        '## 完成回传', '',
        '执行 `session-report`，状态只能使用 `DONE`、`BLOCKED` 或 `CHANGE_REQUEST`。',
        '报告至少包含 STATUS、TASK、FILES、TESTS、INTERFACES、EVIDENCE 和 NEED_FROM_MAIN。',
        f"示例：`python scripts/gsp.py session-report {state['id']} {contract['task_id']} "
        '--session <Snn> --status DONE --summary \"...\"`', '',
    ]
    return '\n'.join(lines)


def verify_task_contract(contract, state, plan, spec='', source_paths=None):
    envelope = {
        key: value for key, value in contract.items()
        if key != 'contract_uid'
    }
    if (
        contract.get('content_digest')
        != _digest(_content_payload(contract))
        or contract.get('contract_uid') != _digest(envelope)
    ):
        raise ValueError('TASK_CONTRACT_TAMPERED')
    current = compile_task_contract(
        state, contract['task_id'], plan, spec, source_paths=source_paths,
    )
    if current['content_digest'] != contract['content_digest']:
        raise ValueError('TASK_CONTRACT_STALE')
    return current


def write_conflicts(state, plan):
    tasks = {row['id']: row for row in state.get('tasks', [])}
    def ancestors(task, seen=None):
        seen = set() if seen is None else seen
        for dep in tasks[task].get('depends', []):
            if dep in tasks and dep not in seen:
                seen.add(dep)
                ancestors(dep, seen)
        return seen
    contracts = [compile_task_contract(state, task, plan) for task in tasks]
    conflicts = []
    for index, first in enumerate(contracts):
        for second in contracts[index + 1:]:
            left, right = first['task_id'], second['task_id']
            if left in ancestors(right) or right in ancestors(left):
                continue
            for a in first['writes']:
                for b in second['writes']:
                    # Unknown glob tails conservatively claim their fixed prefix.
                    a = re.split(r'[*?\[]', a.casefold())[0].rstrip('/')
                    b = re.split(r'[*?\[]', b.casefold())[0].rstrip('/')
                    if not a or not b or a == b or a.startswith(b + '/') or b.startswith(a + '/'):
                        conflicts.append({'tasks': [left, right], 'paths': [a, b]})
    return conflicts
