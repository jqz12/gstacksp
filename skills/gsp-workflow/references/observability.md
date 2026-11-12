# GSP 观测日志

GSP 的观测日志用于发现工作流偏离，不替代 `.gsp/runs` 的任务状态、命令收据或用户授权。

## 文件位置

目标项目本地保存：

```text
.gsp/telemetry/events.jsonl
docs/gsp/reports/observability/<运行ID>/report.json
docs/gsp/reports/observability/<运行ID>/report.md
```

事件只追加，报告按运行 ID 新建，不覆盖历史报告。原始事件不进入 GstackSP 源仓发布包，也不自动同步到 gbrain。

## 事件语义

- `declared`：回复或交付记录声明使用了某技能。
- `observed`：CLI、Codex hook 或 Claude hook 有可配对事件。
- `inferred`：由任务状态或 route 推导，不能作为宿主调用证明。
- `unknown` / `not_checked`：没有足够证据，必须保留未知状态。

事件可以包含技能、预期下一技能、实际下一技能、偏离、人工决策、任务编号和收据路径摘要。禁止保存完整 prompt、响应、stdout、stderr、凭据、Cookie 和项目源代码。

## 生成报告

```powershell
python <gsp-workflow/scripts/gsp.py> --project <项目> observe
```

报告至少说明事件数量、技能/事件统计、unknown 数量、路由偏离、证据入口和下一步改进候选。候选只供 GSP 主仓审查，不自动修改技能或用户偏好。

宿主事件没有配对时，报告必须写明原生触发 `NOT_CHECKED`。技能文件存在、技能被宿主发现和技能遵循方法是三个不同结论。
