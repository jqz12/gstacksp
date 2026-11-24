# 单命令验证上下文与控制面开销

`status`、`resume`、`ready` 等控制命令会在一次 `dispatch` 内复用同一份验证上下文。实现正本为 `gsp/validation_context.py`，接入点是 `gsp/runtime.py` 的 `cached_value`、`evidence_for` 和 `validate_delivery`。

## 生命周期

每次 `dispatch` 创建一个 `ValidationContext`，命令结束立即丢弃：

- 初始阶段复用 Git 身份、拓扑探针、源指纹、契约摘要、回执选择和交付验证结果。
- 进入 `final` 阶段时清空全部缓存，重新读取账本、源内容、回执和日志摘要。
- `atomic` 写入、可变 Git 子命令和测试命令结束后都会使上下文失效；不会把写前结果用于写后判断。
- 证据与交付验证按任务、工作区、指纹、契约、纪元、入口和回执列表分键；不同入口不会互相顶替。

最终门禁只在一个未变化阶段内去重，不跨命令缓存安全结论。两次命令之间外部修改源码、日志或账本仍会被重新发现；最终复核结束后的任意外部写入仍是文件系统检查的竞态边界。

## 廉价预检

`close`、`ready`、`advance` 在通用 `Project.state` 昂贵路径之前先调用 `preflight_command(project, state_snapshot, command, args)`。预检只读取最小账本、参数和涉及的流程文档：

- `close` 先检查 work 档案、关闭条件、归档阶段和 reconcile 边界。缺少 `closure_condition` 时直接返回 `WORK_CLOSURE_CONDITION_REQUIRED`，不运行 Git，也不扫描源码或回执。
- `ready` 先检查控制器阶段、inflight、待重新验证任务、未完成任务和 review 报告；缺少 review 时返回结构化 `REVIEW_MISSING`，不再先做完整交付验证。
- `advance` 先检查阶段顺序和已知阻塞（任务、scope、设计/计划批准、拓扑、review）。`handoff` 复用同一组阻塞以只读预览呈现。

预检结果使用 `code`、`detail`、`required_action`、`stage`、`documents`、`evidence` 结构。空列表只表示“没有已知廉价阻塞”，不表示验证通过；通过预检后仍必须执行一次完整 `Project.state`、证据、集成、审查和授权门禁。预检函数不写状态，也不把旧账本或 brief 快照当作交付结论。

未批准设计的草稿 `task-add` 保持 `design`，不会把状态无条件推进到 `plan`；设计批准后的任务登记仍保持 `plan`，已封存计划和执行阶段新增任务仍会使旧计划封存失效并要求重新封存。

## 阶段示例

`ready` 命令：

1. 读取账本并检查绑定/HEAD，跳过重复的交付预验。
2. 开始 `final` 阶段，执行一次完整 `validate_delivery`。
3. 核对账本在最终验证期间未被改写，再写入 `ready` 状态和交付报告。

`status`/`resume` 保留原有权威验证语义；同一命令内只做一次相同输入的完整交付检查。`run` 的 pre/post 源扫描不受去重影响，命令结束后缓存失效，回执使用真实的 post-command 身份和内容摘要。

预检只减少试探式错误的首轮开销，不改变成功命令的安全路径。证据篡改、未完成任务、HEAD 变化、控制器阶段或授权不足仍由完整门禁拒绝。

## 廉价的 `status --brief`

`status <id> --brief` 只读取 `.gsp/runs/<id>/state.json`，不运行 Git、不扫描源码、不获取工程写锁，也不调用完整 `Project.state`。输出固定包含：

- `recorded_status`：账本记录值；
- `validation: "NOT_CHECKED"`；
- `not_authoritative: true`。

它适合查看进度和 inflight 摘要，不能用于证明任务当前仍可交付。需要权威结论时运行普通 `status`、`resume` 或交付门禁。

## 性能预算

五任务夹具的稳定调用预算：

| 命令 | Git 调用上限 |
|---|---|
| `status`（ready） | 35 |
| `resume`（ready） | 35 |
| `ready` | 45 |

单任务 running/state 的既有 `<=4` Git 预算不退化。`run` 的 pre/post 扫描和输入真实变化后的必要重验单独统计，不算重复浪费。

配对采样使用 `scripts/profile_control_plane.py`：

```powershell
python <workspace>/scripts/profile_control_plane.py --source-root E:/GstackSP --repetitions 5 --output <task>/evidence/performance-before.json
python <workspace>/scripts/profile_control_plane.py --source-root <workspace> --repetitions 5 --output <task>/evidence/performance-after.json
```

脚本在被测进程内从 `--source-root` 加载 `gsp`，记录真实代码根、HEAD、每个样本的耗时、Git 调用、扫描次数和缓存命中；秒数只用于同机配对报告，不作为单测硬阈值。
