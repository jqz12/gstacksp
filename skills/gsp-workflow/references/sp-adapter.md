# SP 原版保留与 GstackSP 适配契约

> 当前文档治理布局（0.8.4）：新任务默认固定档案 docs/work/<年份>/<月份>/<编号-初始标题>/spec.md、plan.md，其他文档按阶段产生；旧布局继续绑定兼容。docs/superpowers 与 docs/gsp/tasks 仅为旧布局兼容，实际以 status/resume 返回路径为准。documents-adopt 现允许项目 docs 内已有 Markdown。完整新规则见 [文档治理](document-governance.md)。

这是差异说明，不是原版摘要。固定基线为本地 Superpowers 7.0.2 文件快照，82 个文件逐个 SHA-256；未验证父本独立 Git 提交。完整原文件在 ../upstream/。主技能正文保留全部原文，只定向正文 Markdown 资源链接，不删除步骤、示例、反例、检查清单。英文原文避免翻译漂移。

## 原版名称映射

| 原版 | 入口或完整资源 |
|---|---|
| using-superpowers | gsp-workflow |
| brainstorming | gsp-brainstorming |
| writing-plans | gsp-writing-plans |
| executing-plans | gsp-execute 主文 |
| subagent-driven-development | upstream/skills/subagent-driven-development/ORIGINAL.md |
| dispatching-parallel-agents | upstream/skills/dispatching-parallel-agents/ORIGINAL.md |
| using-git-worktrees | upstream/skills/using-git-worktrees/ORIGINAL.md |
| test-driven-development | gsp-test-driven-development |
| systematic-debugging | gsp-systematic-debugging |
| requesting-code-review | gsp-review 主文及原版 code-reviewer.md |
| receiving-code-review | upstream/skills/receiving-code-review/ORIGINAL.md |
| verification-before-completion | gsp-verify |
| finishing-a-development-branch | gsp-ship |
| writing-skills | gsp-writing-skills |
| diagnosing-superpowers | gsp-diagnosing-superpowers，仅用户指定会话诊断时使用 |

upstream 路径相对 gsp-workflow。进入子代理执行、并行调查、隔离或反馈处理模式前必须读对应全文及其引用。原 SKILL.md 仅改资源文件名为 ORIGINAL.md，字节未变，防止被发现为另一套技能。原版文字中相对 prompts/scripts/references 位置始终以对应 ORIGINAL.md 所在目录为准。Skill 工具名称按上表映射，不启动已安装的另一套 SP。

## 规格和计划治理

新任务默认 docs/work/YYYY/MM/<编号-初始标题>/spec.md 与 plan.md；创建年月固定、两位月份，跨月不移动。旧年目录和旧 specs/plans 位置仅兼容已有绑定，不重新生成一份。实际以status/resume返回路径为准。spec是设计唯一正本，plan明确引用它。

审查、决策、交付在同一任务档案，机器状态在.gsp/runs/<运行ID>/。旧状态不静默搬迁。显式documents-adopt保留旧关联历史、撤销批准与证据，需核实后按真实授权重新确认；不得绕过在场驱动和未知执行保护。

## 执行账本与单一状态正本

SP 的 progress.md 是每个计划、每个工作树的 scratch ledger，不是项目根进度文件。在 GSP 中身份、任务状态、完成证据与历史由 .gsp/runs/<id>/state.json、events.jsonl、evidence 保存；任务/计划身份和 Git 分支/HEAD 都必须核验。不得让另一份 progress.md 与它竞争完成状态。

原版 task-start/task-done/sdd-workspace 等账本脚本保留但不直接运行；协调者通过 GSP task-start → 实施与真实 run → task-done 登记。修复返工用 task-reopen。修复轮次和裁决仍需记录：后台驱动持久计数，会话内须在 decisions.md 逐轮写发现、证据、修复、裁决和影响，不可声称 CLI 已强制所有会话轮次。brief/review package 保留原版要求的规格、任务全文、全局约束、实际 diff、测试、修复历史与裁决；仅替换 ledger 位置，不删语义。

不能为了清理 SP 临时 workspace 删除 .gsp、specs/plans 或 docs/gsp；裁决和证据先持久保存，只有确认为本任务临时文件才清理。Windows 先核验绝对路径。不会把循环结束、问题被搁置当作已验证或可交付。

## 宿主和 Windows 的必要适配

原版工具映射仅是参考，实际宿主工具契约优先。不得因模型/effort 建议改全局配置；覆盖仅在宿主允许、用户授权或明确角色能力需要时使用。当前 full-history fork 不允许覆盖模型；需独立上下文时使用最小 task brief。等待按宿主限制并及时通知，不机械阻塞 5–10 分钟。不可用代理按原版允许的自查降级，如实标明，不假称独立。

原版部分脚本已有 Git Bash/cygpath 适配，并非全都不可用；但完整保留不代表已做 Windows 实机认证。依赖 Bash/Node/Graphviz 的辅助工具启用前检测，按显式解释器调用，包括无扩展名脚本的内部依赖。Windows 子进程隐藏窗口。视觉 companion 需接受及实际工具；能力缺失如实降级，不假造原型或浏览器验证。审查临时目录用宿主临时位置，保留不改工作树/index/HEAD 的约束。

## 授权、冻结与阶段衔接

用户当前要求优先。首次实现冻结问一次，已有指示不重问。worktree 按需；选用时保持原版已有隔离探测、忽略规则、子模块处理、基线和归属清理。按项目实际依赖配置安装，不机械认为 pyproject.toml 就必须用 Poetry。冻结与 worktree 均不等于沙箱。

默认先讨论确认设计。对同一已确认内容不重复批准，改变设计仍需核对范围。只有真实无人值守授权才按有依据的推荐连续处理普通技术选择；不能削弱设计质量、测试、审查和失败上限。默认到交付准备。原版自动提交示例、固定收尾菜单、英语 discard 口令不能替代或扩大真实授权；已有精确推送/合并/删除授权不重问，付费、部署、消息等按实际范围。未提交或不归属本任务的文件不擅自删除。

## GS / CLI 接入点（不替代原版方法）

实际产物的阶段检查见 [产物一致性](artifact-fidelity.md)。原版完整自查结果在本任务 plan.md 的 Self-Review 留痕；这是 GSP 的记录落点。计划自查后运行 plan-check，并以 seal-plan --check-structure 封存。结构合格不代替内容判断，也不回写历史任务伪造遵循。

- brainstorming：读根待办；保留完整讨论、设计、builder check；确认后 approve。
- writing-plans：保留完整任务粒度、全局约束、Review Focus、spec coverage；根待办按需关联，完整 task-add 后 seal-plan。按风险使用 autoplan/UI/devex，不叠加 GS 四阶段。
- execute：resume 核对，不重复已完成任务；CLI 登记和证据；后台显式读取 driver.md 并 drive-init/start，不能和当前会话并发写。
- review：请求和接收反馈两份原文均需使用；审查 SHA 范围覆盖整个任务而非机械 HEAD~1。独立审查保留完整模板，自查注明限制；review.md 保存范围、结论、发现和未覆盖，再 CLI review 登记。审查者先核对已有证据，仅具体疑点补跑，不无意义重复全套。
- verify/ship：核对根待办、真实证据、最终 all 回归、当前整体审查后 ready。原版 evidence-before-claims 保留；复用证据必须证明输入及契约仍匹配，不能旧日志冒充当前验证。外部动作完成后补交付，不预先称成功。
- learn/context：经验有来源追加，本地保存；save/resume 管恢复。不默认向 gbrain 外发。

## 维护要求

sources/superpowers 为只读父本基线，修改须独立审核；不能为过检查改哈希。build_skills 重建，validate_skills 验证全部资源和正文完整性。原版写作词数建议不构成压缩父本的理由。完整性和路由测试不等于真实模型行为验收；子代理压力测试与纯参考检索应用测试区分记录，不能拿静态通过代替二者。

显式关联迁移会将已完成任务标为 needs-verification，旧完成证据留在 history；重新批准和封存后按依赖 task-start → 重新验证 → task-done，不要求重写正确实现。最后重新 all 回归、整体审查和 ready。
