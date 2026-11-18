---
name: gsp-verify
description: "Use when 准备宣布任务完成、提交交付准备或复用测试证据之前."
---

## GstackSP adaptation (only the explicitly listed differences)

Read the [SP adaptation contract](../gsp-workflow/references/sp-adapter.md) first. The complete upstream body follows, not a summary. Resolve upstream `superpowers:*` names using the contract; never load a second SP installation. Relative prompts/scripts are based on the corresponding ORIGINAL.md directory. Resource links below are redirected to the complete preserved files. Missing host capabilities must be reported honestly.

Artifact layout is adapted, not the method: use the paths returned by GSP status/resume. New tasks live in docs/work/<year>/<month>/<stable-folder>/ (spec.md, plan.md and stage artifacts); existing bindings stay authoritative. Read [document governance](../gsp-workflow/references/document-governance.md) for saved ideas, module relations, discovery, health reports and controlled migration. Do not create a second spec or plan at the upstream default path.

Use the [output and handoff contract](../gsp-workflow/references/output-handoff.md) for actual results, honestly attributed skills used, evidence, next action and owner. Return nested methods to the current task; continue authorized work without a redundant approval.

### GSP stage integration

交付前读取 todo-list 和人读待办原文；人工历史内容也要核对，不能只看机器计数。核实关联任务与问题验收，再用 todo-done 带实际版本关闭；没有版本时如实留空，不捏造。完成证据和历史在 .gsp/backlog/records.json，正文只显示完成版本/日期。人工移到 Completed 或改动已完成正文不算验证通过；先核对关闭再跑最终 all 回归，待办与执行记录变化都会使旧证据过期。

## 跨会话开发（仅 `intent=cross-session`）

单会话仍是默认路径。没有 `cross-session` 意图的任务不创建 session registry、brief/report 目录，也不增加额外门禁。启用跨会话后，一个 GSP 工作项仍只有一份 `spec.md`、`plan.md`、`decisions.md`、`review.md` 和 `delivery.md`，这就是唯一总体计划及其配套记录。总体详细执行计划由 controller 主会话维护，它负责目标、非目标、任务图、依赖、公共接口、基线、worktree、允许和禁止路径、测试矩阵以及最终集成验收。子会话读取总体计划和自己的 brief，自主安排局部阅读、实现和局部测试顺序，不能复制 `plan-v2.md`，也不能替代 controller 做跨任务裁决。

使用 `<gsp> session-register` 分配稳定的 `S01`、`S02` 会话身份。`T01`、`T02` 仍是计划任务身份。controller 用 `<gsp> session-brief` 派发局部输入，worker 用 `<gsp> session-report` 回传 `DONE`、`BLOCKED` 或 `CHANGE_REQUEST`。换主会话只用 `<gsp> session-handoff` 接管同一 registry 和总体计划。报告替换前会把旧版本移入任务 `sessions/history/`，不会覆盖证据。

主会话发布时使用 `<gsp> session-dispatch`。它按依赖图批量计算 ready 任务，检查并发上限、`claimed_paths` 和路径重叠，并生成 `Dnnn`、预分配的 `Snn`、`Tnn-brief.md` 和 `Tnn-prompt.md`。没有显式 worktree 时，正式 worker 默认使用 `--worktree-mode auto`，GSP 创建受控的 managed worktree 和对应分支，并把实际路径、分支和基线写入 dispatch。显式 `--worktree` 或 `--worktree-mode bind` 只绑定宿主已有目录，GSP 取得写租约但不取得删除权。Codex 目标返回 `adapter-ready`，controller 再调用宿主原生线程创建、worktree 绑定、消息发送和有界等待入口。没有连接器的平台进入 `manual-required`，用户转发同一份 prompt。CLI 不伪造线程创建或外部消息发送。

controller 在发布前和进入等待时使用 `<gsp> session-progress`。摘要必须展示总体进度、发布次数、独立任务数、DONE 报告数、accepted 数、已清理 worktree 数、managed worktree 数、bound worktree 数、cleanup-pending 数、运行中、阻塞、CHANGE_REQUEST、待人工转发和下一阶段任务表。总体进度只按 `accepted_tasks / planned_tasks` 计算，不能把报告、接受或 prompt 生成冒充清理完成。

`session-report` 会把结构化报告追加到 durable outbox。主会话或有界 watcher 使用 `<gsp> session-watch` 按事件 ID 消费，重复消费保持幂等。Codex 宿主用有界 `wait_threads` 等待，不要求 controller 进程全天轮询。主会话关闭时事件留在 outbox，下一次 resume 继续消费。controller 检查报告、测试和集成结果后，使用 `<gsp> session-accept <run> <task> --session <Snn>` 明确接受任务。`session-accept` 会记录接受事件、释放 worker lease，并按 worktree 所有权自动处理清理。干净的 managed worktree 会被移除并执行 prune，bound worktree 只释放租约并保留目录。

接受和清理是两个独立状态。managed worktree 遇到脏文件、锁定、Git 非零退出或未知结果时保留目录和分支，状态为 `cleanup-pending`，不计入 `cleaned_worktrees`。主会话随后使用 `<gsp> session-clean --retry` 重试 cleanup-pending。重复接受、释放或重试必须幂等。正常路径不要求用户手动删除 GSP 创建的 worktree，也不删除分支、commit、报告或显式绑定目录。

跨会话写入需要三层门禁。controller lease 保证同一任务只有一个主会话，worktree write lease 保证同一源码目录同一时刻只有一个可写会话，`--path` 声明和已接线的 direct-edit hook 在 Edit、Write、apply_patch 前复核会话身份、写租约和路径归属。未注册会话、未声明路径、租约冲突和重叠声明直接拒绝，分别返回 `SESSION_WRITE_LEASE_REQUIRED`、`SESSION_PATH_NOT_CLAIMED` 或 `SESSION_PATH_CONFLICT`。该机制不是文件系统沙箱。Shell、外部编辑器、MCP 或未接线宿主只能记录为 `NOT_CHECKED`，不能把事后 Git 指纹检查说成编辑前保护。

子 agent 与正式子会话必须区分。子 agent 是当前主会话内的临时辅助，不拥有独立 `Snn`、worktree、report 或恢复边界。正式子会话才计入 dispatch 统计，并拥有 `Snn`、`Tnn`、brief、report、outbox 和 worktree 清理生命周期。普通单会话任务不创建这些资料，原有 `new`、`approve`、`seal-plan`、`task-start`、`run`、`review`、`ready` 和恢复语义保持兼容。

For document onboarding or governance, compare the original user objective with the spec, plan and delivered artifacts; a narrower spec does not silently supersede that objective. Use the [governance execution rules](../gsp-workflow/references/document-governance-execution.md). Inventory or migration preview is not completed migration or acceptance. Required unfinished work remains blocking unless the user actually changes the scope.

Before handing this stage onward, apply the [artifact fidelity checks](../gsp-workflow/references/artifact-fidelity.md) to the actual bound artifacts. Read the complete upstream method below; this integration is not its replacement. Plan authors use plan-check then seal-plan --check-structure after the upstream self-review. Structural success, preserved source text and a skills-used table do not prove semantic compliance.

Use the same artifact contract to route applicable engineering responsibilities from design to tasks, evidence and current documentation: data/recovery, configuration/dependencies, integration, project materials, delivery and maintenance. Expand only the topics triggered by this change; no extra mandatory table, document set or skill sequence. Headless plan workers return self-reviewed content; the controller owns registration and sealing.

Stage transitions use the unified controller: preview with `<gsp> handoff <id> --to <stage>` and advance with `<gsp> advance <id> --to <stage>`. Read `next_skill` and pending gates; do not treat chat text as a handoff or mutate workflow state around a failed advance.

<!-- sp-preserved-body:begin -->
# Verification Before Completion

## Overview

**Core principle:** Evidence before claims, always.

**Violating the letter of this rule is violating the spirit of this rule.**

## The Iron Law

```
NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE
```

If you haven't run the verification command in this message, you cannot claim it passes.

## The Gate Function

```
BEFORE claiming any status or expressing satisfaction:

1. IDENTIFY: What command proves this claim?
2. RUN: Execute the FULL command (fresh, complete)
3. READ: Full output, check exit code, count failures
4. VERIFY: Does output confirm the claim?
   - If NO: State actual status with evidence
   - If YES: State claim WITH evidence
5. ONLY THEN: Make the claim

Skip any step = lying, not verifying
```

## Common Failures

| Claim | Requires | Not Sufficient |
|-------|----------|----------------|
| Tests pass | Test command output: 0 failures | Previous run, "should pass" |
| Linter clean | Linter output: 0 errors | Partial check, extrapolation |
| Build succeeds | Build command: exit 0 | Linter passing, logs look good |
| Bug fixed | Test original symptom: passes | Code changed, assumed fixed |
| Regression test works | Red-green cycle verified | Test passes once |
| Agent completed | VCS diff shows changes | Agent reports "success" |
| Requirements met | Line-by-line checklist | Tests passing |

## Red Flags - STOP

- Using "should", "probably", "seems to"
- Expressing satisfaction before verification ("Great!", "Perfect!", "Done!", etc.)
- About to commit/push/PR without verification
- Trusting agent success reports
- Relying on partial verification
- Thinking "just this once"
- Tired and wanting work over
- **ANY wording implying success without having run verification**

## Rationalization Prevention

| Excuse | Reality |
|--------|---------|
| "Should work now" | RUN the verification |
| "I'm confident" | Confidence ≠ evidence |
| "Just this once" | No exceptions |
| "Linter passed" | Linter ≠ compiler |
| "Agent said success" | Verify independently |
| "I'm tired" | Exhaustion ≠ excuse |
| "Partial check is enough" | Partial proves nothing |
| "Different words so rule doesn't apply" | Spirit over letter |

## Key Patterns

**Tests:**
```
✅ [Run test command] [See: 34/34 pass] "All tests pass"
❌ "Should pass now" / "Looks correct"
```

**Regression tests (TDD Red-Green):**
```
✅ Write → Run (pass) → Revert fix → Run (MUST FAIL) → Restore → Run (pass)
❌ "I've written a regression test" (without red-green verification)
```

**Build:**
```
✅ [Run build] [See: exit 0] "Build passes"
❌ "Linter passed" (linter doesn't check compilation)
```

**Requirements:**
```
✅ Re-read plan → Create checklist → Verify each → Report gaps or completion
❌ "Tests pass, phase complete"
```

**Agent delegation:**
```
✅ Agent reports success → Check VCS diff → Verify changes → Report actual state
❌ Trust agent report
```

## When To Apply

**ALWAYS before:**
- ANY variation of success/completion claims
- ANY expression of satisfaction
- ANY positive statement about work state
- Committing, PR creation, task completion
- Moving to next task
- Delegating to agents

**Rule applies to:**
- Exact phrases
- Paraphrases and synonyms
- Implications of success
- ANY communication suggesting completion/correctness

<!-- sp-preserved-body:end -->
