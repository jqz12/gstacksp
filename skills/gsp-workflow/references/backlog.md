# 项目待办生命周期

## 文件正本与范围

根目录 `TODOS.md` 保存跨任务问题、接受的延期与技术债；当前需求的实施任务仍在 plan/state。已有 `TODO.md` 时沿用它，不另建一份；两者同时存在报歧义，先明确正本。`init` 只在两者都没有时创建骨架，已有文件不覆盖。发现原文件丢失先调查，不盲用 init 抹掉历史。

TODO 以人阅读为主，按组件分区、区内 P0–P4 排序，Completed 在末尾。正文保留稳定 ID、标题、What、Why、Context、Effort、Priority；依赖 Depends on / Blocked by 可选，完成后显示版本和日期。支持多段背景和人工附注，不把执行流水暴露成十多个正文栏位。保留 ID 与边界注释，人工修改正文后 AI 必须读取最新内容。

`.gsp/backlog/records.json` 保存来源归因、Origin、Blocking、任务关联、Evidence、Resolution、History；不复制标题和问题正文。todo-list 每次从当前 Markdown 派生条目再合并记录，不能用旧索引覆盖人工编辑。P0–P4 是排序，Blocking 才表示某需求的交付阻塞；两者不能混为一谈。执行记录不是可随意重建的缓存，丢失时恢复备份，不能从一行 Completed 伪造过去的验证。

不把非目标、被否定的建议自动登记为待办。需要记录的是有实际来源且值得保留的工作；普通发现可在当前授权内登记，写清 AI 观察或用户决定，不能伪造批准延期。必做缺口在解决前保持 blocker。

## 规划、发现、交付、恢复

1. 设计和计划前读原文与 `todo-list`，避免重复工作；计划纳入事项引用其 ID，再用 todo-link 关联完整实施任务。关联本身不表示问题解决。
2. 评审/QA/调试发现问题，先决定立即修复、按依据延期或经核实不成立。真实未解决项落盘；没有授权的功能扩张不自动执行。
3. 当前需求必需的问题登记 `--blocking --origin <需求ID>`。其他项目问题可非阻塞；ready 不会因为其他任务的问题强行扩大本次范围。
4. 完成任务并跑其真实检查后，核实问题与验收对应才 todo-done，使用 --version 记录实际发布/交付版本；未知版本如实不指定。随后再做最终 all 回归、整体审查、ready。待办正文和执行记录变化都会使旧证据过期。
5. 交付报告披露未完成项并链接正本；人工/历史内容需原文核对，机器数量不代表全部问题。
6. context-save 保留下一步与来源，resume 同时返回待办路径与当前事项。经验库和 gbrain 不替代此文件。

## 命令

`<gsp>` 使用共同契约定义的真实脚本位置。

```text
<gsp> todo-list
<gsp> todo-list --origin login
<gsp> todo-add --title "错误提示缺失" --what "配置缺失时没有解释" --why "用户无法恢复" --context "复现输入、现象及处理起点" --source "docs/gsp/tasks/login/review.md#finding-1" --component "配置与启动" --priority P1 --effort S --origin login --blocking
<gsp> todo-link GSP-0001 --run login --task T1
<gsp> todo-done GSP-0001 --run login --task T1 --reason "具体修复、断言和覆盖范围" --version 1.2.3
<gsp> todo-reopen GSP-0001 --reason "哪个输入再次复现、来源在哪里"
<gsp> todo-migrate
<gsp> todo-format
<gsp> todo-recover --reason "已核对事务和当前文件的调查结论"
```

相同来源与问题复用 ID，关联需求或阻塞分类冲突则拒绝，需要明确核对。重复发现已完成项不会自动重新打开；核实复现后 todo-reopen。完成命令要求所指任务确已 done 且验证指纹、设计/计划、日志哈希均有效，再保存 receipt 路径和理由；它不自动理解“这个测试是否足以证明该问题解决”，调用者仍需解释行为对应关系。重开保留此前解决记录。

旧格式只读兼容，显式 todo-migrate 或首次写入时先备份迁移，原内容与证据历史保全。历史版本不明标注“历史完成记录”，不拿当前版本充当过去的完成版本。todo-format 按现有组件/优先级整理，不替人猜组件。新人工笔记可保持普通 Markdown，机器计数不代表全部事项。

手动移到 Completed 不产生机器完成证明；已完成正文有变动会标为待核实。删除本次阻塞条目也不会自动解除阻塞，记录中保留原 ID，不能复用为另一个问题。

两文件更新中断会留下 `.gsp/backlog/transaction.json` 并阻止继续。先核对现场，再 todo-recover；仅接受已知前/后内容，出现新的人工编辑拒绝覆盖。迁移备份位于 migrations/，事务历史位于 history/，都在 .gsp/backlog 下，仅供调查恢复。

文件维护失败要在任务记录/最终回复说明未保存事项和恢复下一步，不假称已入库；不能借落盘失败豁免交付阻塞。禁止绕过 CLI 手改完成证据以通过检查。
