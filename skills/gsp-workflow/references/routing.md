# GSP 路由契约

这是 GSP 技能之间的衔接契约。它描述“在什么已观测状态下建议哪个技能”，不替代任何 SP 技能正文，也不授予外部动作权限。随安装包提供的机器正本是 `gsp/routing.py`；`scripts/routing_registry.py` 只是源仓库的校验和兼容 CLI；本页用于解释和审查。

## 统一字段

每条路由都有 `route_id`、`source`、`condition`、`next_skill`、`return_to`、`blocking`、`user_decision_required`、`decision_kind`、`evidence_kind` 和 `unknown_fallback`。未配对的事件保留 `unknown`，统一回到 `gsp-workflow` 调查。

## 主路径

```text
开始/恢复
  ├─ 活动任务不匹配 → gsp-context-restore → gsp-workflow
  ├─ 没有活动任务 → gsp-brainstorming
  ├─ Git 脏或未跟踪 → gsp-git（worktree 决策）→ gsp-workflow
  ├─ Git detached → gsp-git（阻塞的 branch 决策）→ gsp-workflow
  ├─ Git 无仓库或未知 → gsp-git（阻塞调查）→ gsp-workflow
  └─ Git 干净且有活动任务 → gsp-workflow 按任务状态继续
        ├─ 设计未确认 → gsp-brainstorming
        ├─ 设计已确认、计划未封存 → gsp-writing-plans
        ├─ 计划已封存、任务未完成 → gsp-execute
        ├─ 代码质量必检失败 → gsp-execute → gsp-code-quality
        ├─ 代码质量 change 通过 → gsp-review
        ├─ 代码质量 baseline 通过 → gsp-workflow（等待基线决策）
        ├─ 代码质量 release 通过 → gsp-verify
        ├─ 测试失败 → gsp-systematic-debugging → gsp-execute
        ├─ 审查阻塞 → gsp-execute → gsp-review
        ├─ 证据过期 → gsp-verify
        └─ 全部任务完成且最终 all 回归通过 → gsp-review → gsp-verify/ready → gsp-ship
```

## 条件规则

- `gsp-health` 只读检查，按需运行；AI 优先消费其短 `route`/`handoff`，不把完整报告塞进上下文。
- Git 路由的运行时字段直接来自 `gsp/routing.py`：脏工作区是 `git.dirty`，detached HEAD 是 `git.detached`，两者不能合并成同一条路由。
- `gsp-git` 处理工作区归属、分支、提交准备和 observe-head；完成后回到 `gsp-workflow`。
- `gsp-document-governance` 只在有治理授权时接收文档发现；只读检查停在报告并记录一次决策。
- `gsp-systematic-debugging` 只在真实测试/命令失败时进入；修复后回到原执行阶段。
- `gsp-review` 发现阻塞项时回到执行；通过后进入 `gsp-verify`。
- `gsp-code-quality` 是声明式质量栈的执行入口：只运行项目在 `.gsp/quality.json` 明确登记的 argv，不扫描构建缓存、不猜测工具、不自动修复。`change` 用于任务改动后的快速门禁，`baseline` 用于老项目接入或建立基线，`release` 用于交付前完整门禁。
- 质量报告中的 `FAIL` 表示已执行的必需检查失败，`BLOCKED` 表示必需检查无法启动或质量栈缺失；具体去向以 `gsp/routing.py` 的 `quality.failure` 和 `quality.not-checked` 为准。没有声明配置时只给候选命令并标记 `NOT_CHECKED`，不能把工具存在或文件特征当成通过。
- `gsp-verify` 发现证据过期时重新验证，不修改旧收据。
- `gsp-ship` 准备交付，不自动启动另一个需求；推送、合并、发布和删除分支需要各自精确授权。

## 观测语义

`declared` 表示回复中声明使用；`observed` 表示有 CLI、hook 或宿主事件；`inferred` 表示根据状态推断；`unknown` 表示没有足够证据。技能文件存在、技能列表可见和技能真正执行是三种不同事实。
