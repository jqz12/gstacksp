# GstackSP

适合个人开发者的 Windows 开发工作流，供 Claude Code 和 Codex 使用。

保留 Superpowers 完整技能正文与配套资源，以其设计、计划、测试和审查为主干，加入按授权连续执行、冻结、经验、恢复、项目代码质量检测与交付。**22 个核心技能、28 个按需扩展**；没有必要每个任务都加载全部技能。代码质量检测只运行项目声明的检查，按 change、baseline、release 三种模式路由到审查、验证或执行修复。

## 先看哪里

- [项目文档索引](docs/README.md)：公开使用说明和维护入口。
- [计划结构检查接口](docs/reference/plan-quality.md)：计划结构检查与验收边界。
- `skills/`：可安装的 GSP 技能定义。
- `scripts/`：安装、构建和校验工具。

历史设计讨论、技能取舍和父本源码研究属于本地来源档案，保存在开发工作区，不进入公开仓库或安装包。

## 安装范围

可以选择项目级（`--scope project`）或当前用户全局级（`--scope user`）。全局安装供多个项目共用，不新建目标项目技能目录。任务文档和证据保存在各项目本地。AI 协助安装时，范围未说明先询问一次。具体路径与命令见 [项目文档索引](docs/README.md)。

下面是项目级安装示例：

需要 Python 3.10+，不需要 Bun、WSL、数据库或付费 API。

```powershell
python E:\GstackSP\scripts\gsp.py --project E:\MyProject install --host both
python E:\GstackSP\scripts\gsp.py --project E:\MyProject init
python E:\GstackSP\scripts\gsp.py --project E:\MyProject health --host both
```

目标目录需已存在。安装器添加本项目的技能和带标记引导块，保留用户已有内容，遇到未知或用户修改过的技能文件拒绝覆盖。核心默认安装；例如 UI 扩展：

```powershell
python E:\GstackSP\scripts\gsp.py --project E:\MyProject install --host both --optional design-consultation design-review
```

装好后在实际宿主调用 `gsp-workflow`，再调用 `gsp-health` 验证真实发现。Claude 通常为 `/gsp-workflow`，Codex 为 `$gsp-workflow`；实际列表由宿主决定，项目引导文件提供读取路径。脚本检查文件存在不等于真实会话已加载。

## 日常使用

新任务采用 `docs/work/<年份>/<月份>/<固定档案>/`，先保存想法用 `work-new`，文档按阶段生成。`health --documents-only` 检查结构并保存独立报告；旧项目先预览再整理。命令及边界见 [项目文档索引](docs/README.md)。旧任务继续兼容，以 status/resume 返回路径为准。

告诉 AI：“用 GstackSP 做这个需求，先讨论设计；确认后按推荐连续推进，准备交付。”

Git 项目的新任务先盘点分支和 worktree，再记录一次工作位置选择；同一任务恢复时复用有效绑定。多会话、并行 agent 与前后端协作见[分支与 worktree 规则](docs/reference/git-topology.md)。如果需要主会话协调多个独立聊天，使用明确的 `cross-session` 意图；主会话维护唯一总体计划，使用 `session-dispatch` 批量发布 ready 任务，使用 `session-progress` 展示进度，使用 `session-watch` 消费完成事件，并在接受后使用 `session-clean` 记录 worktree 清理。完整规则见[跨会话开发模式](docs/reference/cross-session-development.md)。

冻结没说明时问一次；已开启无人值守并授权推荐选择时记录后继续。小修改可以原地冻结，大功能可用隔离工作区。你已授权的推送、合并、删除分支等动作按精确范围执行，不重复问；默认只准备交付。

目标项目的任务记录和 `.gsp/` 状态由目标项目本地维护；公开仓库只提供运行时代码、技能和用户文档。计划结构检查与语义边界见[接口说明](docs/reference/plan-quality.md)。

完整发布源和生成规则见 `scripts/build_skills.py`、`scripts/sp_preservation.py` 及 `skills/gsp-workflow/references/`。

## 开发此项目

```powershell
python scripts/build_skills.py
python -m unittest discover -s tests -v
python scripts/validate_skills.py
python scripts/package.py
```

`scripts/build_skills.py` 是生成技能的维护源；路由 `gsp-workflow` 和共同契约手工维护。可选技能通过当前宿主/项目已有工具完成工作，未集成第三方浏览器守护进程、模型服务或部署账户。


