<!-- internal-method: gsp-git -->
# gsp-git

Owner: `gsp-ship`

Trigger: Git health route 指向 Git 专业处理，或用户要求检查分支、归属、提交和分支收尾时

This is a conditional internal method resource, not a discoverable skill. Load the complete method only when the trigger applies.

## GSP stage integration

新任务按需求声明 `--intent`；大功能或跨模块单任务可用 `new --size large` 记录风险，但规模不单独强制 worktree。并行写者、独立验证或保护现有改动才优先隔离；工作位置与构建缓存是两个独立决定。同一任务换会话先复核和复用绑定；仅一个可写任务不额外要求 integration。`--base` 明确派生提交，改变绑定须显式 `topology-select --rebind` 并重新验证；Git 创建或切换中断后先调查，再 recover-run，不自动清理。

读取 Git health 的短 handoff，不加载完整报告。检查仓库、分支、HEAD、工作区归属和任务绑定；输出下一动作。新任务建议 feature/GSP-<编号>-<slug>、fix/...、docs/...、test/...、refactor/...、chore/...、research/...。提交使用 Conventional Commit，正文包含 GSP 编号；用 `<gsp> authorize <id> commit --scope <范围> --quote "用户原话"` 登记提交授权。提交、推送、合并、删除分支必须分别取得精确授权；不自动 git add .、reset、clean 或拆分未知修改。完成提交后核对 diff、测试和 HEAD，再用 observe-head 登记。保留完整 SP worktree 原文 [using-git-worktrees](../../upstream/skills/using-git-worktrees/ORIGINAL.md) 与 finishing branch 方法 [gsp-ship](../../../gsp-ship/SKILL.md)，真实远程 Git 和宿主触发未验证时标记 NOT_CHECKED。 New tasks run <gsp> topology <id> first to show the current branch, HEAD, every worktree, dirty paths, and active bindings; then record one choice with <gsp> topology-select <id> --mode in-place|branch|worktree --quote "{user quote}". Block direct implementation on main/master or detached HEAD by default. Use feature/GSP-<number>[-Tnn]-<Chinese topic>; every GSP number must bind to a real branch or an explicit in-place override. GSP number, T task, branch, commit SHA, Issue, and PR are separate identities; leave Issue/PR empty when no real object exists. Git topology contract: use topology and topology-select; inspect main/master and every worktree; keep GSP 编号, T task, branch, commit SHA, control_root, workspace_root, and Issue/PR separate.

共享规则：[方法索引](../../references/methods.md)、[阶段转换](../../references/stage-transitions.md)、[跨会话](../../references/cross-session.md)、[共同契约](../../references/contract.md)。
