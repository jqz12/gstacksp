"""Task-scoped cross-session registry, leases and document handoffs."""
from pathlib import Path, PurePosixPath
import os
import re
import shutil
import uuid

from .runtime import WorkflowError, atomic, inside, now, read, redact

ROLES = {"controller", "worker", "integration", "observer"}
WRITE_MODES = {"write", "read-only"}
REPORT_STATUSES = {"DONE", "BLOCKED", "CHANGE_REQUEST"}
ACTIVE = {"active", "waiting", "reported"}
SESSION_RE = re.compile(r"^S[0-9]{2,}$")


def _paths(project, state):
    run, doc = project.paths(state["id"])
    machine = run / "sessions"
    human = doc / "sessions"
    machine.mkdir(parents=True, exist_ok=True)
    human.mkdir(parents=True, exist_ok=True)
    (human / "history").mkdir(parents=True, exist_ok=True)
    return machine, human


def _default(state):
    return {"schema": 1, "task": state["id"], "number": state.get("number"),
            "controller_session": None, "next_session": 1,
            "workspace_leases": {}, "sessions": [], "tasks": {}, "events": [], "outbox": []}


def load_registry(project, state):
    machine, _ = _paths(project, state)
    file = machine / "index.json"
    registry = _default(state) if not file.exists() else read(file)
    if registry.get("task") != state["id"]:
        raise WorkflowError("SESSION_REGISTRY_TASK_MISMATCH")
    for key, default in (("schema", 1), ("controller_session", None),
                         ("next_session", 1), ("workspace_leases", {}),
                         ("sessions", []), ("tasks", {}), ("events", []), ("outbox", [])):
        registry.setdefault(key, default)
    return registry


def _save(project, state, registry):
    machine, human = _paths(project, state)
    atomic(machine / "index.json", registry)
    lines = [
        f"# {state.get('number') or state['id']} 会话登记", "",
        f"- 任务：{state['id']}",
        f"- 当前 controller：{registry.get('controller_session') or 'NONE'}",
        f"- 下一个会话号：S{int(registry.get('next_session', 1)):02d}", "",
        "| 会话 | 角色 | 宿主 | T 任务 | 写入 | worktree | 状态 | 声明路径 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in registry.get("sessions", []):
        paths = ", ".join(row.get("claimed_paths") or []) or "—"
        lines.append("| {id} | {role} | {host} | {task} | {mode} | {worktree} | {status} | {paths} |".format(
            id=row["id"], role=row["role"], host=row.get("host") or "unknown",
            task=row.get("task") or "—", mode=row.get("write_mode") or "read-only",
            worktree=row.get("worktree") or "—", status=row.get("status") or "unknown",
            paths=paths.replace("|", "\\|")))
    if registry.get("events"):
        lines.extend(["", "## 最近事件", ""])
        lines.extend(f"- {event.get('at')} {event.get('event')} {event.get('session') or '—'} {event.get('detail') or ''}"
                     for event in registry["events"][-20:])
    atomic(human / "index.md", "\n".join(lines) + "\n")


def _event(registry, name, session=None, detail=""):
    registry.setdefault("events", []).append({
        "id": uuid.uuid4().hex, "at": now(), "event": name,
        "session": session, "detail": redact(detail),
    })
    registry["events"] = registry["events"][-200:]


def _find(registry, token):
    token = str(token or "").strip()
    if not token:
        raise WorkflowError("SESSION_ID_REQUIRED")
    rows = [row for row in registry.get("sessions", [])
            if row.get("id") == token or row.get("thread_id") == token
            or row.get("host_session_id") == token]
    if len(rows) != 1:
        raise WorkflowError("SESSION_NOT_FOUND " + token)
    return rows[0]


def _allocate(registry):
    used = {row.get("id") for row in registry.get("sessions", [])}
    number = int(registry.get("next_session", 1))
    while f"S{number:02d}" in used:
        number += 1
    registry["next_session"] = number + 1
    return f"S{number:02d}"


def _workspace(project, state, args, role):
    raw = getattr(args, "worktree", None)
    if not raw:
        topology = state.get("git_topology") or {}
        raw = (topology.get("control_root") if role == "controller" else
               topology.get("workspace_root") or topology.get("control_root")) or str(project.root)
    path = Path(raw).resolve()
    if not inside(project.root, path):
        raise WorkflowError("SESSION_WORKSPACE_ESCAPE " + str(path))
    return path


def _normalize_paths(workspace, values):
    result = []
    for raw in values or []:
        value = str(raw or "").strip().replace("\\", "/")
        if not value:
            continue
        candidate = Path(value)
        if candidate.is_absolute():
            try:
                value = candidate.resolve().relative_to(Path(workspace).resolve()).as_posix()
            except ValueError as exc:
                raise WorkflowError("SESSION_PATH_ESCAPE " + value) from exc
        else:
            value = PurePosixPath(value).as_posix()
        value = value.strip("/") or "."
        if (value == ".." or value.startswith("../") or "/../" in value or value.endswith("/..")
                or "\x00" in value):
            raise WorkflowError("SESSION_PATH_ESCAPE " + value)
        if value not in result:
            result.append(value)
    return result


def _overlap(left, right):
    return left == "." or right == "." or left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _key(path):
    return os.path.normcase(str(Path(path).resolve()))


def _lease_owner(registry, workspace):
    owner_id = registry.get("workspace_leases", {}).get(_key(workspace))
    return next((row for row in registry.get("sessions", [])
                 if row.get("id") == owner_id and row.get("status") in ACTIVE
                 and row.get("write_mode") == "write"), None)


def _conflicts(registry, workspace, paths, exclude=None):
    key = _key(workspace)
    conflicts = []
    for row in registry.get("sessions", []):
        if row.get("id") == exclude or row.get("status") not in ACTIVE or row.get("write_mode") != "write":
            continue
        if _key(row.get("worktree")) != key:
            continue
        for left in paths:
            for right in row.get("claimed_paths") or []:
                if _overlap(left, right):
                    conflicts.append((row["id"], left, right))
    return conflicts


def _task(project, state, value):
    return project.item(state, value) if value else None


def register_session(project, state, args):
    registry = load_registry(project, state)
    role = args.role
    mode = args.write_mode
    if role not in ROLES:
        raise WorkflowError("SESSION_ROLE_INVALID")
    if mode not in WRITE_MODES:
        raise WorkflowError("SESSION_WRITE_MODE_INVALID")
    if role == "observer" and mode == "write":
        raise WorkflowError("OBSERVER_CANNOT_WRITE")
    task = _task(project, state, getattr(args, "task", None))
    if role in {"worker", "integration"} and task is None:
        raise WorkflowError("SESSION_TASK_REQUIRED")
    session_id = str(getattr(args, "session", None) or "").strip() or _allocate(registry)
    if not SESSION_RE.fullmatch(session_id):
        raise WorkflowError("SESSION_ID_INVALID use S01, S02 ...")
    if any(row.get("id") == session_id for row in registry["sessions"]):
        raise WorkflowError("SESSION_ID_EXISTS " + session_id)
    if args.thread and any(row.get("thread_id") == args.thread for row in registry["sessions"]):
        raise WorkflowError("SESSION_THREAD_EXISTS " + args.thread)
    active_controllers = [row for row in registry["sessions"]
                          if row.get("role") == "controller" and row.get("status") in ACTIVE]
    old_controller = next((row for row in active_controllers
                           if row.get("id") == registry.get("controller_session")),
                          active_controllers[0] if active_controllers else None)
    if role == "controller" and old_controller:
        if not args.takeover:
            raise WorkflowError("CONTROLLER_LEASE_HELD " + old_controller["id"])
        if not str(args.reason or "").strip():
            raise WorkflowError("TAKEOVER_REASON_REQUIRED")
        old_controller["status"] = "handed-off"
        _event(registry, "controller-takeover", old_controller["id"], args.reason)
    workspace = _workspace(project, state, args, role)
    paths = _normalize_paths(workspace, getattr(args, "path", None))
    if mode == "write" and not paths:
        raise WorkflowError("SESSION_PATHS_REQUIRED declare --path before writing")
    lease = _lease_owner(registry, workspace)
    if mode == "write" and lease:
        raise WorkflowError("WORKSPACE_WRITE_LEASE_HELD " + lease["id"])
    conflict = _conflicts(registry, workspace, paths)
    if conflict:
        raise WorkflowError("SESSION_PATH_CONFLICT " + conflict[0][1])
    record = {
        "id": session_id, "role": role, "host": args.host or "unknown",
        "thread_id": args.thread, "task": task.get("id") if task else None,
        "branch": args.branch, "worktree": str(workspace), "write_mode": mode,
        "claimed_paths": paths, "status": "active", "started_at": now(), "updated_at": now(),
    }
    registry["sessions"].append(record)
    if role == "controller":
        registry["controller_session"] = session_id
    if mode == "write":
        registry.setdefault("workspace_leases", {})[_key(workspace)] = session_id
    _event(registry, "session-register", session_id, f"{role} {mode}")
    _, human = _paths(project, state)
    label = re.sub(r"[^a-z0-9]+", "-", f"{role}-{args.host or 'unknown'}".lower()).strip("-") or "session"
    atomic(human / f"{session_id}-{label}.md",
           f"# {session_id} — {role}\n\n- 任务：{state['id']}\n- 宿主：{args.host or 'unknown'}\n- thread：{args.thread or 'unknown'}\n- worktree：{workspace}\n- 写入模式：{mode}\n- 声明路径：{', '.join(paths) or '—'}\n")
    _save(project, state, registry)
    return {"task": state["id"], "session": record, "controller_session": registry.get("controller_session")}


def _write_session(project, state, token):
    registry = load_registry(project, state)
    row = _find(registry, token)
    if row.get("status") not in ACTIVE:
        raise WorkflowError("SESSION_WRITE_LEASE_REQUIRED " + row["id"])
    owner = _lease_owner(registry, row.get("worktree"))
    if row.get("write_mode") != "write" or not owner or owner["id"] != row["id"]:
        raise WorkflowError("SESSION_WRITE_LEASE_REQUIRED " + row["id"])
    return registry, row



def release_session_lease(project, state, token, reason=''):
    """Release a worker write lease after controller acceptance."""
    registry = load_registry(project, state)
    row = _find(registry, token)
    if row.get('status') == 'released':
        return {'session': token, 'released': True, 'idempotent': True}
    key = _key(row.get('worktree')) if row.get('worktree') else None
    if key and registry.get('workspace_leases', {}).get(key) == row.get('id'):
        registry['workspace_leases'].pop(key, None)
    row['status'] = 'released'
    row['released_at'] = now()
    row['release_reason'] = redact(reason)
    row['updated_at'] = now()
    _event(registry, 'session-lease-released', row['id'], reason)
    _save(project, state, registry)
    return {'session': row['id'], 'released': True, 'idempotent': False}

def claim_session(project, state, token, paths, replace=False):
    registry, row = _write_session(project, state, token)
    normalized = _normalize_paths(Path(row["worktree"]), paths)
    if not normalized:
        raise WorkflowError("SESSION_PATHS_REQUIRED declare --path")
    if _conflicts(registry, row["worktree"], normalized, row["id"]):
        raise WorkflowError("SESSION_PATH_CONFLICT " + normalized[0])
    merged = normalized if replace else list(dict.fromkeys((row.get("claimed_paths") or []) + normalized))
    if _conflicts(registry, row["worktree"], merged, row["id"]):
        raise WorkflowError("SESSION_PATH_CONFLICT " + merged[0])
    row["claimed_paths"] = merged
    row["updated_at"] = now()
    _event(registry, "session-claim", row["id"], ", ".join(normalized))
    _save(project, state, registry)
    return {"task": state["id"], "session": row, "claimed_paths": merged}


def _document(human, name, text, replace=False):
    target = human / name
    if target.exists():
        if not replace:
            raise WorkflowError("SESSION_DOCUMENT_EXISTS " + name)
        stamp = now().replace(":", "").replace("+00:00", "Z").replace("-", "")
        shutil.move(str(target), str(human / "history" / f"{target.stem}-{stamp}.md"))
    atomic(target, text)
    return target


def write_brief(project, state, args):
    registry = load_registry(project, state)
    author = _find(registry, args.session)
    owner = _find(registry, args.owner or args.session)
    if author.get("role") not in {"controller", "integration"}:
        raise WorkflowError("SESSION_BRIEF_AUTHOR_REQUIRED")
    if author.get("role") == "controller" and registry.get("controller_session") != author["id"]:
        raise WorkflowError("CONTROLLER_LEASE_REQUIRED")
    task = project.item(state, args.task)
    if owner.get("task") not in (None, task["id"]):
        raise WorkflowError("SESSION_TASK_MISMATCH")
    if owner.get("write_mode") == "write":
        if args.path:
            claim_session(project, state, owner["id"], args.path)
            registry = load_registry(project, state)
            owner = _find(registry, args.owner or args.session)
        if not owner.get("claimed_paths"):
            raise WorkflowError("SESSION_PATHS_REQUIRED brief needs --path")
    if not args.title.strip() or not args.objective.strip():
        raise WorkflowError("SESSION_BRIEF_REQUIRED")
    _, human = _paths(project, state)
    build = {"build_root": None, "build_env": {}}
    if owner.get("worktree") and inside(Path(project.root) / ".gsp" / "worktrees",
                                       Path(owner["worktree"])):
        from .worktree_lifecycle import build_environment
        build = build_environment(project, state, task)
    lines = [
        f"# {task['id']} Brief — {args.title.strip()}", "",
        f"- GSP：{state.get('number') or state['id']}", f"- 任务：{task['id']}",
        f"- 发出会话：{author['id']}", f"- 执行会话：{owner['id']}",
        f"- worktree：{owner.get('worktree') or '—'}",
        f"- 构建根：{build.get('build_root') or '—'}",
        *[f"- {key}={value}" for key, value in (build.get("build_env") or {}).items()],
        "",
        "## 目标", "", args.objective.strip(), "",
        "## 允许路径", "",
    ]
    lines.extend(f"- {path}" for path in (owner.get("claimed_paths") or args.path or []))
    lines.extend(["", "## 接口", ""])
    lines.extend(f"- {item}" for item in (args.interface or [])) or lines.append("- 无新增公共接口")
    lines.extend(["", "## 测试", ""])
    lines.extend(f"- {item}" for item in (args.test or [])) or lines.append("- 由子会话补充局部测试")
    lines.extend(["", "## 依赖", "", f"- {args.depends or '无'}",
                  "", "## 禁止范围", "", f"- {args.forbid or '不得扩大到其他 T 任务或公共契约'}",
                  "", "## 交付条件", "", "- 完成局部实现并提交结构化 report；公共冲突先返回 CHANGE_REQUEST。", ""])
    path = _document(human, f"{task['id']}-brief.md", "\n".join(lines), args.replace)
    registry = load_registry(project, state)
    registry.setdefault("tasks", {})[task["id"]] = {
        **registry.get("tasks", {}).get(task["id"], {}),
        "brief_status": "briefed", "brief": str(path.relative_to(project.root)).replace("\\", "/"),
        "owner_session": owner["id"], "author_session": author["id"],
        "claimed_paths": owner.get("claimed_paths") or args.path or [],
    }
    _event(registry, "brief-written", owner["id"], task["id"])
    _save(project, state, registry)
    return {"task": state["id"], "plan_task": task["id"], "brief": registry["tasks"][task["id"]]["brief"], "owner_session": owner["id"]}


def write_report(project, state, args):
    registry, row = _write_session(project, state, args.session)
    task = project.item(state, args.task)
    mapping = registry.get("tasks", {}).get(task["id"]) or {}
    if mapping.get("owner_session") and mapping["owner_session"] != row["id"]:
        raise WorkflowError("SESSION_REPORT_OWNER_MISMATCH")
    status = args.status.upper()
    if status not in REPORT_STATUSES or not args.summary.strip():
        raise WorkflowError("SESSION_REPORT_REQUIRED")
    _, human = _paths(project, state)
    lines = [
        f"# {task['id']} Report — {status}", "",
        f"- GSP：{state.get('number') or state['id']}", f"- 会话：{row['id']}",
        f"- 宿主：{row.get('host') or 'unknown'}", f"- 状态：{status}", "",
        "## Summary", "", args.summary.strip(), "",
    ]
    for label, values, empty in (("FILES", args.files, "无文件"), ("TESTS", args.tests, "未提供"),
                                 ("INTERFACES", args.interfaces, "无变化"), ("EVIDENCE", args.evidence, "未提供")):
        lines.extend([f"## {label}", ""])
        lines.extend(f"- {item}" for item in (values or [])) or lines.append(f"- {empty}")
        lines.append("")
    lines.extend(["## NEED_FROM_MAIN", "", f"- {args.need_from_main or '无'}",
                  "", "## Ruling", "", f"- {args.ruling or '无'}", ""])
    path = _document(human, f"{task['id']}-report.md", "\n".join(lines), True)
    mapping.update({"report_status": status, "report": str(path.relative_to(project.root)).replace("\\", "/"),
                    "report_session": row["id"], "reported_at": now()})
    registry.setdefault("tasks", {})[task["id"]] = mapping
    # Keep a DONE reporter active until the controller explicitly reuses or
    # hands off the workspace; this preserves repeated report history without
    # silently reopening the write lease to another session.
    row["status"] = "blocked" if status != "DONE" else "reported"
    row["updated_at"] = now()
    _event(registry, "report-written", row["id"], f"{task['id']} {status}")
    # Reports are durable handoff events.  Keep them in an append-only outbox
    # so an inactive controller can consume the completion later without
    # polling the child session or relying on chat history.
    registry.setdefault("outbox", []).append({
        "id": uuid.uuid4().hex,
        "task": task["id"],
        "session": row["id"],
        "status": status,
        "report": mapping["report"],
        "worktree": row.get("worktree"),
        "reported_at": mapping["reported_at"],
        "delivered": False,
        "created_at": now(),
    })
    registry["outbox"] = registry["outbox"][-500:]
    _save(project, state, registry)
    return {"task": state["id"], "plan_task": task["id"], "report": mapping["report"], "status": status}


def handoff_controller(project, state, args):
    registry = load_registry(project, state)
    old = _find(registry, args.from_session)
    new = _find(registry, args.to_session)
    if registry.get("controller_session") != old["id"]:
        raise WorkflowError("CONTROLLER_SOURCE_MISMATCH")
    if old["status"] not in ACTIVE or new["status"] not in ACTIVE:
        raise WorkflowError("SESSION_NOT_ACTIVE")
    if old["id"] == new["id"]:
        raise WorkflowError("CONTROLLER_TARGET_SAME")
    if new.get("write_mode") != "write":
        new["write_mode"] = "write"
        new["worktree"] = old["worktree"]
        new["claimed_paths"] = list(old.get("claimed_paths") or ["."])
    new["role"] = "controller"
    old["status"] = "handed-off"
    new["status"] = "active"
    old["updated_at"] = new["updated_at"] = now()
    registry["controller_session"] = new["id"]
    registry.setdefault("workspace_leases", {})[_key(new["worktree"])] = new["id"]
    _event(registry, "controller-handoff", new["id"], args.reason)
    _, human = _paths(project, state)
    atomic(human / f"handoff-{old['id']}-to-{new['id']}-{uuid.uuid4().hex[:8]}.md",
           f"# Controller handoff {old['id']} to {new['id']}\n\n- 时间：{now()}\n- 原因：{redact(args.reason)}\n")
    _save(project, state, registry)
    return {"task": state["id"], "controller_session": new["id"], "from": old["id"], "to": new["id"], "status": "handed-off"}


def session_status(project, state):
    registry = load_registry(project, state)
    blockers = []
    active = [row for row in registry.get("sessions", []) if row.get("status") in ACTIVE]
    if active and not registry.get("controller_session"):
        blockers.append({"code": "CONTROLLER_MISSING"})
    if len([row for row in active if row.get("role") == "controller"]) > 1:
        blockers.append({"code": "CONTROLLER_LEASE_CONFLICT"})
    for row in active:
        if row.get("write_mode") != "write":
            continue
        owner = _lease_owner(registry, row.get("worktree"))
        if not owner or owner["id"] != row["id"]:
            blockers.append({"code": "WORKSPACE_WRITE_LEASE_INVALID", "session": row["id"]})
        if not row.get("claimed_paths"):
            blockers.append({"code": "SESSION_PATHS_MISSING", "session": row["id"]})
    for task_id, mapping in registry.get("tasks", {}).items():
        if mapping.get("report_status") == "BLOCKED":
            blockers.append({"code": "SESSION_TASK_BLOCKED", "task": task_id})
        if mapping.get("report_status") == "CHANGE_REQUEST":
            blockers.append({"code": "SESSION_CHANGE_REQUEST", "task": task_id})
    return {"schema": registry.get("schema", 1), "task": state["id"], "number": state.get("number"),
            "status": "blocked" if blockers else "ready",
            "controller_session": registry.get("controller_session"),
            "workspace_leases": registry.get("workspace_leases", {}),
            "sessions": registry.get("sessions", []), "tasks": registry.get("tasks", {}),
            "blockers": blockers}


def authorize_edit(project, state, token, paths):
    registry = load_registry(project, state)
    registry, row = _write_session(project, state, token)
    absolute = [Path(path).resolve() for path in paths]
    normalized = _normalize_paths(Path(row["worktree"]), absolute)
    for path in normalized:
        if not any(claimed == "." or path == claimed or path.startswith(claimed + "/")
                   for claimed in row.get("claimed_paths") or []):
            raise WorkflowError("SESSION_PATH_NOT_CLAIMED " + path)
    return {"session": row["id"], "paths": normalized, "allowed": True}
