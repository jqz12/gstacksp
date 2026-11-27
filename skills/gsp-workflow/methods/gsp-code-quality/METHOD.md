<!-- internal-method: gsp-code-quality -->
# gsp-code-quality

Owner: `gsp-execute`

Trigger: 项目需要建立基线、检查本次改动质量或在交付前执行完整质量门禁时

This is a conditional internal method resource, not a discoverable skill. Load the complete method only when the trigger applies.

按场景选择 `change`、`baseline` 或 `release`。先读取项目 `.gsp/quality.json`；只运行其中明确登记的 argv，不遍历 target/build/node_modules，不猜测命令，也不自动修复。质量栈至少可以声明 build/type、format/lint、test、security、maintainability 和 contract 检查；每项写明 id、kind、cwd、modes、required 和 timeout。运行 `<gsp> quality --mode change|baseline|release --task <任务ID>`，结果保存到 `.gsp/runs/<任务ID>/quality/<运行ID>/report.json`、`report.md`、`commands.jsonl` 和 `evidence/`。`FAIL` 是已启动的必需检查失败，`BLOCKED` 是必需检查无法启动，`NOT_CHECKED` 是没有声明式质量栈；工具特征推断出的命令只能列为候选。change 通过后交给 gsp-review，baseline 通过后回到 gsp-workflow 让用户决定基线，release 通过后交给 gsp-verify；失败回到 gsp-execute，先用 systematic-debugging 定位再修复。报告不得以单一分数替代证据，不把质量门禁冒称为真实宿主触发或业务验收。

共享规则：[方法索引](../../references/methods.md)、[阶段转换](../../references/stage-transitions.md)、[跨会话](../../references/cross-session.md)、[共同契约](../../references/contract.md)。
