"""Task dispatch, progress, durable event delivery and cleanup controls."""
from pathlib import Path, PurePosixPath
import re
import uuid

from .runtime import WorkflowError, atomic, inside, now, read, redact
from .sessions import _save as save_registry
from .sessions import load_registry

PLATFORMS = {'codex', 'claude', 'manual', 'other'}
DISPATCH_MODES = {'auto', 'adapter', 'manual'}
ACTIVE_DISPATCH = {'adapter-ready', 'running', 'reported', 'manual-required'}
CLEANUP_RESULTS = {'archived', 'released', 'removed'}


def _folders(project, state):
    run, doc = project.paths(state['id'])
    machine = run / 'sessions'
    human = doc / 'sessions'
    machine.mkdir(parents=True, exist_ok=True)
    human.mkdir(parents=True, exist_ok=True)
    (human / 'history').mkdir(parents=True, exist_ok=True)
    return machine, human


def _default(state):
    return {
        'schema': 1,
        'task': state['id'],
        'next_dispatch': 1,
        'dispatches': [],
        'cleanup': [],
    }


def load_dispatch(project, state):
    machine, _ = _folders(project, state)
    file = machine / 'dispatch.json'
    data = _default(state) if not file.exists() else read(file)
    if data.get('task') != state['id']:
        raise WorkflowError('DISPATCH_REGISTRY_TASK_MISMATCH')
    for key, value in (('schema', 1), ('next_dispatch', 1), ('dispatches', []), ('cleanup', [])):
        data.setdefault(key, value)
    return data


def _save_dispatch(project, state, data):
    machine, _ = _folders(project, state)
    atomic(machine / 'dispatch.json', data)


def _assignment(values, label):
    result = {}
    common = []
    for raw in values or []:
        text = str(raw or '').strip()
        if '=' not in text:
            common.append(text)
            continue
        key, value = text.split('=', 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise WorkflowError(f'DISPATCH_{label.upper()}_INVALID use TASK=VALUE')
        if key in result:
            raise WorkflowError(f'DISPATCH_{label.upper()}_DUPLICATE {key}')
        result[key] = value
    if len(common) > 1:
        raise WorkflowError(f'DISPATCH_{label.upper()}_DUPLICATE common value')
    return result, common[0] if common else None


def _task_map(state):
    return {str(task.get('id')): task for task in state.get('tasks', [])}


def _accepted_task_ids(state, data):
    accepted = {
        str(task.get('id')) for task in state.get('tasks', [])
        if task.get('status') in {'done', 'accepted', 'integrated', 'cleaned'}
    }
    for row in data.get('dispatches', []):
        if row.get('status') in {'accepted', 'integrated', 'cleaned'}:
            accepted.add(str(row.get('task')))
    return accepted


def _normalize_path(worktree, raw):
    value = str(raw or '').strip().replace('\\', '/')
    if not value:
        raise WorkflowError('DISPATCH_PATH_REQUIRED')
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            value = candidate.resolve().relative_to(Path(worktree).resolve()).as_posix()
        except ValueError as exc:
            raise WorkflowError('DISPATCH_PATH_ESCAPE ' + value) from exc
    else:
        value = PurePosixPath(value).as_posix()
    value = value.strip('/') or '.'
    if value == '..' or value.startswith('../') or '/..' in value or '\x00' in value:
        raise WorkflowError('DISPATCH_PATH_ESCAPE ' + value)
    return value


def _overlap(left, right):
    return left == '.' or right == '.' or left == right or left.startswith(right + '/') or right.startswith(left + '/')


def _next_id(data):
    number = int(data.get('next_dispatch', 1))
    used = {row.get('id') for row in data.get('dispatches', [])}
    while f'D{number:03d}' in used:
        number += 1
    data['next_dispatch'] = number + 1
    return f'D{number:03d}'


def _next_session_hints(project, state, count, data=None):
    registry = load_registry(project, state)
    used = {row.get('id') for row in registry.get('sessions', [])}
    used.update(row.get('session_hint') for row in (data or {}).get('dispatches', []))
    number = int(registry.get('next_session', 1))
    result = []
    while len(result) < count:
        token = f'S{number:02d}'
        if token not in used:
            result.append(token)
        number += 1
    return result


def _relative(project, path):
    try:
        return path.resolve().relative_to(project.root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def render_task_prompt(state, task, dispatch):
    paths = dispatch.get('claimed_paths') or ['—']
    dependencies = task.get('depends') or ['无']
    plan_path = (state.get('documents') or {}).get('plan') or f"{state.get('doc_dir', 'docs/work')}/plan.md"
    brief_path = dispatch.get('brief') or f"{state.get('doc_dir', 'docs/work')}/sessions/{task['id']}-brief.md"
    interfaces = task.get('interfaces') or ['遵循总体 plan.md 中的公共接口约束']
    tests = task.get('tests') or [str(task.get('acceptance') or '完成任务验收并提交 report')]
    return '\n'.join([
        f"# {task['id']} 子会话启动提示词", '',
        f"- GSP：{state.get('number') or state['id']}",
        f"- 任务：{task['id']} {task.get('title') or ''}".rstrip(),
        f"- provisional session：{dispatch.get('session_hint') or '由主会话登记'}",
        '- 角色：worker',
        '- 主会话：controller，结果通过 report 回传',
        f"- 平台目标：{dispatch.get('platform')}",
        f"- 派发方式：{dispatch.get('dispatch_mode')}",
        f"- branch：{dispatch.get('branch') or '由宿主绑定'}",
        f"- worktree：{dispatch.get('worktree') or '由宿主绑定'}", '',
        '## 必读文件', '',
        f'- 总体计划：{plan_path}',
        f'- 本任务 brief：{brief_path}', '',
        '## 目标', '',
        str(task.get('acceptance') or task.get('title') or '完成计划任务并提交验证报告'), '',
        '## 允许路径', '',
        *[f'- {path}' for path in paths], '',
        '## 公共接口', '',
        *[f'- {item}' for item in interfaces], '',
        '## 测试要求', '',
        *[f'- {item}' for item in tests], '',
        '## 依赖', '',
        *[f'- {item}' for item in dependencies], '',
        '## 禁止范围', '',
        '- 不修改总体 plan、父状态或其他任务的文件。',
        '- 不扩大到未声明的路径、公共契约、凭据或外部动作。', '',
        '## 工作规则', '',
        '- 先读取总体 plan.md 和本任务 brief。',
        '- 局部实现、局部测试和普通机械检查由本子会话决定顺序。',
        '- 公共接口、范围、基线、安全和外部动作问题先返回 CHANGE_REQUEST。', '',
        '## 完成回传', '',
        '执行 `session-report`，状态只能使用 `DONE`、`BLOCKED` 或 `CHANGE_REQUEST`。',
        '报告至少包含 STATUS、TASK、FILES、TESTS、INTERFACES、EVIDENCE 和 NEED_FROM_MAIN。',
        f"示例：`python scripts/gsp.py session-report {state['id']} {task['id']} --session <Snn> --status DONE --summary \"...\"`", '',
    ])


def dispatch_tasks(project, state, args):
    registry = load_registry(project, state)
    controller = next((row for row in registry.get('sessions', []) if row.get('id') == args.session), None)
    if not controller or controller.get('role') != 'controller' or registry.get('controller_session') != args.session:
        raise WorkflowError('CONTROLLER_LEASE_REQUIRED')
    data = load_dispatch(project, state)
    tasks_by_id = _task_map(state)
    target_map, common_platform = _assignment(getattr(args, 'target', []), 'target')
    path_map, common_path = _assignment(getattr(args, 'path', []), 'path')
    worktree_map, common_worktree = _assignment(getattr(args, 'worktree', []), 'worktree')
    requested = list(getattr(args, 'tasks', []) or [])
    if not requested:
        accepted = _accepted_task_ids(state, data)
        existing = {row.get('task') for row in data.get('dispatches', []) if row.get('status') in ACTIVE_DISPATCH}
        requested = [
            task['id'] for task in state.get('tasks', [])
            if task.get('id') not in accepted and task.get('id') not in existing
            and all(dep in accepted for dep in task.get('depends') or [])
        ]
    if not requested:
        raise WorkflowError('DISPATCH_READY_EMPTY')
    max_parallel = int(getattr(args, 'max_parallel', 4) or 0)
    if max_parallel < 1 or len(requested) > max_parallel:
        raise WorkflowError('DISPATCH_BATCH_LIMIT')
    if len(set(requested)) != len(requested):
        raise WorkflowError('DISPATCH_TASK_DUPLICATE')
    accepted = _accepted_task_ids(state, data)
    existing_active = {row.get('task') for row in data.get('dispatches', []) if row.get('status') in ACTIVE_DISPATCH}
    session_hints = _next_session_hints(project, state, len(requested), data)
    prepared = []
    supersede = []
    for index, task_id in enumerate(requested):
        task = tasks_by_id.get(task_id)
        if not task:
            raise WorkflowError('DISPATCH_TASK_NOT_FOUND ' + str(task_id))
        if task_id in existing_active and not getattr(args, 'retry', False):
            raise WorkflowError('DISPATCH_TASK_EXISTS ' + task_id)
        dependencies = set(task.get('depends') or [])
        if dependencies - accepted:
            raise WorkflowError('DISPATCH_DEPENDENCY_BLOCKED ' + task_id)
        if dependencies.intersection(requested):
            raise WorkflowError('DISPATCH_DEPENDENCY_BATCH ' + task_id)
        platform = (target_map.get(task_id) or common_platform or getattr(args, 'platform', None) or 'manual').lower()
        if platform not in PLATFORMS:
            raise WorkflowError('DISPATCH_PLATFORM_INVALID ' + platform)
        mode = (getattr(args, 'dispatch_mode', None) or ('auto' if platform == 'codex' else 'manual')).lower()
        if mode not in DISPATCH_MODES:
            raise WorkflowError('DISPATCH_MODE_INVALID ' + mode)
        worktree = worktree_map.get(task_id) or common_worktree
        if not worktree:
            worktree = (state.get('git_topology') or {}).get('workspace_root') or str(project.root)
        worktree_path = Path(worktree).resolve()
        if not inside(project.root, worktree_path):
            raise WorkflowError('DISPATCH_WORKTREE_ESCAPE ' + str(worktree_path))
        raw_path = path_map.get(task_id) or common_path
        if not raw_path:
            raise WorkflowError('DISPATCH_PATH_REQUIRED ' + task_id)
        claimed = [_normalize_path(worktree_path, raw_path)]
        for previous in prepared:
            if previous['worktree'] != str(worktree_path):
                continue
            if any(_overlap(left, right) for left in claimed for right in previous['claimed_paths']):
                raise WorkflowError('DISPATCH_PATH_CONFLICT ' + task_id)
            raise WorkflowError('DISPATCH_WORKTREE_CONFLICT ' + task_id)
        for previous in data.get('dispatches', []):
            if previous.get('status') not in ACTIVE_DISPATCH or previous.get('worktree') != str(worktree_path):
                continue
            if previous.get('task') == task_id and getattr(args, 'retry', False):
                supersede.append(previous)
                continue
            if any(_overlap(left, right) for left in claimed for right in previous.get('claimed_paths') or []):
                raise WorkflowError('DISPATCH_PATH_CONFLICT ' + task_id)
            raise WorkflowError('DISPATCH_WORKTREE_CONFLICT ' + task_id)
        status = 'adapter-ready' if mode in {'auto', 'adapter'} and platform != 'manual' else 'manual-required'
        dispatch_id = _next_id(data)
        prepared.append({
            'id': dispatch_id,
            'task': task_id,
            'session_hint': session_hints[index],
            'platform': platform,
            'dispatch_mode': mode,
            'status': status,
            'worktree': str(worktree_path),
            'branch': (state.get('git_topology') or {}).get('branch'),
            'claimed_paths': claimed,
            'dependencies': sorted(dependencies),
            'created_at': now(),
            'updated_at': now(),
            'thread_id': None,
            'prompt': None,
        })
    _, human = _folders(project, state)
    for row in prepared:
        task = tasks_by_id[row['task']]
        prompt_target = human / f"{row['task']}-prompt.md"
        brief_target = human / f"{row['task']}-brief.md"
        if prompt_target.exists() or brief_target.exists():
            if not getattr(args, 'retry', False):
                raise WorkflowError('DISPATCH_PROMPT_EXISTS ' + row['task'])
            for target in (prompt_target, brief_target):
                if target.exists():
                    archive = human / 'history' / f"{target.stem}-{row['id']}{target.suffix}"
                    target.replace(archive)
        row['brief'] = _relative(project, brief_target)
        atomic(brief_target, render_task_prompt(state, task, row).replace('子会话启动提示词', '子会话 Brief', 1))
        atomic(prompt_target, render_task_prompt(state, task, row))
        row['prompt'] = _relative(project, prompt_target)
        data['dispatches'].append(row)
    for previous in supersede:
        previous['status'] = 'superseded'
        previous['updated_at'] = now()
    _save_dispatch(project, state, data)
    for row in prepared:
        registry.setdefault('tasks', {}).setdefault(row['task'], {}).update({
            'dispatch_id': row['id'],
            'dispatch_status': row['status'],
            'brief': row['brief'],
            'platform': row['platform'],
            'dispatch_mode': row['dispatch_mode'],
            'prompt': row['prompt'],
        })
    save_registry(project, state, registry)
    return {
        'task': state['id'],
        'batch_size': len(prepared),
        'dispatches': prepared,
        'next_action': 'Codex host adapter creates threads for adapter-ready requests; user forwards manual-required prompts.',
    }


def session_progress(project, state):
    data = load_dispatch(project, state)
    registry = load_registry(project, state)
    tasks = list(state.get('tasks') or [])
    accepted_ids = _accepted_task_ids(state, data)
    reports_done = len({task for task, row in (registry.get('tasks') or {}).items() if row.get('report_status') == 'DONE'})
    blocked_ids = {task.get('id') for task in tasks if task.get('status') == 'blocked'}
    change_requests = 0
    for task, row in (registry.get('tasks') or {}).items():
        if row.get('report_status') == 'BLOCKED':
            blocked_ids.add(task)
        if row.get('report_status') == 'CHANGE_REQUEST':
            change_requests += 1
    cleaned = len([row for row in data.get('cleanup', []) if row.get('result') in CLEANUP_RESULTS])
    manual_required = len([row for row in data.get('dispatches', []) if row.get('status') == 'manual-required'])
    running = len([row for row in data.get('dispatches', []) if row.get('status') == 'running'])
    active_tasks = {row.get('task') for row in data.get('dispatches', []) if row.get('status') in ACTIVE_DISPATCH}
    next_tasks = []
    for task in tasks:
        task_id = task.get('id')
        if task_id in accepted_ids or task_id in active_tasks or task_id in blocked_ids:
            continue
        if all(dep in accepted_ids for dep in task.get('depends') or []):
            next_tasks.append(task_id)
    total = len(tasks)
    percent = round((len(accepted_ids) / total * 100) if total else 0.0, 1)
    phases = []
    for task in tasks:
        task_id = task.get('id')
        row = next((item for item in reversed(data.get('dispatches', [])) if item.get('task') == task_id), None)
        status = 'accepted' if task_id in accepted_ids else (row.get('status') if row else ('blocked' if task_id in blocked_ids else 'planned'))
        phases.append({'task': task_id, 'title': task.get('title'), 'status': status, 'depends': task.get('depends') or [], 'next': 'ready' if task_id in next_tasks else ''})
    return {
        'task': state['id'],
        'overall_progress': percent,
        'planned_tasks': total,
        'dispatches_total': len(data.get('dispatches', [])),
        'distinct_tasks': len({row.get('task') for row in data.get('dispatches', [])}),
        'reports_done': reports_done,
        'accepted_tasks': len(accepted_ids),
        'cleaned_worktrees': cleaned,
        'running_sessions': running,
        'blocked_tasks': len(blocked_ids),
        'change_requests': change_requests,
        'manual_required': manual_required,
        'next_tasks': next_tasks,
        'phases': phases,
        'controller_session': registry.get('controller_session'),
    }


def watch_session_events(project, state, args):
    registry = load_registry(project, state)
    adapter = getattr(args, 'adapter', None) or 'manual'
    limit = int(getattr(args, 'limit', 20) or 20)
    if limit < 1:
        raise WorkflowError('WATCH_LIMIT_INVALID')
    event_id = getattr(args, 'event_id', None)
    pending = [event for event in registry.get('outbox', []) if not event.get('delivered') and (not event_id or event.get('id') == event_id)]
    selected = pending[:limit]
    delivered = 0
    notifications = []
    for event in selected:
        notification = {key: event.get(key) for key in ('id', 'task', 'session', 'status', 'report')}
        if adapter == 'manual':
            event['delivered'] = True
            event['delivered_at'] = now()
            event['delivery'] = 'manual'
            delivered += 1
            notification['delivery'] = 'manual'
        else:
            notification['delivery'] = 'adapter-required'
        notifications.append(notification)
    if delivered:
        save_registry(project, state, registry)
    return {
        'task': state['id'],
        'adapter': adapter,
        'delivered': delivered,
        'pending': len(pending) - delivered,
        'notifications': notifications,
    }


def clean_session_worktree(project, state, args):
    data = load_dispatch(project, state)
    task_id = args.task
    rows = [row for row in data.get('dispatches', []) if row.get('task') == task_id]
    if not rows:
        raise WorkflowError('CLEANUP_DISPATCH_NOT_FOUND ' + task_id)
    row = rows[-1]
    task = next((item for item in state.get('tasks', []) if item.get('id') == task_id), None)
    if not task or task.get('status') not in {'done', 'accepted', 'integrated', 'cleaned'}:
        if row.get('status') not in {'accepted', 'integrated'}:
            raise WorkflowError('CLEANUP_ACCEPTANCE_REQUIRED ' + task_id)
    result = args.result
    if result not in CLEANUP_RESULTS:
        raise WorkflowError('CLEANUP_RESULT_INVALID')
    existing = next((item for item in data.get('cleanup', []) if item.get('dispatch_id') == row.get('id') and item.get('result') in CLEANUP_RESULTS), None)
    if existing:
        return {'task': state['id'], 'cleanup': existing, 'idempotent': True}
    cleanup = {
        'id': uuid.uuid4().hex,
        'dispatch_id': row['id'],
        'task': task_id,
        'result': result,
        'worktree': row.get('worktree'),
        'reason': redact(args.reason),
        'at': now(),
    }
    data.setdefault('cleanup', []).append(cleanup)
    row['status'] = 'cleaned'
    row['updated_at'] = now()
    _save_dispatch(project, state, data)
    registry = load_registry(project, state)
    registry.setdefault('tasks', {}).setdefault(task_id, {})['cleanup_status'] = result
    save_registry(project, state, registry)
    return {'task': state['id'], 'cleanup': cleanup}
