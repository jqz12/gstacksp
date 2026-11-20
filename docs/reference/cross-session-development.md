# 主会话协调的跨会话开发模式

GSP 把“一个主会话管理多个独立开发会话”作为 `cross-session` 可选模式。它适合需求已经确认、任务可以拆成互相独立的边界，并且需要在不同 Codex/Claude 聊天之间继续工作的项目。普通小修改仍走单会话路径，不会因为安装了这个扩展而多出 registry、brief、report 或额外审批。

## 它解决什么问题

单个主会话通常同时承担需求澄清、总体计划、局部实现、测试、冲突裁决和交付，长任务会让上下文成为瓶颈。跨会话模式把责任拆成两层：controller 主会话维护目标和全局状态，worker 子会话在自己的任务边界内实现和验证。主会话换宿主或换聊天线程时，接管的是同一个 GSP 工作项，而不是从聊天记录猜测进度。

这个模式不会只靠约定解决覆盖问题。你遇到的“另一个会话已经提交同名 unit 和测试，后一个会话把工作区文件覆盖”的情况有三层防护：

1. **controller lease**：同一任务同一时刻只能有一个 controller。第二个主会话必须等待或通过 `session-handoff` 接管。
2. **worktree write lease**：同一 worktree 同一时刻只能有一个可写 session。第二个写会话在注册时返回 `WORKSPACE_WRITE_LEASE_HELD`，它不能等到提交后才暴露冲突。
3. **路径声明和 direct-edit hook**：可写 session 必须声明相对于 worktree 的文件或目录。已接线的 Edit、Write、NotebookEdit 和 `apply_patch` 在真正写入前检查身份、租约和路径；未声明的文件返回 `SESSION_PATH_NOT_CLAIMED`，租约或路径冲突返回 `SESSION_WRITE_LEASE_REQUIRED` 或 `SESSION_PATH_CONFLICT`。

因此，两个会话不能在同一个 room/worktree 中先后写同一批文件后再互相覆盖。Shell、外部编辑器、MCP 或尚未接线的宿主仍可能绕过编辑前 hook；它们必须记录为 `NOT_CHECKED`，不能被描述成完整文件系统沙箱。Git 提交后的指纹检查是事后证据，不能替代写入前门禁。

## 计划由谁写

详细的**总体执行计划由主会话写**，因为它必须保持跨任务一致性。它包含：

- 目标、非目标和验收标准；
- `T01`、`T02` 的拆分、依赖和集成顺序；
- 公共接口、数据契约、基线、branch/worktree；
- 每个会话允许和禁止修改的路径；
- 测试矩阵、集成测试和最终交付条件。

子会话不再复制一份 `plan-v2.md`。它读取总体计划和自己的 brief，自主确定局部阅读顺序、实现步骤和局部测试顺序，然后用固定字段写 report：`STATUS`、`TASK`、`FILES`、`TESTS`、`INTERFACES`、`EVIDENCE`、`NEED_FROM_MAIN`。子会话遇到公共接口、范围、依赖、基线、安全、凭据或外部动作问题时暂停并提交 `CHANGE_REQUEST`，由主会话裁决并更新总体计划或 `decisions.md`。

主会话因此从“逐条操作员”变成“全局控制者”：批量分派和读取报告，集中处理冲突、依赖和集成，最后进行整体审查和交付。局部实现、局部测试、普通机械检查和报告整理不应全部回流到主会话。

## 发布任务和并行批次

总体计划封存后，controller 主会话先登记自己，再运行 `session-dispatch`。不指定 `--task` 时，控制面根据依赖图计算所有 ready 任务。显式指定多个 `--task` 时，它们必须互不依赖，且 worktree 和 `claimed_paths` 不能冲突。

```powershell
<gsp> session-dispatch <run> --session S01 `
  --task T01 --task T02 `
  --target T01=codex --target T02=manual `
  --path T01=src/data --path T02=src/api `
  --worktree-mode auto `
  --max-parallel 2
```

没有显式指定 worktree 时，`--worktree-mode auto` 是正式 worker 的默认路径。GSP 在受控的 `.gsp/worktrees/sessions/<GSP>/` 下创建独立 managed worktree 和分支，并把真实 path、branch、base、lease 和所有权写入 dispatch。派发失败或批量中途失败时会回滚本次已创建的 managed worktree，避免留下不可追踪资源。

如果宿主已经准备好目录，传入 `--worktree <path>` 或 `--worktree-mode bind`。这类目录只做 bound，GSP 取得写租约并记录绑定关系，任务接受后只释放租约，不自动删除目录、分支或宿主文件。显式绑定的目录仍需满足路径和 worktree 预检。

每个批次都会产生唯一的 `Dnnn`、预分配的 `Snn` 提示、独立的 brief、可复制 prompt、平台和派发方式。Codex 目标返回 `adapter-ready`，当前主会话随后调用宿主原生的线程创建、worktree 绑定、消息发送和有界等待入口。控制面记录线程返回的身份后，子会话再按同一 `Snn` 注册。GSP Python CLI 不假装拥有 MCP 客户端，也不会在没有宿主回调时伪造线程已创建。

没有连接器的平台返回 `manual-required`。主会话把 `sessions/Tnn-prompt.md` 转发给用户，用户只需在对应平台粘贴一次。提示词内容与 Codex 自动发送的内容来自同一份任务记录，包含总体计划、brief、目标、允许路径、接口、测试、依赖、禁止范围和固定 report 命令。任务完成后用户带回 report 路径或报告内容，主会话仍通过 `session-report` 写入同一 registry。

同一批次中可以并行发布多个独立任务。控制面拒绝重复任务、依赖未接受的任务、同一 worktree 的第二个可写任务以及父子或相同路径声明。重试必须显式使用 `--retry`，旧 prompt 会进入 `sessions/history/`，旧 dispatch 记录保留为 `superseded`。

## 主会话发布和等待时的固定摘要

主会话在每次发布前和进入等待时运行 `session-progress <run>`，并把以下摘要原样展示给用户。总体进度按 `accepted_tasks / planned_tasks` 计算，`DONE` 报告、接受和 worktree 清理分别统计。`managed_worktrees`、`bound_worktrees` 和 `cleanup_pending_worktrees` 用来区分 GSP 拥有的目录、宿主绑定目录和待重试现场。

```text
GSP-<编号> 主任务状态
当前主会话：S01 controller
当前阶段：dispatch / wait / integrate
总体进度：2 / 8 个计划任务已接受（25.0%）
子会话统计：已发布 4 次，独立任务 3 个，DONE 2 个，已接受 2 个，已清理 worktree 1 个，managed 1 个，bound 1 个，cleanup-pending 0 个，运行中 1 个，阻塞 0 个，待人工转发 1 个
下一批：T03、T04，等待 T02 的 CHANGE_REQUEST 裁决

| 阶段 | 任务 | 数量 | 状态 | 下一动作 |
|---|---|---:|---|---|
| 已接受 | T01、T02 | 2 | accepted | 进入集成依赖 |
| 运行中 | T03 | 1 | running | 等待 report |
| 待派发 | T04 | 1 | ready | 下一批创建线程 |
| 集成 | T05 | 1 | blocked | 等待 T01、T02 |
| 验证 | T06、T07、T08 | 3 | planned | 集成完成后执行 |
```

机器 JSON 至少包含 `dispatches_total`、`distinct_tasks`、`reports_done`、`accepted_tasks`、`cleaned_worktrees`、`managed_worktrees`、`bound_worktrees`、`cleanup_pending_worktrees`、`running_sessions`、`blocked_tasks`、`change_requests`、`manual_required`、`next_tasks` 和 `phases`。控制器可以在发布命令输出后立即追加这份摘要，也可以在宿主等待器被唤醒后重新输出。

## 完成发现、等待和清理

子会话执行 `session-report` 时会同时写入报告历史和 durable outbox。事件有唯一 ID、任务、会话、状态、报告路径、worktree 和 delivered 标记。`session-watch` 按事件 ID 消费，重复执行不会重复通知。

Codex 任务由宿主使用有界的 `wait_threads` 等待一批线程。任一线程完成或需要主会话裁决时等待器唤醒，主会话读取 report 并处理事件。主会话关闭期间不要求持续轮询，未消费事件会留在 outbox，下一次 resume 或 watcher 先消费积压事件。没有宿主事件适配器的平台只能记录 `manual-required`，不能宣称实时通知。

主会话必须先检查 report、测试和集成结果，再运行 `session-accept` 明确接受任务。接受入口会写入 accepted 事件、释放 worker lease，并按 worktree 所有权触发清理。

```powershell
<gsp> session-accept <run> T01 --session S02 --reason "主会话已接受 report 和测试"
```

清理规则由 worktree 所有权决定。

- 干净的 managed worktree 自动运行 `git worktree remove` 和 `git worktree prune`，进度中的 `cleaned_worktrees` 增加一次。
- bound worktree 只释放租约，目录和分支保留给宿主。
- 脏目录、锁定目录、Git 非零退出或未知结果进入 `cleanup-pending`。目录、分支和报告保留，不计入 `cleaned_worktrees`。

`session-clean --retry` 只用于重试 `cleanup-pending` 或恢复已接受但尚未完成的清理。重复接受、释放和重试保持幂等，正常路径不需要用户手动调用清理入口。

## 文档在哪里

文档归属于 GSP 任务，不归属于聊天线程：

```text
docs/work/YYYY/MM/GSP-<编号>-<稳定主题>/
├─ spec.md                 # 唯一设计正本
├─ plan.md                 # 唯一总体执行计划
├─ decisions.md            # 公共裁决和范围变更
├─ review.md
├─ delivery.md
└─ sessions/
   ├─ index.md             # registry 的人读摘要
   ├─ S01-controller-codex.md
   ├─ S02-controller-claude.md
   ├─ T01-brief.md
   ├─ T01-report.md
   └─ history/             # 被替换的旧 report 和交接材料
```

`S01`、`S02` 是会话身份；`T01`、`T02` 是计划任务身份。branch、worktree、Commit SHA、thread ID、Issue/PR 仍分别保存，不能用一个字段代替另一个。控制器可以用以下命令查看或推进控制面：

```text
<gsp> session-register <run> --role controller --host codex --thread <id> --write-mode write --path <dir>
<gsp> session-register <run> --role worker --host claude --task T01 --worktree <path> --write-mode write --path <file>
<gsp> session-brief <run> T01 --session S01 --owner S02 --title "..." --objective "..." --path <file>
<gsp> session-report <run> T01 --session S02 --status DONE --summary "..." --files <file> --tests "..."
<gsp> session-handoff <run> --from S01 --to S03 --reason "主会话连接中断，接管同一计划"
```

重复 report 会把旧版本移入 `sessions/history/`；它不会覆盖或删除历史证据。未来两次独立开发会得到不同的 GSP 编号和不同目录。同一任务再次打开主会话则继续使用原目录，只增加新的 `Snn` 记录。因此命名不会因为会话数量增加而混淆。

中文标题无法形成 ASCII 主题时，GSP 使用稳定的 run id 作为目录后缀，例如 `GSP-000019-cross-session-development`；不会让未来任务全部退化为同一个 `GSP-xxxxxx-work`。已经存在的历史目录不自动搬迁，`status/resume` 返回的绑定路径始终优先。

## 什么时候不使用

以下情形继续使用单会话或普通并行拓扑：任务很小、没有可独立的边界、子会话需要同时改同一组文件，或者宿主没有可用的 direct-edit hook 且风险无法接受。跨会话不是远程消息队列，也不会自动合并、推送、部署、创建 Issue/PR 或替用户决定业务验收。

## 与原有流程的关系

原有单会话的设计确认、计划封存、`task-start`、测试收据、审查、验证和交付语义保持不变。`cross-session` 只在任务明确选择该意图时增加 session registry、租约、brief/report、dispatch、progress、watch、clean 和 handoff；旧任务没有 `sessions/` 也不会因此阻塞恢复。子 agent 仍可以作为主会话内部的临时只读辅助，不计入正式 `Snn` 派发统计。需要独立 worktree、独立恢复或并行编码时才登记正式子会话。公开规则的生成源在 `scripts/build_skills.py`，共享约束在 `skills/gsp-workflow/references/contract.md` 与 `document-governance.md`，修改后必须重建并校验生成技能。

## 模型路由

跨会话派发可通过 --provider、--model-profile、--model 和 --reasoning-effort 为 worker 记录模型选择，并使用 TASK=VALUE 为单个任务覆盖默认值。主会话继续维护总体计划，worker 只读取 brief/prompt。推荐让主会话保留原生 Codex provider，让 Codex worker 使用独立 profile 连接 Magpie/OpenCode Go；ZCode 和其他平台继续走 manual-required。路由字段只保存非敏感标识，不保存 API key 或 token。完整字段和边界见 [模型路由与宿主适配](model-routing.md)。

