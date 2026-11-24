# 构建上下文与独占缓存

`run` 在启动真实命令前解析一次构建上下文，并把实际生效的 `CARGO_TARGET_DIR`、`BUILD_ROOT` 注入子进程。只写进 prompt 或 dispatch 记录不算生效。实现正本为 `gsp/build_context.py`，回执字段为 `build_context`。

## 模式

在 `.gsp/build-policy.json` 中配置：

```json
{
  "worktree_build": "managed",
  "build_cache": {
    "mode": "isolated",
    "root": null,
    "context": {}
  }
}
```

| 模式 | 行为 | 约束 |
|---|---|---|
| `isolated` | 使用本任务或 dispatch 已有的 owned 构建根 | 默认；仅对 Cargo 项目注入，非 Cargo 保持无注入 |
| `reuse-exclusive` | 显式复用指定热根 | 必须是绝对路径；必须提供匹配的 `compatibility_key`；同一时刻只允许一个写者 |
| `off` | 不注入构建变量 | `worktree_build=off` 同样生效 |

`reuse-exclusive` 的 `context.compatibility_key` 覆盖 `Cargo.lock`、工具链文件/环境、平台及构建 flags 的摘要。缺少根、缺少 context 或摘要不匹配分别报 `BUILD_CONTEXT_INCOMPLETE`、`BUILD_CONTEXT_INCOMPATIBLE`。兼容键相同不承诺 Cargo 实际命中缓存。

默认 `isolated` 只在工作区存在 `Cargo.toml`，或命令本身是 `cargo` 时注入。非 Cargo 项目返回 `mode=off`、`root=null`、`env={}`，不会继承外层 GSP 的构建变量作为新绑定；显式 `reuse-exclusive` 仍按策略注入。

## 环境合并

子进程环境按以下顺序合并：

1. 宿主普通环境；
2. 已绑定的构建白名单变量；
3. 本次运行的 `TMP`、`TEMP`、`TMPDIR` 隔离目录。

`isolated` 模式发现宿主已有不同 `CARGO_TARGET_DIR`/`BUILD_ROOT` 时拒绝启动，返回 `BUILD_CONTEXT_CONFLICT`，不静默换根。`reuse-exclusive` 是显式决策，可在回执中标记覆盖来源。回执只保存白名单变量、路径、来源和兼容键摘要，不保存完整进程环境。

## 租约

实际构建根内使用原子排他的 `.gsp-build-lease.json` 协调同机 GSP 写者，跨控制根生效。租约记录控制进程和实际构建子进程身份、阶段、证据 ID 和兼容键。

- 控制进程仍存活：第二个写者报 `BUILD_LEASE_HELD`。
- 控制进程已退出但子进程仍活，或启动窗口结果未知：报 `BUILD_LEASE_UNKNOWN`，保留现场等待人工取证，不自动抢锁或杀进程。
- 命令正常结束后只删除本次取得且子进程已结束的租约；外部根和构建产物不随租约释放自动删除。

该租约是 GSP 参与者之间的协作保证，不是文件系统沙箱，不能约束绕过 GSP 启动的编译器或自行 daemonize 的写者。`token=unknown` 不构成“确认死亡/拥有”。

## 记录与恢复

回执的 `build_context` 包含 `mode`、`root`、`env`、`provenance`、`compatibility_key` 和 owner；`hygiene` 同时记录 `build_mode` 与 `build_root`。失败或恢复重跑沿用同一绑定根；若要换根，必须显式修改策略并作废相关证据。请求失败不会自动回落到 worktree target 或宿主 target。
