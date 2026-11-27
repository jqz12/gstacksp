<!-- internal-method: gsp-canary -->
# gsp-canary

Owner: `gsp-ship`

Trigger: 已授权部署后需要在约定时间观察健康指标时

This is a conditional internal method resource, not a discoverable skill. Load the complete method only when the trigger applies.

先明确服务、版本、观察窗口、指标阈值和失败后的授权动作。使用已有监控或无副作用健康请求，记录时间与结果。观察结束给出结论和未覆盖；不能声称在会话结束后仍后台监控。回滚只在已明确授权范围执行，费用和线上负载均按真实约束。

共享规则：[方法索引](../../references/methods.md)、[阶段转换](../../references/stage-transitions.md)、[跨会话](../../references/cross-session.md)、[共同契约](../../references/contract.md)。
