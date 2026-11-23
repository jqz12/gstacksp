# GstackSP 项目文档

这里说明公开项目的使用和维护入口。历史设计讨论、技能取舍和父本源码研究仅保存在开发工作区，不进入公开仓库或安装包。

## 公开文档

| 文档 | 用途 |
|---|---|
| [计划结构检查接口](reference/plan-quality.md) | 计划结构检查与验收边界 |
| [分支、worktree 与并行任务](reference/git-topology.md) | 选择矩阵、中文编号、父子任务、集成与恢复 |
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

任务记录、审计、研究和构建产物由各开发工作区本地保存，不随公开发布版本提供。

## 锁恢复

inspect-lock 只读显示 writer.lock 的 live/dead/unknown/malformed 状态；ecover-lock --evidence <报告> --reason <理由> 仅恢复已证明退出的 owner，并保存恢复备份。health --documents-only 不写默认报告目录，显式 --documents 才持久化审计。

