# 计划结构检查接口

当前实现正本为 `gsp/plan_quality.py` 和 `gsp/runtime.py`。本文人工维护接口行为；修改这两个入口时同步核对本文及 `tests/test_plan_quality.py`。不另维护 OpenAPI，本接口是本地 CLI/Python。

## CLI

```powershell
python scripts/gsp.py --project E:/MyProject plan-check <运行ID>
python scripts/gsp.py --project E:/MyProject seal-plan <运行ID> --check-structure
```

`plan-check` 读取指定运行的绑定计划和登记任务，只向 stdout 输出 JSON；不扫描工程文件、不获取写锁、不改状态/索引/计划。不要在其他写者正在改计划时把瞬时结果当成最终验收，实际封存使用第二个命令。

退出 0 表示支持的结构检查无缺项；1 表示结构缺项或无法检查。结构缺项返回 JSON，IO/绑定/解析失败输出 `GSP_ERROR` 到 stderr。计划上限 1 MiB（按 UTF-8 字节），超限报 `PLAN_TOO_LARGE`。`seal-plan --check-structure` 在既有写锁和设计/任务校验下检查，结构失败报 `PLAN_STRUCTURE_INVALID`，不封存。

不带开关的旧封存调用保持兼容。新版 GSP 计划技能要求带开关，这不是覆盖所有旧调用的全局强制门禁。封存哈希保护之后的改动，不能证明内容合理。

## Python

`check_plan(text: str, task_ids: list[str]) -> dict` 是纯文本检查。

`inspect_plan(project: Project, task_id: str) -> dict` 使用绑定路径检查并增加 `task` 和 `plan`，不得调用具有状态失效副作用的 `Project.state()`。

返回示例（示例不是执行收据）：

```json
{
  "schema": 1,
  "structural_ok": false,
  "findings": [{"code": "CONSUMES_MISSING", "task": "T1", "detail": "Missing task field: Consumes"}],
  "semantic_review": "NOT_CHECKED",
  "task": "sample",
  "plan": "docs/work/2026/11/GSP-000001-example/plan.md"
}
```

`task` 为 null 的 finding 表示全局问题。所有诊断 code 使用 `<字段>_MISSING`，另有 `TASK_DUPLICATE` 和 `TASKS_MISSING`。调用方应展示 detail，不通过字符串猜语义验收成功。

## 支持格式及边界

按 SP 模板使用显式标签 Goal/Architecture/Tech Stack/Spec，以及章节 Global Constraints/Review Focus/Self-Review。中文支持目标/架构/技术栈/规格、全局约束/审查重点/自查。自查结果记录在同一个计划中，是 GSP 的可追溯落点要求。

每个登记任务使用 `## Task T1: 标题`、`### 任务 T1：标题` 或 `## T1 标题`，ID 必须与登记一致。需要 Files/Interfaces/Consumes/Produces 标签、checkbox 步骤、Run/Expected。中文支持文件/接口/消费/产出/运行/预期。允许加粗标签，Files 可在标签下一行列文件。命令及预期需有非空行内说明，代码块用于示例而不提供结构标签。

围栏代码块和 HTML 注释被排除。工具不是完整 Markdown 解析器，不保证所有等价排版均识别。缺项提示须结合正文核对；已有 prose Spec 链接被报缺标签不等于其真实缺少链接。

无程序接口可写 `N/A — 本任务只修改说明，不改变参数或调用`。无自动测试需解释理由并记录实际替代检查及预期。结构检查不会验证理由真伪、链接存在、签名一致、错误语义、每个步骤的有效性或真实测试结果；`semantic_review` 始终为 `NOT_CHECKED`。这些必须由原版计划自查、执行预检与整体审查完成。


## 新后台计划的接纳（0.8.9）

新后台计划由 worker 返回完整正文与匹配的任务列表。worker 做内容自查；主控负责登记任务、结构检查与封存，不要求只读 worker 执行 GSP 记账命令。

主控在 accepted.json、正式计划与任务写入之前，生成含实际 Spec 链接的候选正文，按 UTF-8 1 MiB 上限和现有 check_plan 核对。失败保留原始 response.json，并写同 attempt 的 rejected.json（job、响应哈希、quality）；不写 accepted，不改变绑定计划、任务与 seal。job.plan_findings 将结构诊断交给下一次规划。

结构缺项自动进入 plan 修复链：初次不计修复，重试派发前记入 fix_rounds.plan，复用 max_fix_rounds、总派发数与绝对截止时间，重启不清零。有效修复接纳后清除当前反馈与 pending_fix，保留历史次数，正常实施不继续计作计划修复。超过预算停止并报告原因；不能手改计数解除。

实际写入后仍以 seal-plan --check-structure 核对绑定正文。已有任务不能自动清空重建；旧无开关 CLI、已有封存和历史任务保持兼容。结构结果始终 semantic_review=NOT_CHECKED，不证明迁移方案、接口含义或需求覆盖正确。

格式不可信、状态改变、进程/文件写入中断仍属于未知执行，需要按 drive-recover 调查恢复。存在 accepted.json 或 rejected.json 的未完成接纳禁止直接重放；标记后持久状态写入失败也不能当成普通计划缺项重试。保留原始响应不等于其已被接纳。

调查恢复时，若 pending attempt 已有 rejected.json，主控先核对 job、响应哈希和重新计算的结构诊断，再恢复 plan 修复标志与反馈；不增加免费的初次尝试，也不重置历史预算。记录不匹配报 PLAN_REJECTION_MISMATCH，保留现场继续调查。若计划已成功封存而最后的驱动状态写入中断，核对 seal 后转 execute，清除临时计划反馈并保留 fix_rounds；重复恢复不会重新计费或放宽预算。
