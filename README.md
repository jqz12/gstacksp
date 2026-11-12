# GstackSP

适合个人开发者的 Windows 开发工作流，供 Claude Code 和 Codex 使用。

保留 Superpowers 完整技能正文与配套资源，以其设计、计划、测试和审查为主干，加入按授权连续执行、冻结、经验、恢复、项目代码质量检测与交付。**22 个核心技能、28 个按需扩展**；没有必要每个任务都加载全部技能。代码质量检测只运行项目声明的检查，按 change、baseline、release 三种模式路由到审查、验证或执行修复。

## 先看哪里

- [项目文档索引](docs/README.md)：当前使用说明、维护资料与任务记录的分类入口。
- [安装与使用](docs/安装与使用.md)：装到哪个项目、如何调用、如何验证。
- [用户级安装维护](docs/用户级安装维护.md)：正式安装、升级、卸载、备份与中断恢复。
- [经验管理](docs/经验管理.md)：有效结论、来源去重、纠错、失效与历史。
- [无人值守驱动器](docs/无人值守驱动器.md)：Codex 后台执行、状态门禁、停止和恢复。
- [Codex 全局 hooks](docs/Codex全局hooks.md)：独立安装、冻结检查、实验事件记录与宿主信任。
- [验证输入与扫描](docs/验证输入与扫描.md)：构建缓存排除、必要配置纳入、进度与总超时。
- [测试结果与实验反馈](docs/测试结果与实验反馈.md)：Cargo 多目标统计、测试入口绑定和失败基线。
- [文件流转与阅读指南](docs/文件流转与阅读指南.md)：你看什么，AI 看什么，最终保存在哪里。
- [TODO 使用说明](docs/TODO使用说明.md)：人读待办与执行记录如何分工、关联和迁移。
- [技能目录](docs/技能目录.md)：实际发布入口及融合来源。
- [实现范围与验证](docs/实现范围与验证.md)：已实现机制、测试与真实宿主验证边界。
- [机制与产物复核](docs/audits/2026-11-07-mechanisms.md)：父本能力的实际去向、遗漏修正及未完成项。
- [项目待办](TODOS.md)：本工具本身仍未完成的工作和已验证完成记录。

早期《技能解析》《一阶段设计》、技能取舍讨论和父本源码研究统一放在 `research/`，入口为 `research/README.md`。它们是来源档案，不作为当前运行规范，不随安装包发布。

## 安装范围

可以选择项目级（`--scope project`）或当前用户全局级（`--scope user`）。全局安装供多个项目共用，不新建目标项目技能目录；任务文档和证据仍保存在各项目。AI 协助安装时，范围未说明先询问一次。具体路径与命令见 [安装与使用](docs/安装与使用.md)。

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

新任务采用 `docs/work/<年份>/<月份>/<固定档案>/`，先保存想法用 `work-new`，文档按阶段生成。`health --documents-only` 检查结构并保存独立报告；旧项目先预览再整理。命令及边界见 [文档治理与健康检查](docs/文档治理与健康检查.md)。旧任务继续兼容，以 status/resume 返回路径为准。

告诉 AI：“用 GstackSP 做这个需求，先讨论设计；确认后按推荐连续推进，准备交付。”

冻结没说明时问一次；已开启无人值守并授权推荐选择时记录后继续。小修改可以原地冻结，大功能可用隔离工作区。你已授权的推送、合并、删除分支等动作按精确范围执行，不重复问；默认只准备交付。

你在目标项目打开 `docs/gsp/README.md` 看当前任务，沿任务链接确认 `docs/work/YYYY/MM/<固定档案>/spec.md`，查看同档案的 `plan.md` 和完成时的 `delivery.md`；根 `TODOS.md` 看跨任务未解决事项。旧任务以实际绑定为准。运行状态在 `.gsp/`，AI 通过脚本更新和恢复。计划结构检查与语义边界见[接口说明](docs/reference/plan-quality.md)。

完整原文、局部差异及文档治理见 [SP 保留与适配清单](docs/SP保留与适配清单.md)。

## 开发此项目

```powershell
python scripts/build_skills.py
python -m unittest discover -s tests -v
python scripts/validate_skills.py
python scripts/package.py
```

`scripts/build_skills.py` 是生成技能的维护源；路由 `gsp-workflow` 和共同契约手工维护。可选技能通过当前宿主/项目已有工具完成工作，未集成第三方浏览器守护进程、模型服务或部署账户。

