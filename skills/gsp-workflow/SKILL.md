---
name: gsp-workflow
description: Use when starting a development task in a project using GstackSP, or resuming its interrupted design, implementation or delivery work.
---

# GstackSP 工作流

阶段总结和交付使用固定“本轮技能执行顺序”四列表格，遵循 [交付与衔接](references/output-handoff.md)：展示实际成果、实际使用/仅参考的技能、证据及下一动作与执行者；已授权的当前工作继续推进，不等用户逐项提醒。

路由选择遵循 [统一路由契约](references/routing.md)；事件和报告语义遵循 [观测日志](references/observability.md)，没有宿主事件时保留 `unknown`。

条件内部方法只在实际触发条件成立时从 [方法索引](references/methods.md) 加载完整正文；常规阶段推进遵循 [阶段转换契约](references/stage-transitions.md)，跨会话与恢复遵循 [跨会话契约](references/cross-session.md)。

计划切片、执行 checkpoint 或候选审查时才读取 [收敛与检查点契约](references/convergence.md)；其他阶段不加载。该一处集中三道门、最短 Convergence JSON 和 review 证据格式，六入口只保留短引用，不增加模型调用或审批。

当前默认且唯一支持的集成宿主是 Codex。Codex 原生聊天、代理、历史、压缩/恢复、模型参数和 worktree 负责运行生命周期；GSP 保留业务编号、TaskContract、SP+GS/TDD/SDD、质量门禁、共享资源互斥、证据与验收。Goal 仅在用户明确创建目标时使用。`drive-*` 只作为用户明确要求脱离会话连续执行时的 legacy 兼容入口。

先读 [共同契约](references/contract.md) 和 [SP 适配契约](references/sp-adapter.md)，只加载当前阶段需要的技能。运行脚本位置：本技能的 `scripts/gsp.py`；源码包则为仓库根 `scripts/gsp.py`。下面以 `<gsp>` 表示 `python "<脚本绝对路径>" --project "<目标项目绝对路径>"`。

1. 看宿主真实工具列表；存在 `.gsp/active.json` 时默认运行 `<gsp> resume <id> --brief` 或 `status <id> --brief` 定位当前业务位置。该视图不验证可交付性；需要严格恢复时才调用旧 `resume <id>`，实际写入和验收仍各自强制门禁。发现分歧先调查，不盲续、不覆盖。
2. 新需求读 `gsp-brainstorming`。明确的小改可用短设计；已充分批准的方案继续执行，无需重新批准相同内容。建立任务用 `<gsp> init`、`<gsp> new <id> --title "标题"`。
3. 在首次写实现前落实工作位置。目录冻结已退役；旧任务中的 freeze 记录只作历史读取，不据此拒绝新操作或要求解除。小修改可沿用当前位置，并行写者、独立验证或保护现有改动时使用宿主原生隔离。
   Git 拓扑是首次实现前的必经交接：先运行 `<gsp> topology <id>`，向用户展示当前分支、HEAD、全部 worktree、脏状态和已有任务绑定，再用 `<gsp> topology-select <id> --mode in-place|branch|worktree --quote "用户原话"` 记录一次选择。默认阻止 main/master 或 detached HEAD 直接写入；只有明确原地覆盖才可继续。状态中的 GSP 编号、T 任务号、branch、commit SHA、Issue/PR、control_root 和 workspace_root 各自独立，不能互相替代。
   工作位置和构建缓存是两个独立决定：`size=large` 只提示风险，不单独强制 worktree；并行写者、独立验证或保护现有改动才优先隔离。即使使用 worktree，也不能让多个写者无租约地共写同一个可写 target。
4. 设计确认后依次使用 `gsp-writing-plans`、`gsp-execute`。计划评审、TDD、调试、浏览器和 QA 都是条件方法，只在方法索引中的实际触发条件成立时加载；独立审查与验收由 `gsp-review` 承接。
   阶段交接核对[实际产物与接口文档](references/artifact-fidelity.md)，读取完整阶段正文，不能以本入口摘要代替方法；计划自查后使用 `plan-check` 和 `seal-plan --check-structure`。计划涉及测试、构建、target/temp、worktree 或 CARGO_TARGET_DIR 时必须有“产物与临时文件生命周期”小节（产物位置、回收责任、保留与上限、验收方式），否则大型任务封计划失败。
5. 所有任务完成后先跑一次最终 `run --task all`，再由 `gsp-review` 完成整体审查和 verify 方法；`gsp-ship` 统一承接提交、合并、资源收敛和交付。同一输入不重复全量回归，只有输入、入口或集合改变才重跑并保留新回执。有精确外部操作授权则执行对应动作，不重复问。恢复方法只在真实恢复场景触发。
6. 任务进入 `blocked` 时不是终态：用 `<gsp> unblock <id> --reason "..."` 恢复阻塞前阶段继续执行，或用 `<gsp> close <id> --abandoned --reason "..."` 明确放弃并把档案归档为 cancelled；普通 `close` 仍只用于 ready 交付。绑定 worktree 已丢失时 `unblock` 仍要求先 `topology-select --rebind` 恢复，`close --abandoned` 可直接按恢复语义丢弃该工作区。交付后 `close --cleanup` 收敛本任务 worktree/分支/构建根；其他残留先 `topology-clean` 预览再 `--apply`，任务结束只保留主仓。

原生 `run` 持续排空 stdout/stderr，写入脱敏的 `stdout.txt`、`stderr.txt`、`command-progress.json` 和终态 `command-result.json`。长命令用 `<gsp> run-status <id> --evidence <id> [--wait 0..30]` 查看心跳、日志路径、截断和进程身份；它返回 `validation=NOT_CHECKED`，不是交付证明。相同空等待最多两次，第三次前必须查看日志、进程、退出码或产物。`status --brief` 同样只读账本，不运行 Git，也不声称当前可交付。

任务完成按有效证据集合判断，而不是最后一张回执：`tests` 与 `check` 按命令入口分组，适用失败必须由同一入口的后续成功解除；输入、契约、epoch、workspace 或入口变化会使旧成功失效。`observe-head` 在 HEAD 和验证输入均未变时是真正 no-op；真实内容、绑定或入口变化仍进入 `needs-verification`。

构建与临时产物由流程接管：`run` 把 TMP/TEMP/TMPDIR 指向本次运行的隔离目录并只回收本次新增的临时根条目；managed worktree 使用 `.gsp/build/<编号>/<slug>` 作为 CARGO_TARGET_DIR/BUILD_ROOT；回执记录 `hygiene`。未解释的 retained/truncated/errors 在 ready 报 `HYGIENE_LEFTOVER`，只有显式 decision 才能放行；残留即缺陷，登记根待办并回溯门禁，不把清理变成例行工作。详见 [共同契约](references/contract.md) 的“构建与临时产物”一节。

每个阶段按需读取项目根 `TODOS.md`，延期问题必须有归宿，必做问题不能靠延期解除阻塞。维护规则见 [待办生命周期](references/backlog.md)。

并行或全栈任务使用 `task-add --role frontend|backend|integration` 和 `topology-select --task Tnn`；每个可写任务一任务一 branch/worktree，控制根只保留一份账本。计划封存后新增 frontend/backend 必须用 `--owned-by <integration>` 声明归属；新增 integration 的 `--depends` 必须传递覆盖现有全部可写任务。前后端各自验证后由唯一 integration owner 在集成 worktree 汇合；缺集成任务、共享写目录或旧 base 时阻止进入 execute/ready。Issue/PR 只有真实对象才登记。

默认不要在 Codex 原生活动会话之外再启动后台驱动器。只有用户明确要求脱离会话连续执行时，才把 [持久驱动器](references/driver.md) 的 `drive-*` 入口当作 legacy 兼容路径；设计确认和工作位置确定后再配置检查命令，不能与当前会话并发写同一项目。

给用户看 `docs/gsp/README.md`、根待办和任务 `delivery.md`；运行状态是恢复记录，用户消息才是授权来源。技能不会自动创建代理或启动付费服务。
Codex 新任务的 `topology-select --mode worktree` 返回持久的原生创建请求，尚未创建树；按 `pending_action` 调用真实 Codex 工具，把原始回执交给 `native-workspace ... confirm` 后才进入实现。连续任务复用工作位置，只有独立并行写入才增加树。旧 GSP 管理的树用显式 `--legacy-worktree` 兼容，归属与清理责任沿用旧 owner；原生树必须经宿主归档，不能用 Git 删除冒充。

## 安装请求的范围选择

用户要求安装本工作流时，先说明两种方式：项目级（--scope project）复制到目标项目 .agents/skills/gsp-* 或 .claude/skills/gsp-*；用户全局级（--scope user）安装到当前用户的宿主 profile，多个项目共用，不新建目标项目技能目录。全局不代表系统所有账户，现有项目安装也不会自动删除。

当前只服务 Codex。用户未选择且没有既有明确选择时，询问一次项目级或用户级安装范围，并展示实际目标路径后执行；已有明确选择直接沿用，不重复问。任务规格、计划、待办和证据始终各自在目标项目中。日常开发不因没有项目技能目录而要求重复安装；先检查已存在的用户级安装。

## Codex 全局 hooks

已授权的 Codex 全局 hooks 使用 `<gsp> hooks-install` 独立安装；检查用 hooks-status，实验汇总用 hooks-report；维护用 hooks-upgrade/hooks-uninstall/hooks-recover，支持 --dry-run 与 --profile。不得改写宿主信任记录。只有 GSP 项目生效；direct-edit hook 只覆盖已接线的直接编辑，不把 Shell/MCP 副作用宣传为已阻止。详细能力和限制见 [hooks 契约](references/codex-hooks.md)。

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
