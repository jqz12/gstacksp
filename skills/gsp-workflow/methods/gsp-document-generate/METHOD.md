<!-- internal-method: gsp-document-generate -->
# gsp-document-generate

Owner: `gsp-execute`

Trigger: 功能、模块或项目确有缺失的使用说明或接口文档需要补齐时

This is a conditional internal method resource, not a discoverable skill. Load the complete method only when the trigger applies.

从真实代码、运行配置和已确认行为提炼读者任务；优先最短可运行示例、参数、失败处理和验证方式。实际执行文档命令，未知行为明确标记。写到项目正常文档路径并在任务计划关联，不复制全部源码、不强制每次改动生成整套文档。

共享规则：[方法索引](../../references/methods.md)、[阶段转换](../../references/stage-transitions.md)、[跨会话](../../references/cross-session.md)、[共同契约](../../references/contract.md)。
