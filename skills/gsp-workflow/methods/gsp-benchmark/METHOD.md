<!-- internal-method: gsp-benchmark -->
# gsp-benchmark

Owner: `gsp-execute`

Trigger: 页面性能或资源指标明确属于任务验收目标时

This is a conditional internal method resource, not a discoverable skill. Load the complete method only when the trigger applies.

定义页面、设备、网络、数据和指标，固定环境做前后多次测量。采用项目现有 Lighthouse/性能测试，保存命令、原始结果和波动，区分测量与估计。基准差异不足时不宣布提升；优化后复查功能与可访问性。工具缺失则报告并由用户决定是否安装，不增加默认云费用。

共享规则：[方法索引](../../references/methods.md)、[阶段转换](../../references/stage-transitions.md)、[跨会话](../../references/cross-session.md)、[共同契约](../../references/contract.md)。
