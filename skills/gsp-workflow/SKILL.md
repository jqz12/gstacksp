---
name: gsp-workflow
description: Use when starting a development task in a project using GstackSP, or resuming its interrupted design, implementation or delivery work.
---

# GstackSP 工作流

阶段总结和交付使用固定“本轮技能执行顺序”四列表格，遵循 [交付与衔接](references/output-handoff.md)：展示实际成果、实际使用/仅参考的技能、证据及下一动作与执行者；已授权的当前工作继续推进，不等用户逐项提醒。

路由选择遵循 [统一路由契约](references/routing.md)；事件和报告语义遵循 [观测日志](references/observability.md)，没有宿主事件时保留 `unknown`。

先读 [共同契约](references/contract.md) 和 [SP 适配契约](references/sp-adapter.md)，只加载当前阶段需要的技能。运行脚本位置：本技能的 `scripts/gsp.py`；源码包则为仓库根 `scripts/gsp.py`。下面以 `<gsp>` 表示 `python "<脚本绝对路径>" --project "<目标项目绝对路径>"`。

1. 看宿主真实工具列表；存在 `.gsp/active.json` 时运行 `<gsp> resume <id>`，按输出核对项目、规格、计划和决策。发现分歧先调查，不盲续、不覆盖。
2. 新需求读 `gsp-brainstorming`。明确的小改可用短设计；已充分批准的方案继续执行，无需重新批准相同内容。建立任务用 `<gsp> init`、`<gsp> new <id> --title "标题"`。
3. 在首次写实现前落实工作位置和冻结选择。未指定冻结时问一次并给推荐；明确的小修改可原地冻结。无人值守须有用户实际授权，按推荐记录，不假造用户答复。
   Git 拓扑是首次实现前的必经交接：先运行 `<gsp> topology <id>`，向用户展示当前分支、HEAD、全部 worktree、脏状态和已有任务绑定，再用 `<gsp> topology-select <id> --mode in-place|branch|worktree --quote "用户原话"` 记录一次选择。默认阻止 main/master 或 detached HEAD 直接写入；只有明确原地覆盖才可继续。状态中的 GSP 编号、T 任务号、branch、commit SHA、Issue/PR、control_root 和 workspace_root 各自独立，不能互相替代。
4. 设计确认后依次使用 `gsp-writing-plans`、`gsp-execute`；计划评审 `gsp-autoplan` 按复杂度使用。测试、调试、审查在执行中调用。UI 按需调用 `gsp-browse`、`gsp-qa` 及已安装视觉扩展。
   阶段交接核对[实际产物与接口文档](references/artifact-fidelity.md)，读取完整阶段正文，不能以本入口摘要代替方法；计划自查后使用 `plan-check` 和 `seal-plan --check-structure`。
5. 所有任务完成后先跑一次最终 `run --task all`，再做一次整体 `gsp-review`，由 `gsp-verify` 确认证据，`gsp-ship` 准备交付。同一输入不重复全量回归，只有测试入口或集合改变、或有回执记录的抖动才重跑并保留每次回执。有精确外部操作授权则执行对应动作，不重复问。保存恢复用 `gsp-context-save`。

每个阶段按需读取项目根 `TODOS.md`，延期问题必须有归宿，必做问题不能靠延期解除阻塞。维护规则见 [待办生命周期](references/backlog.md)。

并行或全栈任务使用 `task-add --role frontend|backend|integration` 和 `topology-select --task Tnn`；每个可写任务一任务一 branch/worktree，控制根只保留一份账本。前后端各自验证后由唯一 integration owner 在集成 worktree 汇合；缺集成任务、共享写目录或旧 base 时阻止 ready。Issue/PR 只有真实对象才登记。

用户明确要求后台无人值守时读 [持久驱动器](references/driver.md)：当前提供 Codex headless 适配器，设计确认和冻结选择后配置检查命令并启动；普通 unattended 标志本身不会创建后台进程。

给用户看 `docs/gsp/README.md`、根待办和任务 `delivery.md`；运行状态是恢复记录，用户消息才是授权来源。技能不会自动创建代理或启动付费服务。
## 安装请求的范围选择

用户要求安装本工作流时，先说明两种方式：项目级（--scope project）复制到目标项目 .agents/skills/gsp-* 或 .claude/skills/gsp-*；用户全局级（--scope user）安装到当前用户的宿主 profile，多个项目共用，不新建目标项目技能目录。全局不代表系统所有账户，现有项目安装也不会自动删除。

用户未选择且没有既有明确选择时，询问一次安装范围，并展示实际目标路径后执行；已有明确选择直接沿用，不重复问。Codex 与 Claude 分别选择宿主，全局安装分别执行，不因一个宿主的授权改另一个宿主。任务规格、计划、待办和证据始终各自在目标项目中。日常开发不因没有项目技能目录而要求重复安装；先检查已存在的用户级安装。

## Codex 全局 hooks

已授权的 Codex 全局 hooks 使用 `<gsp> hooks-install` 独立安装；检查用 hooks-status，实验汇总用 hooks-report；维护用 hooks-upgrade/hooks-uninstall/hooks-recover，支持 --dry-run 与 --profile。不得改写宿主信任记录。只有 GSP 项目生效；冻结仅检查支持的直接编辑，不把 Shell/MCP 副作用宣传为已阻止。详细能力和限制见 [hooks 契约](references/codex-hooks.md)。

## 文档治理

新项目文档入口、老项目完整接入或集中整理使用按需技能 gsp-document-governance；health 只检查并交接，已有整改授权则继续处理。执行见 [治理执行规则](references/document-governance-execution.md)。完整治理必须对照用户原始目标验收，不能在 spec 中静默缩小为盘点或预览。日常只维护受本次改动影响的资料，不重复启动全库治理。

文档位置与生命周期见 [治理契约](references/document-governance.md)。明确保存想法用 work-new；开始任务前 docs-search 检索相关模块与历史，已有想法用 new --work 接续。新任务按阶段生成 docs/work 下档案，不在原版默认目录重复创建。文档检查调用 gsp-health 的 --documents-only；旧项目统一布局先盘点、再预览迁移，不长期双写。
<!-- sp-preserved-body:begin -->
<SUBAGENT-STOP>
If you were dispatched as a subagent to execute a specific task, ignore this skill.
</SUBAGENT-STOP>

<EXTREMELY-IMPORTANT>
If you think there is even a 1% chance a skill might apply to what you are doing, you ABSOLUTELY MUST invoke the skill.

IF A SKILL APPLIES TO YOUR TASK, YOU DO NOT HAVE A CHOICE. YOU MUST USE IT.

This is not negotiable. You cannot rationalize your way out of this.
</EXTREMELY-IMPORTANT>

## The Rule

**Invoke relevant or requested skills BEFORE any response or action** — including clarifying questions, exploring the codebase, or checking files. If it turns out wrong for the situation, you don't have to use it.

**Before entering plan mode:** if you haven't already brainstormed, invoke the brainstorming skill first.

Then announce "Using [skill] to [purpose]" and follow the skill exactly. If it has a checklist, create a todo per item.

## Skill Priority

When multiple skills apply, process skills come first — they set the approach, then implementation skills (frontend-design, etc.) carry it out. Brainstorming and systematic-debugging are Superpowers' most common process skills, but the rule holds for any of them.

- "Let's build X" → superpowers:brainstorming first, then implementation skills.
- "Fix this bug" → superpowers:systematic-debugging first, then domain skills.

## Red Flags

These thoughts mean STOP—you're rationalizing:

| Thought | Reality |
|---------|---------|
| "This is just a simple question" | Questions are tasks. Check for skills. |
| "I need more context first" | Skill check comes BEFORE clarifying questions. |
| "Let me explore the codebase first" | Skills tell you HOW to explore. Check first. |
| "I can check git/files quickly" | Files lack conversation context. Check for skills. |
| "Let me gather information first" | Skills tell you HOW to gather information. |
| "This doesn't need a formal skill" | If a skill exists, use it. |
| "I remember this skill" | Skills evolve. Read current version. |
| "This doesn't count as a task" | Action = task. Check for skills. |
| "The skill is overkill" | Simple things become complex. Use it. |
| "I'll just do this one thing first" | Check BEFORE doing anything. |
| "This feels productive" | Undisciplined action wastes time. Skills prevent this. |
| "I know what that means" | Knowing the concept ≠ using the skill. Invoke it. |

## Platform Adaptation

If your harness appears here, read its reference file for special instructions:

- Claude Code: `upstream/skills/using-superpowers/references/claude-code-tools.md`
- Codex: `upstream/skills/using-superpowers/references/codex-tools.md`
- Pi: `upstream/skills/using-superpowers/references/pi-tools.md`
- Antigravity: `upstream/skills/using-superpowers/references/antigravity-tools.md`
- Hermes Agent: `upstream/skills/using-superpowers/references/hermes-tools.md`
- Muse: `upstream/skills/using-superpowers/references/muse-tools.md`

## User Instructions

User instructions (CLAUDE.md, AGENTS.md, GEMINI.md, etc, direct requests) take precedence over skills, which in turn override default behavior. Only skip skill workflows or instructions when your human partner has explicitly told you to.

<!-- sp-preserved-body:end -->
