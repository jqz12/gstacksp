<!-- internal-method: gsp-open-gstack-browser -->
# gsp-open-gstack-browser

Owner: `gsp-execute`

Trigger: 需要启动或连接当前宿主可用的浏览器测试会话时

This is a conditional internal method resource, not a discoverable skill. Load the complete method only when the trigger applies.

先检查宿主浏览器工具与项目配置，优先连接已提供的测试浏览器。没有时使用项目已有 Playwright/浏览器启动脚本，不假定存在 Aside 或 GS 二进制。记录目标 URL、会话用途与能力限制；不要关闭用户真实工作窗口或擅自安装扩展。若无可用后端，报告不可用并转可行的项目测试。

共享规则：[方法索引](../../references/methods.md)、[阶段转换](../../references/stage-transitions.md)、[跨会话](../../references/cross-session.md)、[共同契约](../../references/contract.md)。
