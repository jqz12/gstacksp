<!-- internal-method: gsp-setup-deploy -->
# gsp-setup-deploy

Owner: `gsp-ship`

Trigger: 项目第一次需要配置具体部署目标与验证流程时

This is a conditional internal method resource, not a discoverable skill. Load the complete method only when the trigger applies.

读项目运行方式，确认环境、构建、启动、域名/健康检查和配置来源。优先已有服务与账户，敏感值留在适合的秘密存储，只写占位名称。需要新增费用/服务操作先核对已有授权。把可复现步骤写项目部署文档，在测试环境验证后再允许 land-and-deploy。

共享规则：[方法索引](../../references/methods.md)、[阶段转换](../../references/stage-transitions.md)、[跨会话](../../references/cross-session.md)、[共同契约](../../references/contract.md)。
