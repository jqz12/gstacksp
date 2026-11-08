# 规格与计划治理

新任务先 init，再 new <id> --title <标题>；ID 由调用者明确选择，不自动编号，已存在就拒绝。日期是创建时本机日期，仅创建时计算，不随跨天工作变化。status 的 documents 和 resume 的 read_next 是定位依据。

- docs/superpowers/specs/YYYY-MM-DD-<id>-design.md：brainstorming 形成的设计规格唯一正本。
- docs/superpowers/plans/YYYY-MM-DD-<id>.md：writing-plans 形成的实施计划，必须引用对应规格。
- docs/gsp/tasks/<id>/：review.md、decisions.md、delivery.md、按需 driver-report.md 等。
- .gsp/runs/<id>/：机器状态、事件、检查点、独立证据、驱动现场。

new 一次创建五份模板，但实施顺序仍是讨论设计→确认批准→计划→执行→审查验证→交付。不同时维护 spec.md 和 design.md 两份正文，不把多个需求写进同一个文件。一次需求的 T1/T2 是计划内部实施任务，不另生成规格；多独立子系统按原版讨论拆分需求和独立计划。

1000 个需求会形成 1000 份规格和计划。当前目录按类型分类，文件按日期/ID 区分，仪表盘按任务链接；没有自动分页、归档或语义搜索，不宣称已经解决全部规模管理问题。不要手动移动状态关联的文件。

## 历史任务与显式关联迁移

旧状态没有 documents 时仍读 docs/gsp/tasks/<id>/design.md 和 plan.md；不能静默改它们或沿用旧批准对应新文档。

如要迁移，先从 status/resume 核对真实旧路径和是否正在执行。将旧正文复制到 specs/plans 的唯一新名称，补计划的规格相对链接，检查内容；不要先删除旧文档。然后运行：

```text
<gsp> documents-adopt <id> --spec docs/superpowers/specs/YYYY-MM-DD-<id>-design.md --plan docs/superpowers/plans/YYYY-MM-DD-<id>.md --reason "核对过的迁移依据"
```

CLI 检查文件在分类目录内、真实路径没有逃逸、没有被别的任务占用；未知执行或有驱动现场时拒绝。保存 document_history，撤销当前批准、封存和审查，增加验证代次，失效当前交付。旧正文按历史保留，不再编辑；唯一现行正本以状态 documents 为准。重新核对设计确认、approve、seal-plan 和证据；这条命令不伪造用户确认，不删除任务进度或旧证据。

这不是事务式批量文件搬迁工具；复制尚未绑定就中断时，原状态仍指向旧正文。新文件留作待核对草稿，不自行当作现行规格。旧任务不要求为升级而全部迁移。

显式关联迁移会将已完成任务标为 needs-verification，旧完成证据留在 history；重新批准和封存后按依赖 task-start → 重新验证 → task-done，不要求重写正确实现。最后重新 all 回归、整体审查和 ready。
