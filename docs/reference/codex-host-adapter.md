# Codex 宿主适配与模型可见性

GSP 的 `model_route` 有四个独立状态，不能合并成一个“已接入”结论：

| 状态 | 含义 | 最低证据 |
| --- | --- | --- |
| `configured` | route 指定的 provider 在主配置或所选 profile 中定义 | provider 名称与 profile 的继承关系 |
| `resolvable` | 指定 profile 和 model 能在本机目录或 profile catalog 中解析 | profile 文件名、catalog 中的 model ID |
| `desktop_visible` | Codex Desktop 当前模型选择器已经显示该模型 | 实际 UI 观察或宿主明确给出的事件 |
| `request_verified` | worker 已经用该路由完成一次真实请求 | 启动收据与模型响应证据 |

配置和 catalog 命中只说明 CLI 有机会启动。它们既不证明 Desktop 选择器已经刷新，也不证明真实模型请求成功。

## 只读诊断入口

`model-route-health` 和 `host-adapter-status` 都是只读命令。前者返回 route 的 `configured`、`resolvable`、`desktop_visible`、`request_verified` 与原因；后者返回不含秘密值的宿主能力、profile catalog 和 provider 摘要。两条命令不会改写 registry、dispatch、文档或宿主配置。

```powershell
<gsp> model-route-health `
  --provider magpie `
  --model-profile magpie `
  --model opencode-go/deepseek-v4.1-flash `
  --reasoning-effort max

<gsp> host-adapter-status
```

只有已经实际看到 Desktop 模型选择器中的某个模型时，才把它作为显式观察传入：

```powershell
<gsp> model-route-health `
  --provider magpie `
  --model opencode-go/deepseek-v4.1-flash `
  --desktop-visible-model opencode-go/deepseek-v4.1-flash
```

省略 `--desktop-visible-model` 时，`desktop_visible` 必须保持 false；catalog 命中不能替代 UI 观察。

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

dispatch 会先规范化非敏感的 `model_route`，再读取宿主帮助、profile、provider 和 catalog，生成 `launch_plan`。路由和启动计划同时写入 `.gsp/runs/<run>/sessions/dispatch.json`、任务 registry 和 `sessions/T01-prompt.md`，使本地人工平台与 Codex 自动化看到同一份任务契约。

Codex 启动计划使用非交互模式，并显式覆盖 provider：

```text
codex exec
  -c model_provider="<provider>"
  --profile <profile>
  --model <model>
  -c model_reasoning_effort="<effort>"
  -C <worktree>
  -
```

prompt 通过 stdin 传入，避免 Windows 命令行长度限制。`launch_plan` 保存 `argv`、非秘密 `env`、`stdin`、`started=false`、`request_verified=false` 和 `status=NOT_CHECKED`。生成计划不等于线程已创建，也不等于模型请求成功。

宿主能力在发布时检查。缺少 `exec`、`--profile`、`--model`、`-c/--config` 或工作目录参数支持时，dispatch 返回 `DISPATCH_MODEL_ROUTE_UNSUPPORTED`，不会静默退回主会话模型。`provider=inherit` 可以用于明确选择继承，但它仍参与 profile/model 解析，不是跳过校验的快捷方式。

## profile 与 provider 冲突

profile 可以自带 `model_provider` 和 `model_catalog_json`。当 route 明确给出另一个 provider，而所选 profile 已声明不同 provider 时，health 和 launcher 都返回 `profile_provider_mismatch`。dispatch 应把它转成 `CHANGE_REQUEST`，由主会话修正 route、profile 或配置；不得先悄悄启动再靠 prompt 提醒 worker。

profile 专属 catalog 不会因此被描述为普通 Desktop 会话已经显示。只要 Codex CLI 能按显式参数解析，GSP worker 才可能启动；模型选择器可见性仍单独报告。

## 启动收据状态机

宿主创建线程或发送 prompt 后，controller 用 `session-launch-receipt` 记录自己实际观察到的事实，而不是修改 `launch_plan`：

```powershell
<gsp> session-launch-receipt <run> `
  --task T01 `
  --dispatch D001 `
  --session S01 `
  --status started `
  --reason "Codex thread created and prompt sent" `
  --evidence thread:<thread-id>
```

允许的状态转换是：

| 当前 dispatch | 可记录的新收据 |
| --- | --- |
| `adapter-ready` | `blocked`、`started` |
| `running` | `blocked`、`started`、`request-verified` |
| `blocked` | `blocked`、`started` |

`started` 和 `request-verified` 必须至少有一个非空 `--evidence` 标识；证据与 reason 会脱敏。`started` 记录线程、进程或投递事实，并把 dispatch 标为 `running`；`request-verified` 必须建立在已经启动的 dispatch 上，只有真实 worker 响应或请求日志才能提供该证据。

`blocked` 会把 dispatch 标为阻塞，但不会抹掉此前已经发生的启动或请求验证事实。之后若排除了阻塞，可以从 `blocked` 记录新的 `started`。`reported`、`accepted`、`integrated`、`cleaned`、`superseded` 是终态，不能再追加启动收据。每次转换保留在 `launch_receipts` 历史中，最新一条位于 `launch_receipt`。

静态 catalog、CLI 帮助、launch plan 和线程创建都不能证明真实请求成功；`request_verified` 只来自真实模型响应或等价请求日志。

## 重派事务与归档

重复发布同一活动任务时必须显式使用 `--retry`。重派先完成所有依赖、worktree、路径和模型路由校验，再把旧 prompt 与 brief 复制到 `sessions/history/`。当前交付文件在新文件原子发布前保持不动；如果后续校验或发布失败，会恢复被复制版本的当前字节，旧 dispatch 仍为 active，不会出现“旧文件已移走、新 dispatch 又没生成”的半完成状态。

只有新批次成功发布后，同一任务的全部旧 active dispatch 才会标记为 `superseded`，包括此前位于其他 worktree 的重试记录。与该 dispatch 对应、仍处于活动或已报告状态的 worker 会话也会标为 `superseded`，释放其写租约并把未消费的旧 outbox 事件标成不可投递；当前 task 投影会重建为新的 dispatch，旧映射保存在 `previous_dispatches`。因此 `session-watch` 不会把旧 `DONE` 报告交给 controller，同一 worktree 也可以由重派 worker 重新取得租约。

历史 dispatch、历史 prompt/brief 和提交不会被删除。不同任务仍不能共享同一个可写 worktree；`--retry` 不会放宽这一限制。旧外部进程、Shell、MCP 或外部编辑器若已经绕过编辑前 hook，仍只能报告为 `NOT_CHECKED`；GSP 能撤销已登记租约和回传资格，但不能保证终止宿主已经启动的进程。

## 普通新会话的刷新边界

如果用户刚修改了 `config.toml` 或模型目录，已经运行的 Codex Desktop 可能继续使用旧快照。应先重新打开模型选择器；仍无变化时完全退出并重新启动 Codex，再进行一次新会话观察。这个操作只刷新宿主缓存，不改变 GSP 任务路由。

如果重启后仍看不到模型，不能把 CLI worker 的可启动性描述成 Desktop 已显示。健康检查应保留 `desktop_visible=NOT_CHECKED` 或 false，并把问题交给宿主适配层调查。

## 安全与失败处理

profile 缺失、model 不在 catalog、provider 不可用、profile/provider 冲突或宿主不支持指定参数时，worker 提交 `CHANGE_REQUEST`。禁止静默切换到主会话模型，也禁止把 API key、token 或秘密 endpoint 写进 GSP 文档、registry、prompt 或日志。

真实付费请求默认不自动触发。需要取得 `request-verified` 收据时，必须使用明确选择的测试任务、预算和证据范围；静态检查与 launch plan 都不能替代该请求的证据。
