<!-- internal-method: gsp-qa -->
# gsp-qa

Owner: `gsp-execute`

Trigger: 实现后需要验证浏览器、API 或 CLI 的实际使用流程与边界时

This is a conditional internal method resource, not a discoverable skill. Load the complete method only when the trigger applies.

## GSP stage integration

先给基线发现再修复；每次在 .gsp/runs/<id>/qa/<唯一运行ID>/ 保存报告、基线及截图，目录存在则换新 ID，禁止覆盖旧材料。报告关联设计/计划中的测试矩阵。未解决项按待办规则登记，已回退修复与未验证修复不得标成 verified。

先明确只报告还是允许修复；用户要求只读则不改代码。按设计验收走最关键成功、失败和边界路径，记录实际输入、输出、期望、严重度和复现步骤。UI 通过 browse；API/CLI 使用项目实际工具和受控测试数据。只在授权环境执行，不把线上数据当测试夹具。修复模式下回到 execute/TDD，验证后再关闭问题；不要为达到评分无限优化。未覆盖项写 review.md，原始证据存运行目录，测试通过不等于所有 QA 已完成。

共享规则：[方法索引](../../references/methods.md)、[阶段转换](../../references/stage-transitions.md)、[跨会话](../../references/cross-session.md)、[共同契约](../../references/contract.md)。
