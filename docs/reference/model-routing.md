# 模型路由

GSP 把模型偏好作为任务路由保存，而不是把它混入 prompt 的自然语言描述。路由包含 `provider`、`model_profile`、`model` 和 `reasoning_effort` 四个非敏感字段；凭据仍由宿主配置拥有，不能进入 dispatcher 的输出。

## 发布入口

`session-dispatch` 的 `--provider`、`--model-profile`、`--model` 和 `--reasoning-effort` 都支持统一值或 `TASK=VALUE`。未提供任何路由字段时，普通 dispatch 保持旧行为。提供路由时：

- 未知 provider、格式错误或 profile/model 无法解析会明确失败；
- Codex 目标会校验 CLI 参数能力并生成 `launch_plan`；
- 非 Codex 目标仍把同一份路由写入 prompt，由对应平台执行或人工转发；
- 失败不会静默换成主会话模型。

字段和启动计划的完整契约见 [Codex 宿主适配](codex-host-adapter.md)。

## 四层证据

`configured`、`resolvable`、`desktop_visible` 和 `request_verified` 必须分别记录。配置文件或 profile catalog 中存在模型标识，只能证明配置与解析状态；不能证明 Desktop 选择器已经刷新，也不能证明真实请求已经完成。

修改模型目录后先重新打开选择器，必要时完全重启 Codex。Desktop 观察、CLI 启动计划和真实模型请求是三种不同证据，不能相互替代。没有执行真实请求时，`request_verified=false` 与 `status=NOT_CHECKED` 必须保留。
