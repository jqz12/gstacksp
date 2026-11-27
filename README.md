# GstackSP

面向 Codex 原生开发会话的 SP+GS 工作流。发布面为 **6 个默认入口和 4 个按需入口**；另有 **28 项内部来源职责**，落为 **26 个独立 `METHOD.md` 加 1 份共享的 workflow 业务恢复说明**，12 个旧包装完全退役。完整 Superpowers 正文、TDD/SDD、独立规格与质量审查、最终 all 回归、验证和交付门禁继续保留，但只有触发条件成立时才加载对应方法。

## 公开技能入口

默认流程：

| 技能 | 用途 |
|---|---|
| `gsp-workflow` | 选择阶段、恢复业务状态并加载当前适用方法 |
| `gsp-brainstorming` | 明确目标、范围、方案和验收 |
| `gsp-writing-plans` | 形成可执行、可验收且写集清晰的计划 |
| `gsp-execute` | 按 TaskContract 和 TDD 实施、运行真实检查 |
| `gsp-review` | 独立规格与质量审查、最终整体审查和 verify |
| `gsp-ship` | 提交、合并、资源收敛和交付准备 |

按需维护入口：

| 技能 | 用途 |
|---|---|
| `gsp-health` | 检查安装、文档结构、Git 路由和跨宿主事实 |
| `gsp-document-governance` | 新项目文档接入、老项目整理和持续治理 |
| `gsp-writing-skills` | 新增或调整可复用 GSP 技能 |
| `gsp-diagnosing-superpowers` | 对指定会话做只读重复工作、成本和触发诊断 |

## 安装

当前只服务 Codex。项目级安装把 6 个默认入口写入目标项目的 `.agents/skills`；用户级安装写入当前 Codex profile。任务规格、计划、待办、状态和证据始终保存在目标项目中。

```powershell
python E:\GstackSP\scripts\gsp.py --project E:\MyProject install --host codex
python E:\GstackSP\scripts\gsp.py --project E:\MyProject init
python E:\GstackSP\scripts\gsp.py --project E:\MyProject health --host codex
```

用户级安装：

```powershell
python E:\GstackSP\scripts\gsp.py install --scope user --host codex
```

4 个按需入口不进入默认安装集合。确有需要时显式点名，例如：

```powershell
python E:\GstackSP\scripts\gsp.py --project E:\MyProject install --host codex --optional writing-skills
```

安装器按 `skills/skill-manifest.json` 的精确分类处理升级和退役；用户修改或未知文件不会被静默覆盖。安装完成后在真实 Codex 会话调用 `$gsp-workflow`，再按需验证实际加载路径；脚本检查文件存在不等于宿主已经触发技能。

## 日常使用

告诉 Codex：“用 GstackSP 做这个需求，先讨论设计；确认后按推荐连续推进，准备交付。” 新任务在 `docs/work/<年份>/<月份>/<固定档案>/` 保存规格、计划和阶段材料。普通查询使用 `status --brief`，只读记录不启动 Git 或全仓扫描，也不证明当前可交付。

Codex 原生聊天、代理、历史、压缩/恢复、模型参数和 worktree 负责运行生命周期；GSP 保存业务编号、TaskContract、主体代际、共享资源租约、质量证据和验收状态。Goal 只在用户明确创建目标时使用。小任务不额外创建会话或 worktree；并行可写单元或独立验证确有必要时，才使用 Codex 原生 worktree 并登记资源归属。

`gsp-ship` 统一承接验收候选、提交、基于最新目标验证合并候选、按授权推送、处置本任务 worktree、分支和临时资源，最后关闭业务项。`drive-*` 仅作为用户明确要求脱离会话连续执行时的 legacy 兼容入口，不属于默认流程。

## 有效证据

每个任务先登记可观察的 TDD Red/Green，再运行项目声明的真实检查；最终候选必须在同一输入契约上完成 all 回归、独立规格与质量审查、整体审查和 verify。相同 argv、输入、环境、工具、阶段、契约及覆盖均可证明等价时复用已有结果；未知、截断、后续失败或输入漂移不能当作通过。质量门禁同时记录原始命令、日志、执行进程、Git/扫描/写入和实际等待时间，不用技能数量、代码行数或并行速度替代真实成本。

## 先看哪里

- [项目文档索引](docs/README.md)：公开使用说明和维护入口。
- [跨会话开发模式](docs/reference/cross-session-development.md)：业务关联、资源租约和恢复边界。
- [计划结构检查接口](docs/reference/plan-quality.md)：计划结构检查与验收边界。
- [构建上下文与独占缓存](docs/reference/build-context.md)：真实环境注入、兼容键、租约和恢复。
- [命令执行进度、日志与恢复](docs/reference/command-progress.md)：流式日志、心跳、`run-status` 和终态。
- `skills/skill-manifest.json`：唯一技能清单；`gsp/skill_catalog.py` 是只读 provider。

## 开发此项目

```powershell
python scripts/build_skills.py
python scripts/validate_skills.py
python -m unittest discover -s tests -v
python scripts/run_tests.py --jobs 4
python scripts/package.py
```

`scripts/build_skills.py` 只从 canonical manifest 生成 10 个公开投影、26 个物理 `METHOD.md`、方法索引和 catalog，并清退源中孤立的旧 `SKILL.md`。条件方法不会被拼进六个默认技能。`scripts/sp_preservation.py` 校验完整 SP 正文和附件未丢失；打包、安装和目录展示消费同一 provider。
