<!-- internal-method: gsp-design-review -->
# gsp-design-review

Owner: `gsp-review`

Trigger: 已有可运行页面需要检查视觉质量与交互一致性时

This is a conditional internal method resource, not a discoverable skill. Load the complete method only when the trigger applies.

先明确只报告还是允许修改，读 UI 规范并用 browse 检查真实页面的桌面/窄屏、层级、间距、对比度、表单状态和交互。截图和复现记录到运行目录。优先实际影响，不为得分反复润色；允许修复时走 execute、回归并再截图。浏览器不可用只做静态检查并明确未覆盖。

共享规则：[方法索引](../../references/methods.md)、[阶段转换](../../references/stage-transitions.md)、[跨会话](../../references/cross-session.md)、[共同契约](../../references/contract.md)。
