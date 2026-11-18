---
name: gsp-review
description: "Use when 一个任务、主要功能或整条分支需要规格符合性和代码质量审查时."
---

## GstackSP adaptation (only the explicitly listed differences)

Read the [SP adaptation contract](../gsp-workflow/references/sp-adapter.md) first. The complete upstream body follows, not a summary. Resolve upstream `superpowers:*` names using the contract; never load a second SP installation. Relative prompts/scripts are based on the corresponding ORIGINAL.md directory. Resource links below are redirected to the complete preserved files. Missing host capabilities must be reported honestly.

Artifact layout is adapted, not the method: use the paths returned by GSP status/resume. New tasks live in docs/work/<year>/<month>/<stable-folder>/ (spec.md, plan.md and stage artifacts); existing bindings stay authoritative. Read [document governance](../gsp-workflow/references/document-governance.md) for saved ideas, module relations, discovery, health reports and controlled migration. Do not create a second spec or plan at the upstream default path.

Use the [output and handoff contract](../gsp-workflow/references/output-handoff.md) for actual results, honestly attributed skills used, evidence, next action and owner. Return nested methods to the current task; continue authorized work without a redundant approval.

### GSP stage integration

每个实际问题须明确立即修复、按依据延期或经核实不成立；延期项登记根 TODOS.md，最终审查必须读取，不得只说以后处理。本次必需项用 --blocking --origin <本需求ID>，ready 会检查它。

## 跨会话开发（仅 `intent=cross-session`）

单会话仍是默认路径；没有 `cross-session` 意图的任务不创建 session registry、brief/report 目录，也不增加额外门禁。启用跨会话后，一个 GSP 工作项仍只有一份 `spec.md`、`plan.md`、`decisions.md`、`review.md` 和 `delivery.md`，这就是唯一总体计划及其配套记录。总体详细执行计划由当前 controller 主会话维护，它负责目标、非目标、任务图、依赖、公共接口、worktree、允许/禁止路径、测试和集成验收。子会话读取总体计划和自己的 brief，自主决定局部阅读顺序、实现步骤与局部测试顺序，不能复制 `plan-v2.md` 或替代 controller 做跨任务裁决。

使用 `<gsp> session-register` 分配稳定的 `S01`、`S02` 会话身份，`T01`、`T02` 仍是计划任务身份。控制器用 `<gsp> session-brief` 派发局部输入，worker 用 `<gsp> session-report` 回传 `DONE`、`BLOCKED` 或 `CHANGE_REQUEST`，换主会话只用 `<gsp> session-handoff` 接管同一 registry 和总体计划。报告替换前会把旧版本放入任务 `sessions/history/`，不会覆盖证据。

主会话发布时使用 `<gsp> session-dispatch`。它按依赖图批量计算 ready 任务，检查并发上限、独立 worktree、`claimed_paths` 和路径重叠，并生成 `Dnnn`、预分配的 `Snn`、`Tnn-brief.md` 和 `Tnn-prompt.md`。Codex 目标默认进入 `adapter-ready`，当前 controller 再调用宿主原生线程、worktree 和等待入口。没有连接器的平台进入 `manual-required`，用户转发同一份 prompt。CLI 不伪造线程创建或外部消息发送。

controller 在发布前和进入等待时使用 `<gsp> session-progress`。摘要必须展示总体进度、发布次数、独立任务数、DONE 报告数、accepted 数、已清理 worktree 数、运行中、阻塞、CHANGE_REQUEST、待人工转发和下一阶段任务表。总体进度只按 `accepted_tasks / planned_tasks` 计算。`session-report` 会把报告写入 durable outbox，`session-watch` 按事件 ID 幂等消费。Codex 宿主用有界 `wait_threads` 等待，controller 关闭时事件保留到下一次 resume。主会话检查并接受报告后才能用 `<gsp> session-clean` 记录 `archived`、`released` 或 `removed`，清理失败不计入 cleaned。

跨会话的安全边界是三层：controller 租约保证同一任务只有一个主会话，worktree write lease 保证同一源码目录同一时刻只有一个可写会话，`--path` 声明和已接线的 direct-edit hook 在 Edit/Write/apply_patch 前检查会话身份、写租约与路径归属。未注册会话、未声明路径、路径冲突或旧 controller 交接后继续编辑都必须拒绝，分别返回 `SESSION_WRITE_LEASE_REQUIRED`、`SESSION_PATH_NOT_CLAIMED` 或 `SESSION_PATH_CONFLICT`。Shell、外部编辑器、未接线宿主和其他未覆盖工具只能记录为 `NOT_CHECKED`，不能宣称拥有文件系统级沙箱。

子 agent 是当前主会话内的临时辅助，不拥有独立 `Snn`、worktree、report 或恢复边界。正式子会话才计入 dispatch 统计，并拥有 `Snn`、`Tnn`、brief、report、outbox 和清理生命周期。普通单会话任务不创建这些资料，原有 `new`、`approve`、`seal-plan`、`task-start`、`run`、`review`、`ready` 和恢复语义保持兼容。

Read these complete originals and their dependencies before using the corresponding mode:

- [receiving-code-review](../gsp-workflow/upstream/skills/receiving-code-review/ORIGINAL.md)

For document onboarding or governance, compare the original user objective with the spec, plan and delivered artifacts; a narrower spec does not silently supersede that objective. Use the [governance execution rules](../gsp-workflow/references/document-governance-execution.md). Inventory or migration preview is not completed migration or acceptance. Required unfinished work remains blocking unless the user actually changes the scope.

Before handing this stage onward, apply the [artifact fidelity checks](../gsp-workflow/references/artifact-fidelity.md) to the actual bound artifacts. Read the complete upstream method below; this integration is not its replacement. Plan authors use plan-check then seal-plan --check-structure after the upstream self-review. Structural success, preserved source text and a skills-used table do not prove semantic compliance.

Use the same artifact contract to route applicable engineering responsibilities from design to tasks, evidence and current documentation: data/recovery, configuration/dependencies, integration, project materials, delivery and maintenance. Expand only the topics triggered by this change; no extra mandatory table, document set or skill sequence. Headless plan workers return self-reviewed content; the controller owns registration and sealing.

Stage transitions use the unified controller: preview with `<gsp> handoff <id> --to <stage>` and advance with `<gsp> advance <id> --to <stage>`. Read `next_skill` and pending gates; do not treat chat text as a handoff or mutate workflow state around a failed advance.

<!-- sp-preserved-body:begin -->
# Requesting Code Review

Dispatch a code reviewer subagent to catch issues before they cascade. The reviewer gets precisely crafted context for evaluation — never your session's history.

**Core principle:** Review early, review often.

## When to Request Review

**Mandatory:**
- After each task in subagent-driven development
- After completing major feature
- Before merge to main

**Optional but valuable:**
- When stuck (fresh perspective)
- Before refactoring (baseline check)
- After fixing complex bug

## How to Request

**1. Get git SHAs:**
```bash
BASE_SHA=$(git rev-parse HEAD~1)  # or: git merge-base origin/main HEAD
HEAD_SHA=$(git rev-parse HEAD)
```

**2. Dispatch code reviewer subagent:**

Dispatch a `general-purpose` subagent, filling the template at [code-reviewer.md](../gsp-workflow/upstream/skills/requesting-code-review/code-reviewer.md)

**Placeholders:**
- `{DESCRIPTION}` - Brief summary of what you built
- `{PLAN_OR_REQUIREMENTS}` - What it should do
- `{BASE_SHA}` - Starting commit
- `{HEAD_SHA}` - Ending commit

**3. Act on feedback:**
- Fix Critical issues immediately
- Fix Important issues before proceeding
- Note Minor issues for later
- Push back if reviewer is wrong (with reasoning)

## Example

```
[Just completed Task 2: Add verification function]

You: Let me request code review before proceeding.

BASE_SHA=$(git log --oneline | grep "Task 1" | head -1 | cut -d' ' -f1)
HEAD_SHA=$(git rev-parse HEAD)

[Dispatch code reviewer subagent]
  DESCRIPTION: Added verifyIndex() and repairIndex() with 4 issue types
  PLAN_OR_REQUIREMENTS: Task 2 from docs/superpowers/plans/deployment-plan.md
  BASE_SHA: a7981ec
  HEAD_SHA: 3df7661

[Subagent returns]:
  Strengths: Clean architecture, real tests
  Issues:
    Important: Missing progress indicators
    Minor: Magic number (100) for reporting interval
  Assessment: Ready to proceed

You: [Fix progress indicators]
[Continue to Task 3]
```

## Common Rationalizations

| Excuse | Reality |
|--------|---------|
| "I'll just review the diff myself instead of dispatching a reviewer" | You're the coordinator — reviewing the diff inline burns the context window you need to keep driving the work. Dispatch a reviewer subagent: the diff and the evaluation live in its context, and only the findings come back to you. |
| "The reviewer needs my whole session history to understand the change" | Hand it precisely crafted context, never your session's history. That keeps the reviewer on the work product, not your thought process. |

## Red Flags

**Never:**
- Skip review because "it's simple"
- Ignore Critical issues
- Proceed with unfixed Important issues
- Argue with valid technical feedback

**If reviewer wrong:**
- Push back with technical reasoning
- Show code/tests that prove it works
- Request clarification

See template at: [code-reviewer.md](../gsp-workflow/upstream/skills/requesting-code-review/code-reviewer.md)

<!-- sp-preserved-body:end -->
