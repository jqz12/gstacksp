# 旧任务、编号与 worktree 迁移预览

T03 提供只读迁移预览。它盘点旧 work、run、任务目录、TODO identity、显式 workspace 绑定和已注册 Git worktree，生成可重新核验的 `MigrationManifest`，但不执行迁移、不重建索引、不初始化权威身份库，也不删除或合并任何资源。

```python
preview_migration(root: Path) -> MigrationManifest
verify_source(root: Path, manifest: MigrationManifest) -> None
```

## 只读边界

预览会先对下列相关来源做一次内容捕获，再只使用捕获到的字节和观察结果：

- `docs/work/**/record.json`
- `.gsp/runs/*/state.json`
- `.gsp/work-counter.json`
- `TODOS.md` 或 `TODO.md` 及 `.gsp/backlog` 读取状态
- `.gsp/documents.sqlite`
- 已有的 `.gsp/orchestration/registry.sqlite`
- `.gsp/writer.lock`、`.gsp/driver.lock`
- `.gsp/runs/*/driver/state.json`
- `.gsp/runs/*/evidence/*/command-progress.json`
- `.gsp/runs/*/sessions/index.json`
- `.gsp/runs/*/sessions/dispatch.json`
- Git worktree、branch、HEAD、status 和 ancestry

普通 JSON、Markdown 和 registry 只读取一次字节。文件缺失会进入明确的
`source_capture.membership=false`；读取失败会进入结构化
`source_capture.unavailable`。两者都不会在后续解析阶段触发第二次读取。预览结束时重新
执行同一捕获；成员、字节和摘要发生变化会使 manifest 以
`MIGRATION_SOURCE_CHANGED` 失效。

两个 SQLite 来源都不会连接原文件，也不会在原目录创建 journal、WAL、SHM 或查询锁。
预览先捕获主库及已有 WAL/SHM 的原始字节，再在私有临时目录组成稳定快照，并以
`PRAGMA query_only=ON` 查询快照。Windows 上使用共享模式为 0 的 `CreateFileW` 和
`OPEN_EXISTING` 获取一致字节；任何保留句柄、活跃 writer、权限不足或平台无法提供同等
保护都会 fail closed，分别表现为 writer-live、unknown 或不可用来源。

身份权威库不只检查 metadata；它必须通过 `IdentityStore._schema_problem` 的完整 schema
验证，并核对 project UID、root 和 incarnation。`documents.sqlite` 使用同一字节捕获策略，
索引查询只发生在私有副本上。

预览不能调用 `Project.initialize/write`、`IdentityStore.initialize/ensure_authority/allocate`、
`Governance._db/_ensure/rebuild`、`runtime.atomic` 或 worktree prepare/cleanup。

权威库不存在时结果是未初始化状态，`allocator_incarnation` 为 `null`。预览不会为了得到
“完整”清单而创建它。Git 探测设置 `GIT_OPTIONAL_LOCKS=0`，避免 `status` 顺手更新索引。

## 恰好一次分类

每个来源对象在 `classification_manifest` 中恰好出现一次：

```text
(domain, source_path, identity)
```

支持六个来源域：

| domain | identity | 含义 |
|---|---|---|
| `work_record` | 规范 `GSP-########` | 一个 work 正本 |
| `run_state` | run 或 plan identity | 一份运行状态 |
| `task_directory` | 相对目录路径 | 一个候选任务目录 |
| `todo_marker` | 原始 marker identity | 一个由 `Backlog.load` 证明来源的 TODO 条目 |
| `explicit_workspace_binding` | run 或 run:task identity | 一条 run/task topology 声明 |
| `registered_git_worktree` | 规范化路径 | 一个 Git 注册 worktree |

匹配关系写入独立的 `link` 对象，例如 `work_run` 和 `work_directory`。link 不会替代或合并两个来源对象。UUID、编号、run identity、目录和文档路径同时保留，不能只看目录名推断归属。

旧 TODO marker 不能再只凭 `GSP-xxxx` 外形推定为 work。迁移预览通过
`Backlog.load` 的私有精确快照证明它来自 TODO 索引，统一标记为 `object_kind=todo`，
同时保留 `raw_alias`。TODO marker 与 backlog records 必须双向覆盖；缺 record、孤立
record、原文路径或原始 Markdown digest 不匹配都会形成 blocker。

run 或 task 显式 topology 同时接受兼容字段 `workspace_root` 与 `worktree_path`。两个字段
都参与资源发现；二者同时存在但规范化后指向不同路径时形成 `WORKTREE_PATH_CONFLICT`，
资源保持 `retain_unknown`。任务级 topology 从已捕获的 run state 提取一次，并保留
`run:task` 归属；不能因为它不属于 `git_topology` 就直接丢弃。

work 行恰好落入以下一种状态：

```text
matched_run
work_without_run
blocked
```

run 行恰好落入：

```text
matched_work
legacy_only_run
blocked
```

计数必须满足：

```text
work_record = work_matched_run + work_without_run + work_blocked
run_state = run_matched_work + run_legacy_only + run_blocked
```

数字来自本次实际扫描，不能硬编码历史快照。

## 执行现场与身份

迁移预览不调用会 `mkdir` 或创建锁的 `runtime.inspect_lock`。它只读取已有锁文件；`writer.lock` 的 advisory probe 使用只读文件描述符，非阻塞地探测一次，不创建成员、不写元数据、不抢锁、不杀进程。探测确认 busy 时直接视为 live。

执行身份必须同时具备有效 PID 和可核验的进程出生 token，并使用 `gsp.hosts.process_token/is_alive` 语义：

- 当前进程存在且出生 token 完全匹配：`live`。
- PID 不存在且历史 token 完整：可证明 `dead`，不得仅因 PID 或时间戳判死。
- token 缺失、为 `unknown`、权限不足以核验或 PID 复用：`unknown`。
- 文件损坏、JSON 非对象、PID 类型非法：`malformed`。

稳定 blocker codes：

| code | 含义 |
|---|---|
| `WRITER_LOCK_LIVE` | writer advisory lock 正被持有，或元数据指向可核验的活进程 |
| `WRITER_LOCK_UNKNOWN` | writer 身份或 advisory 状态不可信 |
| `WRITER_LOCK_MALFORMED` | writer 元数据损坏或字段非法 |
| `DRIVER_LOCK_LIVE` | driver 租约 owner 可核验为活进程 |
| `DRIVER_LOCK_UNKNOWN` | driver owner 身份不可信 |
| `DRIVER_LOCK_MALFORMED` | driver 租约损坏 |
| `DRIVER_LOCK_OWNER_UNKNOWN` | live/unknown driver owner 的 task 不对应任何 run |
| `DRIVER_STATE_LIVE` | `driver/state.json` 中 worker/check 身份可核验为活进程 |
| `DRIVER_STATE_UNKNOWN` | worker/check 身份缺失或不可验证 |
| `DRIVER_STATE_STALE` | worker/check 可证明已退出，但 running/pending 现场仍未恢复 |
| `DRIVER_STATE_INVALID` | driver 状态文件或字段损坏 |
| `DRIVER_STATE_CONFLICT` | lease 与 state、状态与过程身份相互矛盾 |
| `INFLIGHT_LIVE` | run inflight 对应命令的 process 或 parent 可核验为活进程 |
| `INFLIGHT_UNKNOWN` | 命令身份缺失或不可验证 |
| `INFLIGHT_STALE` | process 与 parent 均可证明退出，但 run inflight 仍存在 |
| `INFLIGHT_CONFLICT` | evidence id、task、phase 或 terminal status 与 inflight 冲突 |
| `INFLIGHT_PROGRESS_MISSING` | inflight 已登记但找不到对应 `command-progress.json` |
| `INFLIGHT_PROGRESS_INVALID` | command progress 损坏或字段不可解释 |

`live`、`unknown`、`malformed` 和交叉矛盾一律阻止迁移。可证明 `dead/stale` 不标记为 live，但仍以 stale/conflict 保留现场，要求后续显式恢复判断。blocker 的 `identity` 使用相对 source path 或 run identity，不使用绝对 PID 作为可移植分类键，也不提供清理授权。
## 旧 session/dispatch 别名与高水位

迁移预览只读盘点每个 run 的 `sessions/index.json` 与 `sessions/dispatch.json`。旧会话和派发是 run 作用域身份：`S01`、`D001` 在所属 run/work 内解析，不能转换成新的全局 `S-000001`、`D-000001` 后覆盖历史，也不能把不同工作项中的同名旧 token 判成全局重复。

`MigrationManifest.aliases` 为每条旧身份保留一行作用域别名：

| 字段 | 含义 |
|---|---|
| `object_kind` | `session` 或 `dispatch` |
| `legacy_token` | 原始 `S01`、`S02`、`D001` 等 token |
| `alias_kind` | `registry_session` 或 `dispatch_session_hint` |
| `scope_identity` / `run_identity` | 所属旧 run |
| `work_identity` | 所属旧 work 编号或 UID |
| `plan_task` | 对应计划任务（如有） |
| `source_path` | registry 文件路径 |
| `reservation` | `session_hint` 预留为 true |
| `canonical_identity` | 预览阶段固定为 `null`，不得生成新规范身份 |
| `conversion` | 固定 `scope_alias_only` |

`dispatch.json` 中的 `session_hint` 会作为 `object_kind=session`、`alias_kind=dispatch_session_hint` 的预留别名保存，并参与同一作用域的 S 高水位计算。`backup_policy.session_dispatch` 保存每个作用域的审计高水位，不写任何计数器：

```text
policy = preview_only_never_reclaim
sessions[].observed_max     = 已登记 S token 最大值
sessions[].reserved_max     = session_hint 预留最大值
sessions[].declared_next    = index.json 的 next_session
sessions[].effective_next   = max(declared_next, observed_max+1, reserved_max+1)
dispatches[].observed_max   = 已登记 D token 最大值
dispatches[].declared_next  = dispatch.json 的 next_dispatch
dispatches[].effective_next = max(declared_next, observed_max+1)
```

高水位只向上取有效值。`declared_next` 未高于 observed/reserved 最大值时形成稳定 blocker，但 effective next 仍按更高水位保留，避免迁移时回收、重排或重用历史号。

稳定 blocker codes：

| code | 含义 |
|---|---|
| `SESSION_REGISTRY_INVALID` | index.json 损坏或不是对象 |
| `SESSION_REGISTRY_SCHEMA_INVALID` | schema 字段非法 |
| `SESSION_REGISTRY_TASK_MISMATCH` | registry task 与父目录 run 不一致 |
| `SESSION_REGISTRY_WORK_MISMATCH` | registry number 与 work 身份不一致 |
| `SESSION_ENTRIES_INVALID` | sessions/tasks/events/outbox 容器非法 |
| `SESSION_ENTRY_INVALID` | 单条 entry/event/outbox/task mapping 不是对象 |
| `SESSION_ID_INVALID` | S token 格式非法 |
| `SESSION_ID_DUPLICATE` | 同一作用域重复 S token |
| `SESSION_NEXT_INVALID` | `next_session` 不是正整数 |
| `SESSION_NEXT_NOT_ABOVE_OBSERVED` | declared next 未高于 observed/reserved |
| `SESSION_REFERENCE_UNKNOWN` | controller/lease/event/outbox/task 引用未知会话 |
| `SESSION_TASK_UNKNOWN` | session 绑定的计划任务不在父 run |
| `SESSION_REGISTRY_SCOPE_UNKNOWN` | 旧 registry 无法归属到唯一 work/run |
| `SESSION_DISPATCH_SCOPE_AMBIGUOUS` | 同一 run identity 有多个可解释来源 |
| `DISPATCH_REGISTRY_INVALID` | dispatch.json 损坏或不是对象 |
| `DISPATCH_REGISTRY_SCHEMA_INVALID` | schema 字段非法 |
| `DISPATCH_REGISTRY_TASK_MISMATCH` | registry task 与父目录 run 不一致 |
| `DISPATCH_ENTRIES_INVALID` | dispatches/cleanup 容器非法 |
| `DISPATCH_ENTRY_INVALID` | 单条 dispatch/cleanup entry 不是对象 |
| `DISPATCH_ID_INVALID` | D token 格式非法 |
| `DISPATCH_ID_DUPLICATE` | 同一作用域重复 D token |
| `DISPATCH_NEXT_INVALID` | `next_dispatch` 不是正整数 |
| `DISPATCH_NEXT_NOT_ABOVE_OBSERVED` | declared next 未高于 observed |
| `DISPATCH_SESSION_HINT_INVALID` | session_hint 存在但格式非法 |
| `DISPATCH_TASK_UNKNOWN` | dispatch 绑定的计划任务不在父 run |
| `DISPATCH_TASK_ENTRIES_INVALID` | 父 run tasks 容器非法，dispatch 任务无法核验 |
| `DISPATCH_REFERENCE_UNKNOWN` | cleanup 引用未知 dispatch |
| `DISPATCH_REGISTRY_SCOPE_UNKNOWN` | 旧 dispatch registry 无法归属到唯一 work/run |

预览不创建、修复、推进或回退计数器，也不初始化权威分配库。无法解释的旧 token 或作用域 fail closed，只保留审计高水位供后续迁移任务使用。
## 主要 blocker

预览采用 fail closed。以下条件至少形成一个 blocker：

- JSON、SQLite、Git 或 TODO 来源不可读。
- work UUID、work 编号、run identity 或 TODO marker 重复。
- record 的 UUID、编号、`run_id`、folder 或目录与 run 状态冲突。
- 一个 run 被多个 work 引用，或一个 work 有多个合理解释。
- 文档索引缺少 work、hash stale，或索引的 path/number/kind 与正本冲突。
- run 的 root 或 project identity 不一致。
- 显式 binding 指向存在但未注册的受管理 workspace。
- 同一物理路径、branch 或 Git common dir 被多个 owner 声明。
- 已注册 worktree 没有显式 owner。
- TODO transaction 未恢复、records 缺失/损坏、marker 非法或重复。
- TODO marker 与 backlog records 不能双向解释，或原文路径/digest 不一致。
- 候选任务目录没有 `record.json`。
- SQLite 来源被活跃或保留句柄占用，或无法捕获出一致私有快照。
- topology 的 `workspace_root` 与 `worktree_path` 指向不同资源。
- 活 writer、活/未知 driver worker、未知或矛盾 inflight。
- 预览期间文件字节、执行身份观察或规范化 Git inventory 发生变化。

外部用户目录是 `external_release_only`，不要求 GSP 拥有删除权。未知 owner 使用 `retain_unknown`。这两种状态都可伴随 blocker，但不会进入删除集合。

## Workspace 处置

资源分类不是处置授权。`resources` 中始终设置 `deletable=false`，`disposed` 默认也为 `false`：

| disposition | 后续边界 |
|---|---|
| `workspace_missing` | 不重建；没有持久处置证明不得称 disposed |
| `retain_dirty` | 保留，阻止删除、关闭和 landed 声称 |
| `retain_unmerged` | 保留，阻止删除和把分支当作已合并 |
| `external_release_only` | 只允许释放 claim，不删除目录 |
| `retain_unknown` | 保留，先解决 owner 或 Git 归属 |
| `eligible_after_proof` | 只能进入后续独立证明/清理任务，仍不可由 T03 删除 |
| `control_only` | 控制根位置，不作为源码 worktree 删除候选 |

`clean+merged` 永远不能直接变成 `disposed`。后续任务必须重新核对资源 owner、集成证明、实际 Git 状态和宿主动作回执。

## 完整性验证

`MigrationManifest` 的 `uid` 覆盖除 `uid` 外的 manifest 内容。`source_digest` 覆盖相关
文件字节、候选目录集合、规范化 Git inventory、不含绝对 PID 的执行现场状态摘要，以及旧
session/dispatch registry 字节。不可读来源以稳定的 `unavailable:<reason>` 表示，不能被
当成空文件或 0 字节成功。修改 `sessions/index.json` 或 `sessions/dispatch.json` 后，
旧 manifest 必须因 `MIGRATION_SOURCE_CHANGED` 失效并重新预览。

`verify_source(root, manifest)`：

1. 重算并验证 manifest 内容摘要；
2. 重算当前 source digest；
3. 来源变化时抛出 `MIGRATION_SOURCE_CHANGED`；
4. manifest 被修改时抛出 `MIGRATION_MANIFEST_CHANGED`；
5. 不修复、不刷新、不重试写入任何来源。

来源发生变化后必须重新生成预览。旧 manifest 不能通过手工更新 digest 继续使用。

## 兼容与后续任务

T03 复用 T01 的显式 identity 规则和 T02 的 `MigrationManifest`，不另建兼容双写层。
work/run/TODO 共用的 work high-water 会纳入旧 run 与权威 work 序列，TODO 编号使用独立
high-water，不能因外形相同而互相推高或覆盖。T03 不注册公共 CLI、不执行 apply、不备份、
不迁移、不删除资源，也不修改正本；这些属于后续任务。

验证入口：

```text
python -m unittest discover -s tests -p test_orchestration_migration_preview.py -v
python -m unittest discover -s tests -p test_topology_context.py -v
python -m unittest discover -s tests -p test_document_migration.py -v
```

## 当前显式维护入口

前面的 T03 内容保留为迁移预览的历史契约。当前运行入口是
`orchestration-migrate`；不带 `--action` 时只执行 `preview`：

```text
python <gsp.py 绝对路径> --project <项目> orchestration-migrate
```

默认预览保持字节只读：不创建身份库、不写迁移请求、不移动或删除来源，并在结束前复核来源未变。
`stage`、`apply`、`resume`、`restore` 和 `adopt` 都是显式维护动作；选中动作后仍必须加
`--apply` 才执行。只给 `--action` 而不给 `--apply` 返回 `dry_run=true` 和
`pending_action`，不调用对应维护实现：

```text
python <gsp.py> --project <项目> orchestration-migrate --action stage --manifest <预览JSON> --request-uid <request> --apply
python <gsp.py> --project <项目> orchestration-migrate --action apply --manifest <预览JSON> --request-uid <request> --apply
python <gsp.py> --project <项目> orchestration-migrate --action resume --request-uid <request> --apply
python <gsp.py> --project <项目> orchestration-migrate --action restore --request-uid <request> --apply
python <gsp.py> --project <项目> orchestration-migrate --action adopt --legacy-run <旧run> --new-run <新run> --request-uid <request> --apply
```

`stage/apply` 使用先前的预览 JSON 并再次核对来源；`resume/restore` 续做或恢复同一 request；
`adopt` 只显式接收一个 legacy-only run，不自动重放旧批准、封存、会话或回执。预览文件应放在
源码检出之外。本次仅注册并记录入口，未执行原生产切换或真实安装。
