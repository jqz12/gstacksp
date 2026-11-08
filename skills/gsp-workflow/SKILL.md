---
name: gsp-workflow
description: Use when starting a development task in a project using GstackSP, or resuming its interrupted design, implementation or delivery work.
---

# GstackSP 工作流

先读 [共同契约](references/contract.md)，只加载当前阶段需要的技能。运行脚本位置：本技能的 `scripts/gsp.py`；源码包则为仓库根 `scripts/gsp.py`。下面以 `<gsp>` 表示 `python "<脚本绝对路径>" --project "<目标项目绝对路径>"`。

1. 看宿主真实工具列表；存在 `.gsp/active.json` 时运行 `<gsp> resume <id>`，按输出核对项目、设计、计划和决策。发现分歧先调查，不盲续、不覆盖。
2. 新需求读 `gsp-brainstorming`。明确的小改可用短设计；已充分批准的方案继续执行，无需重新批准相同内容。建立任务用 `<gsp> init`、`<gsp> new <id> --title "标题"`。
3. 在首次写实现前落实工作位置和冻结选择。未指定冻结时问一次并给推荐；明确的小修改可原地冻结。无人值守须有用户实际授权，按推荐记录，不假造用户答复。
4. 设计确认后依次使用 `gsp-writing-plans`、`gsp-execute`；计划评审 `gsp-autoplan` 按复杂度使用。测试、调试、审查在执行中调用。UI 按需调用 `gsp-browse`、`gsp-qa` 及已安装视觉扩展。
5. 所有任务完成后只做一次整体 `gsp-review`，由 `gsp-verify` 确认证据，`gsp-ship` 准备交付。有精确外部操作授权则执行对应动作，不重复问。保存恢复用 `gsp-context-save`。

每个阶段按需读取项目根 `TODOS.md`，延期问题必须有归宿，必做问题不能靠延期解除阻塞。维护规则见 [待办生命周期](references/backlog.md)。

用户明确要求后台无人值守时读 [持久驱动器](references/driver.md)：当前提供 Codex headless 适配器，设计确认和冻结选择后配置检查命令并启动；普通 unattended 标志本身不会创建后台进程。

给用户看 `docs/gsp/README.md`、根待办和任务 `delivery.md`；运行状态是恢复记录，用户消息才是授权来源。技能不会自动创建代理或启动付费服务。
