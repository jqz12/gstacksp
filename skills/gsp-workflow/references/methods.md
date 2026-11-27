# Conditional internal methods

The canonical manifest defines 28 internal source duties: 26 complete METHOD.md resources plus one shared workflow recovery reference used by both context duties. Read a method only when its trigger applies. A method entry is not a skill and grants no extra authorization.

| Method | Owner | Trigger | Resource |
|---|---|---|---|
| `gsp-design-consultation` | `gsp-brainstorming` | 新界面需要确定视觉体系、字体、色彩、布局和交互基调时 | [resource](../methods/gsp-design-consultation/METHOD.md) |
| `gsp-design-shotgun` | `gsp-brainstorming` | 用户明确希望比较多套界面视觉方案时 | [resource](../methods/gsp-design-shotgun/METHOD.md) |
| `gsp-autoplan` | `gsp-writing-plans` | 复杂或有风险的实施计划需要在开发前进行范围与工程审查时 | [resource](../methods/gsp-autoplan/METHOD.md) |
| `gsp-plan-tune` | `gsp-writing-plans` | 用户明确要求调整哪些常规问题需要询问或使用推荐答案时 | [resource](../methods/gsp-plan-tune/METHOD.md) |
| `gsp-plan-design-review` | `gsp-writing-plans` | 实施前的 UI 计划需要检查视觉交互、状态和可访问性覆盖时 | [resource](../methods/gsp-plan-design-review/METHOD.md) |
| `gsp-plan-devex-review` | `gsp-writing-plans` | CLI、SDK、API 或技能计划需要检查使用者上手和错误处理体验时 | [resource](../methods/gsp-plan-devex-review/METHOD.md) |
| `gsp-test-driven-development` | `gsp-execute` | 实现或修复可自动验证的行为，需要可靠回归测试时 | [resource](../methods/gsp-test-driven-development/METHOD.md) |
| `gsp-systematic-debugging` | `gsp-execute` | 测试失败、异常输出或错误行为需要先定位根因再修复时 | [resource](../methods/gsp-systematic-debugging/METHOD.md) |
| `gsp-code-quality` | `gsp-execute` | 项目需要建立基线、检查本次改动质量或在交付前执行完整质量门禁时 | [resource](../methods/gsp-code-quality/METHOD.md) |
| `gsp-browse` | `gsp-execute` | 前端或网页任务需要实际打开页面、交互、截图或查看运行错误时 | [resource](../methods/gsp-browse/METHOD.md) |
| `gsp-qa` | `gsp-execute` | 实现后需要验证浏览器、API 或 CLI 的实际使用流程与边界时 | [resource](../methods/gsp-qa/METHOD.md) |
| `gsp-open-gstack-browser` | `gsp-execute` | 需要启动或连接当前宿主可用的浏览器测试会话时 | [resource](../methods/gsp-open-gstack-browser/METHOD.md) |
| `gsp-setup-browser-cookies` | `gsp-execute` | 用户明确要求为测试浏览器配置已有登录态时 | [resource](../methods/gsp-setup-browser-cookies/METHOD.md) |
| `gsp-benchmark` | `gsp-execute` | 页面性能或资源指标明确属于任务验收目标时 | [resource](../methods/gsp-benchmark/METHOD.md) |
| `gsp-document-generate` | `gsp-execute` | 功能、模块或项目确有缺失的使用说明或接口文档需要补齐时 | [resource](../methods/gsp-document-generate/METHOD.md) |
| `gsp-verify` | `gsp-review` | 准备宣布任务完成、提交交付准备或复用测试证据之前 | [resource](../methods/gsp-verify/METHOD.md) |
| `gsp-design-review` | `gsp-review` | 已有可运行页面需要检查视觉质量与交互一致性时 | [resource](../methods/gsp-design-review/METHOD.md) |
| `gsp-devex-review` | `gsp-review` | 工具实现后需要实际验证安装、首次使用、错误与恢复体验时 | [resource](../methods/gsp-devex-review/METHOD.md) |
| `gsp-git` | `gsp-ship` | Git health route 指向 Git 专业处理，或用户要求检查分支、归属、提交和分支收尾时 | [resource](../methods/gsp-git/METHOD.md) |
| `gsp-land-and-deploy` | `gsp-ship` | 交付已准备且用户授权合并或部署到具体环境时 | [resource](../methods/gsp-land-and-deploy/METHOD.md) |
| `gsp-setup-deploy` | `gsp-ship` | 项目第一次需要配置具体部署目标与验证流程时 | [resource](../methods/gsp-setup-deploy/METHOD.md) |
| `gsp-landing-report` | `gsp-ship` | 需要只读查看多个待合并分支或 PR 的状态时 | [resource](../methods/gsp-landing-report/METHOD.md) |
| `gsp-canary` | `gsp-ship` | 已授权部署后需要在约定时间观察健康指标时 | [resource](../methods/gsp-canary/METHOD.md) |
| `gsp-modernize` | `gsp-ship` | 已有老旧 PR 或任务分支需要评估是否继续、重做或关闭时 | [resource](../methods/gsp-modernize/METHOD.md) |
| `gsp-context-save` | `gsp-workflow` | 任务将中断、换宿主、压缩上下文或需要可恢复交接时 | [resource](contract.md) |
| `gsp-context-restore` | `gsp-workflow` | 恢复中断任务、压缩后的上下文或将同一项目交给另一个宿主时 | [resource](contract.md) |
| `gsp-scrape` | `gsp-workflow` | 用户需要从可访问网页提取结构化数据时 | [resource](../methods/gsp-scrape/METHOD.md) |
| `gsp-skillify` | `gsp-writing-skills` | 已成功重复的网页提取流程值得形成可复用技能时 | [resource](../methods/gsp-skillify/METHOD.md) |
