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

原有单会话的设计确认、计划封存、`task-start`、测试收据、审查、验证和交付语义保持不变。`cross-session` 只在任务明确选择该意图时增加 session registry、租约、brief/report 和 handoff；旧任务没有 `sessions/` 也不会因此阻塞恢复。公开规则的生成源在 `scripts/build_skills.py`，共享约束在 `skills/gsp-workflow/references/contract.md` 与 `document-governance.md`，修改后必须重建并校验生成技能。
