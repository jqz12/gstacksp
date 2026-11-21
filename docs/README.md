# GstackSP 项目文档

这里说明公开项目的使用和维护入口。历史设计讨论、技能取舍和父本源码研究仅保存在开发工作区，不进入公开仓库或安装包。

## 公开文档

| 文档 | 用途 |
|---|---|
| [计划结构检查接口](reference/plan-quality.md) | 计划结构检查与验收边界 |
| [分支、worktree 与并行任务](reference/git-topology.md) | 选择矩阵、中文编号、父子任务、集成与恢复 |
| [主会话协调的跨会话开发模式](reference/cross-session-development.md) | `cross-session` 的 controller/worker、总体计划、批量派发、自动 managed worktree、接受后清理、进度摘要、outbox 和文档命名 |
| [模型路由](reference/model-routing.md) | provider/profile/model/effort 参数、只读 route 诊断、非秘密 launch plan、启动收据和四层状态边界 |
| [Codex 宿主适配](reference/codex-host-adapter.md) | `codex exec` 显式 provider、profile catalog、Desktop 刷新、provider 冲突、事务性重派和启动收据状态机 |
| [根 README](../README.md) | 安装范围、基本用法和项目说明 |

## 源码入口

- `skills/`：可安装技能定义。
- `scripts/`：安装、构建和校验工具。
- `gsp/`：运行时与状态处理代码。
- `tests/`：自动化测试。

任务记录、审计、研究和构建产物由各开发工作区本地保存，不随公开发布版本提供。

## 锁恢复

`inspect-lock` 只读显示 `writer.lock` 的 live/dead/unknown/malformed 状态；`recover-lock --evidence <报告> --reason <理由>` 仅恢复已证明退出的 owner，并保存恢复备份。`health --documents-only` 不写默认报告目录，显式 `--documents` 才持久化审计。
