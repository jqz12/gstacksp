<!-- internal-method: gsp-setup-browser-cookies -->
# gsp-setup-browser-cookies

Owner: `gsp-execute`

Trigger: 用户明确要求为测试浏览器配置已有登录态时

This is a conditional internal method resource, not a discoverable skill. Load the complete method only when the trigger applies.

说明需要的站点、环境和最小凭据范围，优先测试账户和宿主受支持的认证存储。仅在明确授权后配置，不读取或输出整个浏览器 cookie 库，不把凭据写入项目版本库。用一次无副作用的身份页面核实登录；记录状态位置和清理方式，缺工具时交由用户登录，不伪造会话。

共享规则：[方法索引](../../references/methods.md)、[阶段转换](../../references/stage-transitions.md)、[跨会话](../../references/cross-session.md)、[共同契约](../../references/contract.md)。
