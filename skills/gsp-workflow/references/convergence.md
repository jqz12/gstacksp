<!-- convergence-contract -->
# 收敛、切片与检查点

本契约只在计划切片、执行 checkpoint 或候选审查阶段按需读取；普通小任务和
当前阶段不适用时不读取。它不新增模型调用、技能、审批或报表。

## 三道门

1. outcome：每个任务只有一个可验收结果。跨模块、接口、数据所有权或并行写入边界时，
   拆成有序且各自可验的切片，并保留前一 accepted slice 的恢复状态。
2. review：固定当前 candidate digest 和 contract digest。当前契约违约、引入回归或
   安全问题阻断；独立真实缺陷登记后续归属；可选改善退出本收敛循环。
3. time：每 45 分钟 checkpoint 已接受切片、证据、阻塞和下一步；90 分钟没有 accepted
   slice 时停止扩大 scope，由 Sol 裁决继续、缩小、交接或阻塞。重试或换 agent 不重置
   时间、问题或证据；未知进程/outcome 不自动接受或终止。

## 最短 Convergence JSON

单一 outcome，写在对应 `## Tnn` 小节的一行：

```text
Convergence: {"requires_slices":false,"single_outcome":"可检查的单一结果"}
```

跨边界并有依赖的切片：

```text
Convergence: {"requires_slices":true,"slices":[{"id":"G1","outcome":"第一片结果","checks":["定向检查"]},{"id":"G2","outcome":"第二片结果","checks":["集成检查"],"deps":["G1"]}]}
```

计划只声明切片；accepted 状态、candidate 和证据只能在执行时记录，不能写回计划冒充完成。

## review.md 候选证据

最终审查报告包含一个块的 `gsp-review-evidence`：

```text
<!-- gsp-review-evidence
{"candidate_digest":"<sha256>","contract_digest":"<sha256>","findings":[],"reviewer_context":".gsp/runs/session-context/<session-uid>/<binding-uid>.json"}
-->
```

`candidate_digest`、`contract_digest`、`findings` 和 `reviewer_context` 必须存在。
`reviewer_context` 是现有 reviewer binding 的句柄路径，不是自报角色或布尔值。
`verified-context` 由程序校验 authority、角色、代际和独立 thread 后产生；除非当前入口有真实宿主观察，
`host_observation` 如实写 `unknown`，不得据此声称宿主、具体模型或模型行为已验证。

同一问题首次可由 `worker_repair` 修正；第二次 `worker_repair` 必须拒绝，由当前 controller
执行 `sol_takeover` 接管，且 `repair_count` 不重置。候选 digest 标识复核内容，不是目录冻结。
