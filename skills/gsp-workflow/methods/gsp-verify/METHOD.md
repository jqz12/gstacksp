<!-- internal-method: gsp-verify -->
# gsp-verify

Owner: `gsp-review`

This is a conditional internal method resource, not a discoverable skill. Load the complete method only when the trigger applies.

## GstackSP adaptation (only the explicitly listed differences)

Read the [SP adaptation contract](../../references/sp-adapter.md) first. The complete upstream body follows, not a summary. Resolve upstream `superpowers:*` names using the contract; never load a second SP installation. Relative prompts/scripts are based on the corresponding ORIGINAL.md directory. Resource links below are redirected to the complete preserved files. Missing host capabilities must be reported honestly.

Artifact layout is adapted, not the method: use the paths returned by GSP status/resume. New tasks live in docs/work/<year>/<month>/<stable-folder>/ (spec.md, plan.md and stage artifacts); existing bindings stay authoritative. Read [document governance](../../references/document-governance.md) for saved ideas, module relations, discovery, health reports and controlled migration. Do not create a second spec or plan at the upstream default path.

Use the [output and handoff contract](../../references/output-handoff.md) for actual results, honestly attributed skills used, evidence, next action and owner. Return nested methods to the current task; continue authorized work without a redundant approval.

Conditional methods are indexed in [methods](../../references/methods.md). Only when the actual 触发条件 applies, read the complete method; do not load every method by default.

### GSP stage integration

交付前读取 todo-list 和人读待办原文；人工历史内容也要核对，不能只看机器计数。核实关联任务与问题验收，再用 todo-done 带实际版本关闭；没有版本时如实留空，不捏造。完成证据和历史在 .gsp/backlog/records.json，正文只显示完成版本/日期。人工移到 Completed 或改动已完成正文不算验证通过；先核对关闭再跑最终 all 回归，待办与执行记录变化都会使旧证据过期。最终 all 回执 hygiene 有未解释的 retained/truncated/errors 时 ready 报 HYGIENE_LEFTOVER；失败运行在上限内的保留不阻塞，但必须检查是否被误当长久缓存。ready 记录 git_clean/branch_merged，未提交或未合并必须在交付里明示。 证据按任务和命令入口选择有效集合，不取最后一张回执；适用失败必须由同一入口后续成功解除，输入、契约、epoch、workspace、入口或日志摘要变化会失效。observe-head 在 HEAD 与验证输入均未变时是 no-op，不重写 review/ready/证据；真实变化仍进入 needs-verification。

For document onboarding or governance, compare the original user objective with the spec, plan and delivered artifacts; a narrower spec does not silently supersede that objective. Use the [governance execution rules](../../references/document-governance-execution.md). Inventory or migration preview is not completed migration or acceptance. Required unfinished work remains blocking unless the user actually changes the scope.

Before handing this stage onward, apply the [artifact fidelity checks](../../references/artifact-fidelity.md) to the actual bound artifacts. Read the complete upstream method below; this integration is not its replacement. Plan authors use plan-check then seal-plan --check-structure after the upstream self-review. Structural success, preserved source text and a skills-used table do not prove semantic compliance.

Use the same artifact contract to route applicable engineering responsibilities from design to tasks, evidence and current documentation: data/recovery, configuration/dependencies, integration, project materials, delivery and maintenance. Expand only the topics triggered by this change; no extra mandatory table, document set or skill sequence. Headless plan workers return self-reviewed content; the controller owns registration and sealing.

Stage transitions follow [stage-transitions](../../references/stage-transitions.md); a routine advance is one business operation and active preview is optional.

Cross-session and recovery follow [cross-session](../../references/cross-session.md). Read it only when that mode or recovery is active.

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
