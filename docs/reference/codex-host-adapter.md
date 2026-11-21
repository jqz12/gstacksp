# Codex 宿主适配与模型可见性

GSP 的 `model_route` 有四个独立状态，不能合并成一个“已接入”结论：

| 状态 | 含义 | 证据 |
| --- | --- | --- |
| `configured` | route 指定的 provider 在主配置或所选 profile 中定义 | provider 名称和 profile 叠加/继承规则 |
| `resolvable` | 指定 profile 和 model 能在本机目录/目录清单中解析 | profile 文件名、model catalog 标识 |
| `desktop_visible` | Codex Desktop 当前模型选择器已经显示该模型 | 必须由实际 Desktop 观察或宿主事件确认 |
| `request_verified` | worker 已经用该路由完成一次真实请求 | worker 收据和模型响应证据 |

## session-dispatch 接线

主会话发布 Codex worker 时，用以下参数声明模型路由。每个参数都接受统一值或 `TASK=VALUE`：

```powershell
<gsp> session-dispatch <run> --session S01 `
  --task T01 `
  --target T01=codex `
  --path T01=src/module `
  --provider T01=magpie `
  --model-profile T01=magpie `
  --model T01=opencode-go/deepseek-v4.1-flash `
  --reasoning-effort T01=max
```

dispatch 会先把路由规范化为非敏感的 `model_route`，再读取宿主帮助、profile、provider 名称和 catalog，生成 `launch_plan`。路由和启动计划同时写入 `.gsp/runs/<run>/sessions/dispatch.json`、任务 registry 和 `sessions/T01-prompt.md`，因此本地人工平台与 Codex 自动化看到的是同一份任务契约。

Codex 启动计划使用非交互模式：

```text
codex exec
  --profile <profile>
  --model <model>
  -c model_reasoning_effort="<effort>"
  -C <worktree>
  -
```

prompt 通过 stdin 传入，避免 Windows 命令行长度限制。`launch_plan` 保存 `argv`、非秘密 `env`、`stdin`、`started=false`、`request_verified=false` 和 `status=NOT_CHECKED`。主会话根据计划创建或发送任务；仅生成计划不等于已经启动线程，更不等于模型请求成功。

宿主能力在发布时检查。缺少 `exec`、`--profile`、`--model` 或 `-c/--config` 支持时，dispatch 返回 `DISPATCH_MODEL_ROUTE_UNSUPPORTED`，不会静默退回主会话模型。reasoning effort 通过 Codex 文档化的配置覆盖传递，不伪造第三方模型的默认能力。

## profile catalog 的解析边界

profile 可以自带 `model_catalog_json`。宿主诊断会读取路由指定 profile 的目录并用于 worker 可解析性判断；provider 可以定义在 profile 中，也可以从主配置继承。profile 专属目录不会因此被描述为普通 Desktop 会话已经显示。

这条路径不要求 OpenCode 模型出现在普通新会话的 UI 下拉框里。只要 Codex CLI 能解析启动参数，GSP worker 就可以按路由启动；模型选择器可见性仍单独报告。

宿主诊断不会仅凭 catalog 命中就把 `desktop_visible` 设为 true。只有调用方提供显式的 `desktop_visible_models` Desktop/宿主观察证据时，`route_health` 才返回 true；否则即使模型可解析，也保持 false，并继续要求真实 UI 或宿主事件确认。

## 普通新会话的刷新边界

如果用户刚修改了 `config.toml` 或模型目录，已经运行的 Codex Desktop 进程可能继续使用旧的模型目录快照。应先重新打开模型选择器；仍没有变化时完全退出并重新启动 Codex，再进行一次新会话观察。这个操作只刷新宿主缓存，不改变 GSP 任务路由。

如果重启后仍看不到模型，不能把 CLI worker 的可启动性描述成 Desktop 已显示。健康检查应保留 `desktop_visible=NOT_CHECKED`，并把问题交给宿主适配层调查。

## 安全与失败处理

profile 缺失、model 不在 catalog、provider 不可用或宿主不支持指定参数时，worker 提交 `CHANGE_REQUEST`。禁止静默切换到主会话模型，也禁止把 API key、token 或秘密 endpoint 写进 GSP 文档、registry、prompt 或日志。

真实付费请求默认不自动触发。需要验证 `request_verified` 时，必须使用明确选择的测试任务、预算和证据范围；静态目录、CLI 帮助与 launch plan 都不能替代该请求的收据。
