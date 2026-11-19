---
name: gsp-ship
description: "Use when 实现和审查结束，需要准备交付或执行已授权的发布分支操作时."
---

## GstackSP adaptation (only the explicitly listed differences)

Read the [SP adaptation contract](../gsp-workflow/references/sp-adapter.md) first. The complete upstream body follows, not a summary. Resolve upstream `superpowers:*` names using the contract; never load a second SP installation. Relative prompts/scripts are based on the corresponding ORIGINAL.md directory. Resource links below are redirected to the complete preserved files. Missing host capabilities must be reported honestly.

Artifact layout is adapted, not the method: use the paths returned by GSP status/resume. New tasks live in docs/work/<year>/<month>/<stable-folder>/ (spec.md, plan.md and stage artifacts); existing bindings stay authoritative. Read [document governance](../gsp-workflow/references/document-governance.md) for saved ideas, module relations, discovery, health reports and controlled migration. Do not create a second spec or plan at the upstream default path.

Use the [output and handoff contract](../gsp-workflow/references/output-handoff.md) for actual results, honestly attributed skills used, evidence, next action and owner. Return nested methods to the current task; continue authorized work without a redundant approval.

### GSP stage integration

按共同待办规则核对新增、延期、已完成与未完成项；只凭有效任务证据关闭，不凭标题/提交信息猜完成。保留原内容和历史；交付必须链接根待办并披露未完成项。待办写入失败明确报告，绝不声称已保存；阻塞问题仍阻塞。交付前重新运行 `<gsp> topology <id>`，核对 GSP 编号、T 任务、当前 branch、commit SHA、control_root、workspace_root 和实际 Issue/PR；没有真实 Issue/PR 不登记编号。若拓扑尚未选择，先用 `<gsp> topology-select <id> --mode in-place|branch|worktree --quote "用户原话"` 补齐记录；默认不允许在 main/master 或 detached HEAD 上直接实现。并行前后端必须由唯一 integration owner 在集成 worktree 验证后才能 ready；未授权的 commit、push、merge 或删除分支保持未执行。

## 跨会话开发（仅 `intent=cross-session`）

单会话仍是默认路径。没有 `cross-session` 意图的任务不创建 session registry、brief/report 目录，也不增加额外门禁。启用跨会话后，一个 GSP 工作项仍只有一份 `spec.md`、`plan.md`、`decisions.md`、`review.md` 和 `delivery.md`，这就是唯一总体计划及其配套记录。总体详细执行计划由 controller 主会话维护，它负责目标、非目标、任务图、依赖、公共接口、基线、worktree、允许和禁止路径、测试矩阵以及最终集成验收。子会话读取总体计划和自己的 brief，自主安排局部阅读、实现和局部测试顺序，不能复制 `plan-v2.md`，也不能替代 controller 做跨任务裁决。

使用 `<gsp> session-register` 分配稳定的 `S01`、`S02` 会话身份。`T01`、`T02` 仍是计划任务身份。controller 用 `<gsp> session-brief` 派发局部输入，worker 用 `<gsp> session-report` 回传 `DONE`、`BLOCKED` 或 `CHANGE_REQUEST`。换主会话只用 `<gsp> session-handoff` 接管同一 registry 和总体计划。报告替换前会把旧版本移入任务 `sessions/history/`，不会覆盖证据。

主会话发布时使用 `<gsp> session-dispatch`。它按依赖图批量计算 ready 任务，检查并发上限、`claimed_paths` 和路径重叠，并生成 `Dnnn`、预分配的 `Snn`、`Tnn-brief.md` 和 `Tnn-prompt.md`。没有显式 worktree 时，正式 worker 默认使用 `--worktree-mode auto`，GSP 创建受控的 managed worktree 和对应分支，并把实际路径、分支和基线写入 dispatch。显式 `--worktree` 或 `--worktree-mode bind` 只绑定宿主已有目录，GSP 取得写租约但不取得删除权。Codex 目标返回 `adapter-ready`，controller 再调用宿主原生线程创建、worktree 绑定、消息发送和有界等待入口。没有连接器的平台进入 `manual-required`，用户转发同一份 prompt。CLI 不伪造线程创建或外部消息发送。

controller 在发布前和进入等待时使用 `<gsp> session-progress`。摘要必须展示总体进度、发布次数、独立任务数、DONE 报告数、accepted 数、已清理 worktree 数、managed worktree 数、bound worktree 数、cleanup-pending 数、运行中、阻塞、CHANGE_REQUEST、待人工转发和下一阶段任务表。总体进度只按 `accepted_tasks / planned_tasks` 计算，不能把报告、接受或 prompt 生成冒充清理完成。

ZCode 作为用户级宿主时使用 `--host zcode` 安装到 `~/.zcode/skills` 和 `~/.zcode/AGENTS.md`，所有 ZCode 工作区共用同一份 GSP。`session-dispatch --target Tnn=zcode` 当前进入 `manual-required`，主会话把生成的 prompt 复制到 ZCode 子会话；子会话仍必须在指定 worktree 中工作并运行固定的 `session-report`，主会话再用 `session-watch` 和 `session-accept` 收口。这个安装范围不要求每个项目创建 `.agents/skills`。

`session-report` 会把结构化报告追加到 durable outbox。主会话或有界 watcher 使用 `<gsp> session-watch` 按事件 ID 消费，重复消费保持幂等。Codex 宿主用有界 `wait_threads` 等待，不要求 controller 进程全天轮询。主会话关闭时事件留在 outbox，下一次 resume 继续消费。controller 检查报告、测试和集成结果后，使用 `<gsp> session-accept <run> <task> --session <Snn>` 明确接受任务。`session-accept` 会记录接受事件、释放 worker lease，并按 worktree 所有权自动处理清理。干净的 managed worktree 会被移除并执行 prune，bound worktree 只释放租约并保留目录。

接受和清理是两个独立状态。managed worktree 遇到脏文件、锁定、Git 非零退出或未知结果时保留目录和分支，状态为 `cleanup-pending`，不计入 `cleaned_worktrees`。主会话随后使用 `<gsp> session-clean --retry` 重试 cleanup-pending。重复接受、释放或重试必须幂等。正常路径不要求用户手动删除 GSP 创建的 worktree，也不删除分支、commit、报告或显式绑定目录。

跨会话写入需要三层门禁。controller lease 保证同一任务只有一个主会话，worktree write lease 保证同一源码目录同一时刻只有一个可写会话，`--path` 声明和已接线的 direct-edit hook 在 Edit、Write、apply_patch 前复核会话身份、写租约和路径归属。未注册会话、未声明路径、租约冲突和重叠声明直接拒绝，分别返回 `SESSION_WRITE_LEASE_REQUIRED`、`SESSION_PATH_NOT_CLAIMED` 或 `SESSION_PATH_CONFLICT`。该机制不是文件系统沙箱。Shell、外部编辑器、MCP 或未接线宿主只能记录为 `NOT_CHECKED`，不能把事后 Git 指纹检查说成编辑前保护。

子 agent 与正式子会话必须区分。子 agent 是当前主会话内的临时辅助，不拥有独立 `Snn`、worktree、report 或恢复边界。正式子会话才计入 dispatch 统计，并拥有 `Snn`、`Tnn`、brief、report、outbox 和 worktree 清理生命周期。普通单会话任务不创建这些资料，原有 `new`、`approve`、`seal-plan`、`task-start`、`run`、`review`、`ready` 和恢复语义保持兼容。

Previously confirmed content and explicit execution/integration authorization take precedence over the upstream repeated plan confirmation or fixed completion menu. A substantive design change or a new external action still needs the applicable authorization.

Before claiming an independent development/remediation task complete, actually check its current status/resume and finish the final all regression, current review, verification and ready. Do not substitute a raw saved ready flag, health report or a manual addition to an old delivery.md. Nested methods return to the parent task; read-only reports do not require an artificial development task.

Final task report: follow the gsp-ship final-summary section of the [output and handoff contract](../gsp-workflow/references/output-handoff.md). Use the exact heading 本轮技能执行顺序 and the fixed four-column table in that contract for both stage and final reports. Include an ordered table of skills actually used, their actions and results, including repair/review/reverification loops; distinguish reference-only material. Show the ordered summary in the final reply and retain details in the existing delivery.md.

For document onboarding or governance, compare the original user objective with the spec, plan and delivered artifacts; a narrower spec does not silently supersede that objective. Use the [governance execution rules](../gsp-workflow/references/document-governance-execution.md). Inventory or migration preview is not completed migration or acceptance. Required unfinished work remains blocking unless the user actually changes the scope.

Before handing this stage onward, apply the [artifact fidelity checks](../gsp-workflow/references/artifact-fidelity.md) to the actual bound artifacts. Read the complete upstream method below; this integration is not its replacement. Plan authors use plan-check then seal-plan --check-structure after the upstream self-review. Structural success, preserved source text and a skills-used table do not prove semantic compliance.

Use the same artifact contract to route applicable engineering responsibilities from design to tasks, evidence and current documentation: data/recovery, configuration/dependencies, integration, project materials, delivery and maintenance. Expand only the topics triggered by this change; no extra mandatory table, document set or skill sequence. Headless plan workers return self-reviewed content; the controller owns registration and sealing.

Stage transitions use the unified controller: preview with `<gsp> handoff <id> --to <stage>` and advance with `<gsp> advance <id> --to <stage>`. Read `next_skill` and pending gates; do not treat chat text as a handoff or mutate workflow state around a failed advance.

<!-- sp-preserved-body:begin -->
# Finishing a Development Branch

## Overview

**Core principle:** Verify tests → Detect environment → Present options → Execute choice → Clean up.

**Announce at start:** "I'm using the finishing-a-development-branch skill to complete this work."

## Step 1: Verify Tests

Run the project's full test suite (`npm test` / `cargo test` / `pytest` / `go test ./...`).

**If tests fail**, report the failures and stop — the menu comes after a green suite:

```
Tests failing (<N> failures). Must fix before completing:

[Show failures]
```

**If tests pass:** continue to Step 2.

## Step 2: Detect Environment

```bash
GIT_DIR=$(cd "$(git rev-parse --git-dir)" 2>/dev/null && pwd -P)
GIT_COMMON=$(cd "$(git rev-parse --git-common-dir)" 2>/dev/null && pwd -P)
# Capture now, while still inside the workspace — Step 5 changes directory
# before cleanup (Step 6) needs this value
WORKTREE_PATH=$(git rev-parse --show-toplevel)
```

This determines which menu to show and how cleanup works:

| State | Menu | Cleanup |
|-------|------|---------|
| `GIT_DIR == GIT_COMMON` (normal repo) | Standard 3 options | No worktree to clean up |
| `GIT_DIR != GIT_COMMON`, named branch | Standard 3 options | Provenance-based (see Step 6) |
| `GIT_DIR != GIT_COMMON`, detached HEAD | Reduced 2 options (no merge) | Externally managed — leave in place |

## Step 3: Determine Base Branch

The base branch is whatever this work forked from — usually named in the
plan, the conversation, or the branch's upstream. If it is not already
known, ask: "This branch split from <your best guess> - is that correct?"
Confirm before merging: merging into the wrong base is expensive to undo.

## Step 4: Present Options

**Normal repo and named-branch worktree — present exactly these 3 options:**

```
Implementation complete. What would you like to do?

1. Merge back to <base-branch> locally
2. Push and create a Pull Request
3. Keep the branch as-is (I'll handle it later)

Which option?
```

**Detached HEAD — present exactly these 2 options:**

```
Implementation complete. You're on a detached HEAD (externally managed workspace).

1. Push as new branch and create a Pull Request
2. Keep as-is (I'll handle it later)

Which option?
```

Present the menu exactly as written — concise, with every option coming
from the list above. Discarding the work happens only in response to your
human partner explicitly asking for it (see "If your human partner asks to
discard the work" below). Wait for their answer; the integration decision
is theirs.

## Step 5: Execute Choice

### Option 1: Merge Locally

```bash
# Get main repo root for CWD safety
MAIN_ROOT=$(git -C "$(git rev-parse --git-common-dir)/.." rev-parse --show-toplevel)
cd "$MAIN_ROOT"

# Merge first — verify success before removing anything
git checkout <base-branch>
git pull
git merge <feature-branch>

# Verify tests on merged result
<test command>
```

If tests fail on the merged result: stop, leave the worktree and branch in
place, and investigate — nothing has been pushed, so the merge is local
and recoverable.

Once the merged result is green: clean up the worktree (Step 6), then
delete the branch:

```bash
git branch -d <feature-branch>
```

### Option 2: Push and Create PR

```bash
git push -u origin <feature-branch>
# From a detached HEAD, name the new branch on the remote:
# git push origin HEAD:refs/heads/<new-branch>
```

Then create the pull/merge request against <base-branch> with the forge's
tooling — its CLI if one is available, or the creation URL most forges
print when you push — following the repo's PR template and conventions if
present, and report the URL to your human partner.

Keep the worktree — your human partner iterates on PR feedback there.

### Option 3: Keep As-Is

Report: "Keeping branch <name>. Worktree preserved at <path>."

### If your human partner asks to discard the work

This path exists only as a response to an explicit request to throw the
work away. Confirm first:

```
This will permanently delete:
- Branch <name>
- All commits: <commit-list>
- Worktree at <path>

Type 'discard' to confirm.
```

Wait for that exact confirmation. When it arrives:

```bash
MAIN_ROOT=$(git -C "$(git rev-parse --git-common-dir)/.." rev-parse --show-toplevel)
cd "$MAIN_ROOT"
```

Then clean up the worktree (Step 6) and force-delete the branch:

```bash
git branch -D <feature-branch>
```

## Step 6: Cleanup Workspace

**Runs for Option 1 and confirmed discards.** Options 2 and 3 always
preserve the worktree. Both callers have already changed directory to the
main repo root — worktree removal must run from outside the worktree —
and use the `GIT_DIR`/`GIT_COMMON`/`WORKTREE_PATH` values captured in
Step 2, from before that directory change.

**If `GIT_DIR == GIT_COMMON`:** Normal repo, no worktree to clean up. Done.

**If `WORKTREE_PATH` is under `.worktrees/` or `worktrees/`:** Superpowers
created this worktree — we own cleanup:

```bash
git worktree remove "$WORKTREE_PATH"
git worktree prune  # Self-healing: clean up any stale registrations
```

**If removal is refused** (`contains modified or untracked files`): the
worktree holds files that exist nowhere else — uncommitted plans, notes,
or scratch work. Never `--force` on your own initiative. Show your human
partner what is at stake and ask:

```bash
git -C "$WORKTREE_PATH" status --porcelain -uall
```

```
Worktree removal refused — these files were never committed:

<file list>

1. Commit them to <branch> before cleanup
2. Move them into <main repo root>
3. Delete them (unrecoverable)

Which?
```

Carry out the choice, then remove the worktree.

**Otherwise:** The host environment owns this workspace — leave it in
place. If your platform provides a workspace-exit tool, use it.

## Quick Reference

| Option | Merge | Push | Keep Worktree | Cleanup Branch |
|--------|-------|------|---------------|----------------|
| 1. Merge locally | yes | - | - | yes |
| 2. Create PR | - | yes | yes | - |
| 3. Keep as-is | - | - | yes | - |
| Discard (explicit request only) | - | - | - | yes (force) |

## Common Rationalizations

| Excuse | Reality |
|--------|---------|
| "Tests passed earlier this session" | Run the suite on the tree you are about to integrate. A green run only proves the tree it ran on. |
| "They obviously want it merged" | Integration is your human partner's decision. Present the menu and wait. |
| "They seem done with this feature — I'll offer to discard it" | The menu is complete as written. Discard happens only when your human partner asks for it in so many words. |
| "'Yeah, get rid of it' counts as confirmation" | Only the typed word `discard` authorizes deletion. |
| "The PR is up, so the worktree is clutter now" | PR feedback gets fixed in that worktree. It stays until the work lands. |
| "This other worktree looks stale — I'll clean it too" | Clean up only worktrees under `.worktrees/` or `worktrees/`. Everything else belongs to the host. |
| "Removal refused — `--force` is just finishing the cleanup" | The refusal means files exist only in that worktree. `--force` destroys them permanently. Show your human partner and ask. |
| "The merged-result failure is probably flaky" | A failing merged result stops everything. Branch and worktree stay put while you investigate. |
| "The base branch is obviously main" | Confirm the fork point or ask. Merging into the wrong base is expensive to undo. |
| "The push was rejected — force-push will fix it" | A rejected push means the remote moved. Investigate; force-push only on your human partner's explicit request. |

<!-- sp-preserved-body:end -->
