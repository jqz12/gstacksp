# 项目代码质量契约

code-quality 条件方法负责“项目质量门禁”，与其他阶段和方法分工如下：

| 能力 | 负责什么 | 不负责什么 |
| --- | --- | --- |
| `gsp-health` | 环境、安装、文档结构和 Git 路由 | 代码质量打分或质量门禁 |
| code-quality 方法（owner: `gsp-execute`） | 声明式构建、类型、格式、Lint、测试、安全和维护性检查 | 自动修复、代码审查、业务验收 |
| `gsp-review` | 当前任务的规格、差异、逻辑和安全审查 | 全项目质量基线 |
| 测试审查方法 | 测试价值、重复覆盖和行为缺口 | 代替测试执行或静态质量工具 |
| QA 方法（owner: `gsp-execute`） | API、CLI、UI 和运行时行为 | 静态源码质量 |
| verify 方法（owner: `gsp-review`） | 交付前证据完整性和新鲜度 | 重新解释失败命令 |

## 质量栈声明

项目根目录的 `.gsp/quality.json` 是机器读取的唯一执行配置。它只允许项目内的相对工作目录和 argv 数组，不接受一整段 shell 字符串。最小示例：

```json
{
  "schema": 1,
  "checks": [
    {
      "id": "python.tests",
      "label": "Python tests",
      "kind": "test",
      "argv": ["python", "-m", "pytest", "-q"],
      "cwd": ".",
      "modes": ["change", "baseline", "release"],
      "required": true,
      "timeout_s": 600
    }
  ]
}
```

`kind` 使用 `build`、`format`、`lint`、`test`、`security`、`maintainability`、`contract` 或 `runtime`。`modes` 决定检查在哪些场景运行；`required=false` 的失败只能形成 `WARN`，不能把整次门禁判为通过的证据。命令缺失或无法启动是 `BLOCKED`/`NOT_CHECKED`，不是 PASS。

没有 `.gsp/quality.json` 时，运行器只根据 `Cargo.toml`、`pyproject.toml`、`package.json`、`go.mod` 等文件列出候选命令，绝不自动执行。项目应在确认工具、工作目录和耗时后把候选转换为声明式配置。

## 三种模式

- `change`：完成一个功能、模块或修复后，运行受影响的最小质量栈；通过后进入 `gsp-review`。
- `baseline`：老项目接入、迁移或首次建立质量基线；失败可作为已知基线，但必须由用户决定是否接受，不能直接变成交付通过。
- `release`：交付前的完整质量门禁；通过后由 `gsp-review` 完成 verify，再由 `gsp-ship` 准备交付。

输出目录固定为：

```text
.gsp/runs/<任务ID>/quality/<质量运行ID>/
├── report.json
├── report.md
├── commands.jsonl
└── evidence/<检查ID>/
    ├── stdout.txt
    ├── stderr.txt
    └── receipt.json
```

输出经过敏感字段脱敏并有大小上限。质量报告不替代 `.gsp/runs/<任务ID>/evidence/` 的任务测试收据；任务要进入 `ready`，仍需按正常流程完成最终回归、整体审查和 verify 方法。
