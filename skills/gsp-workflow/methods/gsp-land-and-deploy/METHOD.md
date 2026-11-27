<!-- internal-method: gsp-land-and-deploy -->
# gsp-land-and-deploy

Owner: `gsp-ship`

Trigger: 交付已准备且用户授权合并或部署到具体环境时

This is a conditional internal method resource, not a discoverable skill. Load the complete method only when the trigger applies.

核对 ship 结果、当前 HEAD、目标分支/环境和实际授权。按项目已有流程合并并验证合并后代码，再部署；记录每一步真实输出和目标版本。失败停止后续发布并保留恢复位置，不擅自回滚用户数据。服务健康和真实部署成功需要观察证据，不能只凭命令提交成功。

共享规则：[方法索引](../../references/methods.md)、[阶段转换](../../references/stage-transitions.md)、[跨会话](../../references/cross-session.md)、[共同契约](../../references/contract.md)。
