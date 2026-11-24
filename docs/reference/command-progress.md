# 命令执行进度、日志与恢复

`run` 的原生子进程路径由 `gsp/command_execution.py` 执行。测试适配器
`args.runner` 保留原有接口，但真实环境注入、流式日志和进程身份必须用
原生子进程路径验收，不能用 fake runner 冒充。

## 流式日志

- stdout/stderr 由两个读取线程持续排空，子进程未退出前就能读取已输出内容。
- UTF-8 使用增量解码器，跨读取块的字节不会变成替换字符。
- 已知 `redact` 模式在写入前统一过滤；argv、异常和进度 metadata 同样过滤，
  flags/env 只保留摘要，不写完整环境变量。
- 每行最多缓冲 64 KiB；单流最多写入 10 MiB。超过配额继续排空但停止写入，
  标记 `log_integrity.<stream>.truncated=true`；截断回执状态为
  `log-truncated`，不能完成任务。

## 心跳与终态

子进程启动后立即写入 `command-progress.json`，运行中每 2 秒更新
`heartbeat_at`、耗时和已写字节；安静子进程也有心跳。命令结束、超时、
中断或启动失败会原子写入 `command-result.json`：

- 正常退出：`status=completed` 和真实 `returncode`；
- 非零退出：`status=failed` 和真实 `returncode`，不能被当作完成；
- 超时：`status=timeout`、`launch_error=TimeoutExpired`，结果不能算通过；
- Ctrl+C：`status=interrupted`、`interruption=keyboard-interrupt`；
- 启动失败：`status=launch-failed` 和异常类型。

如果子进程已经启动但 `on_started` 钩子（例如构建租约附着）失败，执行器会终止
该子进程并把结果记录为 `failed`/`launch_error`；不会留下无人拥有的后台进程。
结果中的 `process_alive` 是终止后的复核值，不能代替对日志和回执的检查。

## 只读 `run-status`

`run-status <run> --evidence <id> [--wait 0..30]` 不取得项目写锁，可以在
`run` 持有写锁时读取。它校验 evidence id 属于该 run 的 inflight 或历史证据，
只读取 `.gsp/runs/<run>/evidence/<id>/`，不扫描其他目录。

返回 `terminal`、`status`、日志路径、字节数、截断标记、进程身份和父进程
存活状态。`validation=NOT_CHECKED`；这是运行观测，不是交付验证。父进程消失
而命令未终结时返回 `parent-lost`，不能当作成功或 `not-run`。

## 恢复边界

`recover-run` 仍要求先调查 inflight：已有完整回执可认领；否则必须提供本地
恢复报告。`run-status` 只帮助定位最后的日志、build_context 和进程身份，
不会自动终止同名进程、不会认领未知租约，也不会在 inflight 未判明前重跑。

`run` 的前后源扫描、构建租约和 `hygiene` 回收保持不变；流式执行不改变证据
指纹、测试计数或任务完成判定。
