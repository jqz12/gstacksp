# Codex 宿主适配与模型可见性

GSP 的 `model_route` 有四个独立状态，不能合并成一个“已接入”结论：

| 状态 | 含义 | 证据 |
| --- | --- | --- |
| `configured` | provider/profile 语义在宿主配置边界内 | 配置键名、provider 名称或继承规则 |
| `resolvable` | 指定 profile 和 model 能在本机目录/目录清单中解析 | profile 文件名、model catalog 标识 |
| `desktop_visible` | Codex Desktop 当前模型选择器已经显示该模型 | 必须由实际 Desktop 观察或宿主事件确认 |
| `request_verified` | worker 已经用该路由完成一次真实请求 | worker 收据和模型响应证据 |

## GSP worker 的推荐路径

主会话通过 `session-dispatch` 写入 provider、model profile、model 和 reasoning effort。Codex adapter 使用独立 profile 或 `CODEX_HOME`，生成显式的 `--profile`、`--model`、`-c model_reasoning_effort=...` 和 worktree 参数。适配器默认只生成启动计划，不自动执行付费请求。

profile 可以自带 `model_catalog_json`。宿主诊断会按路由中的 profile 读取该目录并用于 worker 可解析性判断；profile 专属目录不会因此被描述为普通 Desktop 会话已经显示。这样主配置可以保留官方模型目录，worker profile 则独立加载 OpenCode 目录。

这条路径不要求 OpenCode 模型出现在普通新会话的 UI 下拉框里。只要 Codex CLI 能解析启动参数，GSP worker 就可以按路由启动；模型选择器可见性仍单独报告。

## 普通新会话的刷新边界

如果用户刚修改了 `config.toml` 或模型目录，已经运行的 Codex Desktop 进程可能继续使用旧的模型目录快照。应先重新打开模型选择器；仍没有变化时完全退出并重新启动 Codex，再进行一次新会话观察。这个操作只刷新宿主缓存，不改变 GSP 任务路由。

如果重启后仍看不到模型，不能把 CLI worker 的可启动性描述成 Desktop 已显示。健康检查应保留 `desktop_visible=NOT_CHECKED`，并把问题交给宿主适配层调查。

## 失败处理

profile 缺失、model 不在 catalog、provider 不可用或宿主不支持指定参数时，worker 提交 `CHANGE_REQUEST`。禁止静默切换到主会话模型，也禁止把 API key、token 或秘密 endpoint 写进 GSP 文档、registry、prompt 或日志。
