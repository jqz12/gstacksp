# GSP 模型路由与宿主适配

GSP 的模型路由是跨会话任务的编排元数据。它解决“主会话使用高质量模型、worker 使用便宜模型”的调度表达问题，实际模型请求仍由宿主负责。

## 推荐分工

- **controller 主会话**：继续使用当前 Codex/ChatGPT provider，维护唯一总体 `spec.md`、`plan.md`、依赖、验收和最终裁决。
- **Codex worker**：由宿主 adapter 创建独立线程；可使用独立 `CODEX_HOME` 或 profile，把 provider 指向本机 Magpie，再选择 OpenCode Go 模型。
- **ZCode/其他平台 worker**：仍是 `manual-required`，主会话把同一份 prompt 转发给目标平台；worker 在指定 worktree 中执行并用 `session-report` 回传。

GSP 只在 `dispatch.json`、session registry、brief 和 prompt 中保存以下非敏感字段：`provider`、`model_profile`、`model`、`reasoning_effort`。API key、token、完整秘密 endpoint 不进入 GSP 文件。

## 派发示例

```powershell
<gsp> session-dispatch <run> --session S01 `
  --task T01 --task T02 `
  --target T01=codex --target T02=zcode `
  --path T01=src/data --path T02=src/api `
  --provider magpie `
  --model-profile worker-low-cost `
  --model opencode-go/qwen4-max `
  --reasoning-effort low `
  --model-assignment T02=opencode-go/deepseek-v4.1-flash `
  --worktree-mode auto
```

按任务的 assignment 优先于批次默认值。没有指定任何路由时，记录 `provider=inherit`，普通单会话和旧跨会话调用保持原样。

## Magpie/OpenCode Go 边界

建议在 worker 专用 profile 中配置 Magpie endpoint 和 OpenCode Go 登录状态，主会话 profile 保持原生 Codex 配置。GSP 不负责启动 Magpie、不复制登录信息、不修改 Clash，也不把 HTTP endpoint 可达性当成模型调用成功。worker 如果无法使用指定 provider 或模型，应提交 `CHANGE_REQUEST`，不能静默切换。

## 证据和状态

`session-progress` 的任务进度仍按 accepted task 计算；模型路由只用于说明“由谁执行”。`session-report`、`session-watch`、`session-accept` 和 worktree cleanup 的生命周期不因模型变化而改变。真实 Codex adapter 创建、Magpie 请求、OpenCode Go 计费和跨平台网络仍需在实际宿主单独验证，GSP 报告为 `NOT_CHECKED` 直到有对应收据。
