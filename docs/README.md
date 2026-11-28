# GstackSP 项目文档

这里说明公开项目的使用和维护入口。历史设计讨论、技能取舍和父本源码研究仅保存在开发工作区，不进入公开仓库或安装包。

## 公开文档

| 文档 | 用途 |
|---|---|
| [计划结构检查接口](reference/plan-quality.md) | 计划结构检查与验收边界 |
| [旧控制器恢复](reference/legacy-controller-recovery.md) | 无新版绑定的历史控制器显式恢复、拒绝条件与留痕 |
| [分支、worktree 与并行任务](reference/git-topology.md) | 选择矩阵、中文编号、父子任务、集成与恢复 |
| [构建上下文与独占缓存](reference/build-context.md) | 构建根、环境注入、兼容键、租约和恢复 |
| [有效证据选择与失败屏障](reference/evidence-selection.md) | tests/check 入口分组、适用失败、失效原因和错误码 |
| [单命令验证上下文与控制面开销](reference/validation-context.md) | 命令内去重、最终复核、性能预算和 `status --brief` |
| [命令执行进度、日志与恢复](reference/command-progress.md) | 流式日志、心跳、10 MiB 上限、`run-status` 与恢复边界 |
| [主会话协调的跨会话开发模式](reference/cross-session-development.md) | `cross-session` 的 controller/worker、总体计划、批量派发、自动 managed worktree、接受后清理、进度摘要、outbox 和文档命名 |
| [根 README](../README.md) | 安装范围、基本用法和项目说明 |

## 源码入口

- `skills/`：可安装技能定义。
- `scripts/`：安装、构建和校验工具。
- `gsp/`：运行时与状态处理代码。
- `tests/`：自动化测试。

## 测试入口

| 命令 | 用途 |
|---|---|
| `python -m unittest discover -s tests -v` | 串行基准，保留完整测试口径 |
| `python scripts/run_tests.py --jobs 4` | 按模块分片并行全量；单一汇总，失败分片单独回显 |
| `python scripts/run_tests.py --jobs 4 --tier fast` | 纯单元快测层，不含子进程/CLI 测试 |

`tests/tiers.json` 维护三件事：`fast` 快测清单、`serial` 不能与其它分片竞争的
锁/PID 模块（先单独运行）、`weights` 本机实测耗时（仅用于分片装箱均衡）。
并行入口不改变测试集合、统计口径或证据要求；串行 discover 始终可用。

收口时同一输入只跑一次最终回归（`run --task all`），随后做一次整体审查；只有测试入口或测试集合确实改变、或有已记录的抖动时，才重复运行并保留每次回执。

长命令可通过 `<gsp> run-status <id> --evidence <id>` 查看心跳、日志路径、截断标记和进程身份，不取得项目写锁。`run-status` 与 `<gsp> status <id> --brief` 都明确是非权威观测；交付判断仍使用有效证据集合、最终 `all` 回归、整体审查和 `ready` 门禁。

## 构建与临时产物

- `run` 为每次执行设置隔离的 TMP/TEMP/TMPDIR，并只回收本次新增的 `temp_roots` 条目；回执记录 `hygiene`。
- 可选 `.gsp/build-policy.json` 调整临时根、失败保留上限、构建预算和 profile 期望；缺省即使用内置默认。
- managed worktree 使用 `.gsp/build/<编号>/<slug>` 作为构建根，收尾时随 worktree 回收。
- 计划缺少“产物与临时文件生命周期”时 `plan-check` 报 `HYGIENE_SECTION_MISSING`（large 任务封计划失败）；未解释残留让 `ready` 报 `HYGIENE_LEFTOVER`。
- 收尾用 `topology-clean`（默认 dry-run）和 `close --cleanup` 收敛 worktree/分支；`close --reconcile` 归一已删除 workspace 的遗留状态。

任务记录、审计、研究和构建产物由各开发工作区本地保存，不随公开发布版本提供。

## 锁恢复

inspect-lock 只读显示 writer.lock 的 live/dead/unknown/malformed 状态；ecover-lock --evidence <报告> --reason <理由> 仅恢复已证明退出的 owner，并保存恢复备份。health --documents-only 不写默认报告目录，显式 --documents 才持久化审计。

