<!-- internal-method: gsp-modernize -->
# gsp-modernize

Owner: `gsp-ship`

Trigger: 已有老旧 PR 或任务分支需要评估是否继续、重做或关闭时

This is a conditional internal method resource, not a discoverable skill. Load the complete method only when the trigger applies.

先只读核对 PR 目标、分支差异和当前基线，列出冲突、已被替代需求与剩余价值。给出继续/重做/关闭建议。fetch、改分支、推送、关闭 PR 按用户指令范围执行，保留原分支证据，不自动强推。选择继续后将剩余工作转为任务并回归验证。

共享规则：[方法索引](../../references/methods.md)、[阶段转换](../../references/stage-transitions.md)、[跨会话](../../references/cross-session.md)、[共同契约](../../references/contract.md)。
