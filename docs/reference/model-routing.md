# 模型路由

GSP 把模型偏好作为任务路由保存，而不是把它混入 prompt 的自然语言描述。路由包含 `provider`、`model_profile`、`model` 和 `reasoning_effort` 四个非敏感字段；凭据仍由宿主配置拥有，不能进入 dispatcher 的输出。

## 发布入口

`session-dispatch` 的 `--provider`、`--model-profile`、`--model` 和 `--reasoning-effort` 都支持统一值或 `TASK=VALUE`。未提供任何路由字段时，普通 dispatch 保持旧行为。提供路由时：

- provider、profile 和 model 会一起解析，`provider=inherit` 也不会跳过解析；
- 未知 provider、格式错误、profile/provider 冲突或 profile/model 无法解析时明确失败；
- Codex 目标会校验 CLI 参数能力并生成 `launch_plan`，其中显式包含 `-c model_provider="<provider>"`；
- 非 Codex 目标仍把同一份路由写入 prompt，由对应平台执行或人工转发；
- 失败不会静默换成主会话模型。

字段、诊断命令、重派事务和启动收据的完整契约见 [Codex 宿主适配](codex-host-adapter.md)。

## 发布前检查

`model-route-health` 用于解析单条 route，`host-adapter-status` 用于核对宿主能力与 profile catalog。两者严格只读，返回结果不会写项目或宿主配置。

```powershell
<gsp> model-route-health `
  --provider magpie `
  --model-profile magpie `
  --model opencode-go/deepseek-v4.1-flash `
  --reasoning-effort max

<gsp> host-adapter-status
```

如果所选 profile 已经声明了不同的 `model_provider`，结果为 `profile_provider_mismatch`。这不是可自动修复的普通技术差异；controller 应把 task 视为 `CHANGE_REQUEST`，明确修正 provider、profile 或宿主配置后再发布。

## 四层证据

`configured`、`resolvable`、`desktop_visible` 和 `request_verified` 必须分别记录。配置文件或 profile catalog 中存在模型标识，只能证明配置与解析状态；不能证明 Desktop 选择器已经刷新，也不能证明真实请求已经完成。catalog 命中不会自动设置 `desktop_visible`，只有显式 Desktop/宿主观察证据才能给出 true。

修改模型目录后先重新打开选择器，必要时完全重启 Codex。Desktop 观察、线程启动和真实模型请求是三种不同证据：

1. `launch_plan` 只证明启动参数已生成。
2. `session-launch-receipt --status started` 证明 controller 实际观察到了线程、进程或投递。
3. `session-launch-receipt --status request-verified` 还需要真实模型响应或等价请求证据。

没有执行真实请求时，`request_verified=false` 与 `status=NOT_CHECKED` 必须保留。`started` 和 `request-verified` 都要求非空 evidence，终态 dispatch 不能再回退为启动中。
