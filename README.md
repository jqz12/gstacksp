# GstackSP

适合个人开发者的 Windows 开发工作流，供 Claude Code 和 Codex 使用。

以 Superpowers 的设计、计划、测试和审查为主干，加入按授权连续执行、冻结、经验、恢复与交付。**20 个核心技能、27 个按需扩展**；没有必要每个任务都加载全部技能。原盘点的 17 个入口加宿主引导、执行、验证三个融合入口，构成这 20 个。

## 先看哪里

- [项目文档索引](docs/README.md)：当前使用说明、维护资料与任务记录的分类入口。
- [安装与使用](docs/安装与使用.md)：装到哪个项目、如何调用、如何验证。
- [用户级安装维护](docs/用户级安装维护.md)：正式安装、升级、卸载、备份与中断恢复。
- [经验管理](docs/经验管理.md)：有效结论、来源去重、纠错、失效与历史。
- [无人值守驱动器](docs/无人值守驱动器.md)：Codex 后台执行、状态门禁、停止和恢复。
- [文件流转与阅读指南](docs/文件流转与阅读指南.md)：你看什么，AI 看什么，最终保存在哪里。
- [TODO 使用说明](docs/TODO使用说明.md)：人读待办与执行记录如何分工、关联和迁移。
- [技能目录](docs/技能目录.md)：实际发布入口及融合来源。
- [实现范围与验证](docs/实现范围与验证.md)：已实现机制、测试与真实宿主验证边界。
- [机制与产物复核](docs/audits/2026-11-07-mechanisms.md)：父本能力的实际去向、遗漏修正及未完成项。
- [项目待办](TODOS.md)：本工具本身仍未完成的工作和已验证完成记录。

早期《技能解析》《一阶段设计》、技能取舍讨论和父本源码研究统一放在 `research/`，入口为 `research/README.md`。它们是来源档案，不作为当前运行规范，不随安装包发布。

## 安装到目标项目

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

告诉 AI：“用 GstackSP 做这个需求，先讨论设计；确认后按推荐连续推进，准备交付。”

冻结没说明时问一次；已开启无人值守并授权推荐选择时记录后继续。小修改可以原地冻结，大功能可用隔离工作区。你已授权的推送、合并、删除分支等动作按精确范围执行，不重复问；默认只准备交付。

你在目标项目打开 `docs/gsp/README.md` 看当前任务，确认 `design.md`，完成时查看 `delivery.md`；根 `TODOS.md` 看跨任务未解决事项。运行状态在 `.gsp/`，AI 通过脚本更新和恢复。

## 开发此项目

```powershell
python scripts/build_skills.py
python -m unittest discover -s tests -v
python scripts/validate_skills.py
python scripts/package.py
```

`scripts/build_skills.py` 是生成技能的维护源；路由 `gsp-workflow` 和共同契约手工维护。可选技能通过当前宿主/项目已有工具完成工作，未集成第三方浏览器守护进程、模型服务或部署账户。
