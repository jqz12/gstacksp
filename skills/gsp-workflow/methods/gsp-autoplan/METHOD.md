<!-- internal-method: gsp-autoplan -->
# gsp-autoplan

Owner: `gsp-writing-plans`

Trigger: 复杂或有风险的实施计划需要在开发前进行范围与工程审查时

This is a conditional internal method resource, not a discoverable skill. Load the complete method only when the trigger applies.

## GSP stage integration

逐项区分本次必做、已接受延期、明确不做；必做事项不能仅因写入待办而解除阻塞。延期需来源、理由和影响，使用共同待办规则落入根 TODOS.md。

读取当前设计、plan.md、任务状态及关键代码。只检查目标是否可达、范围是否过度、接口/依赖/失败路径、任务与验收是否完整。前端视觉和开发者体验仅在相关时调用已安装扩展，不默认执行四角色流水线。有独立审查工具且允许时使用，否则明确自查。将发现、证据、建议与未覆盖写到 decisions.md；常规决策在无人值守授权内采用推荐。修订计划后重新 seal-plan，设计行为改变则先确认变更范围。不将 findings 当成完整任务，不伪造用户 gate 答复。

共享规则：[方法索引](../../references/methods.md)、[阶段转换](../../references/stage-transitions.md)、[跨会话](../../references/cross-session.md)、[共同契约](../../references/contract.md)。
