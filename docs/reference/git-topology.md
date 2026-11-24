# 分支、worktree 与并行任务

新任务先盘点 Git，再选择工作位置。`topology <运行ID>` 只读展示当前分支、HEAD、本地分支、全部 worktree、脏路径和任务绑定，并给出推荐。AI 应把这些信息整理成 `位置 | 分支 | HEAD | 脏状态 | 任务/写者 | 风险` 短表，说明预计创建的位置，再记录一次用户选择。恢复同一任务时沿用已有选择，现场变化时说明具体差异。

## 选择规则

分支保存一条提交历史；worktree 提供一个独立源码目录。一个目录同时只能检出一个分支，因此同会话同时写两个分支也需要两个 worktree。

| 情况 | 推荐 | 约束 |
|---|---|---|
| 单会话、一个小任务、当前干净且分支已归属本任务 | 原地复用目录 | 需要用户明确选择；不机械创建 worktree |
| 大功能、跨模块修改，但串行且无独立验证需求 | 独立分支，复用干净目录 | `--size large` 只提示风险，不单独强制 worktree |
| 需要独立验证 | 独立 worktree | 建立任务时声明 `--independent-verification` |
| 需要保护现有改动 | 从明确基线建立 worktree | 声明 `--protect-existing`；不迁移、stash 或覆盖现场 |
| 多个串行任务 | 每个工作项独立分支，依次复用目录 | 先关闭前任务、释放绑定，再换任务 |
| 多会话分别开发不同任务 | 每个可写任务独立分支和 worktree | 会话名称不能代替隔离 |
| 同一任务换会话继续 | 复核并复用已有工作区 | 不因换会话制造子任务或集成任务 |
| 多 agent 同时修改源码 | 每个可写子任务独立 worktree | 只读分析者可共享；父账本由协调者写入 |
| 前后端大功能 | 同一 GSP 号下拆分前端、后端、集成任务 | 先确认接口契约，最后在集成工作区验收 |
| 存在未归属改动 | 保留现场，从明确基线建立 worktree | 不自动 stash、reset、clean 或迁移改动 |
| main/master | 推荐新分支 | 只有用户明确选择原地并记录理由才允许 |
| detached HEAD | 建立命名分支或 worktree | 不允许原地实现 |

当前 intent 支持 `single-task`、`serial-multi-task`、`parallel-multi-task`、`cross-session`、`full-stack`。AI 必须结合范围声明 intent、`--size small|large`，并在需要时声明 `--independent-verification` 或 `--protect-existing`。工作位置与构建缓存是两个独立决定：即使使用 worktree，也不默认让多个写者共用同一个可写 target。`cross-session` 只有一个可写任务时不要求额外集成任务；真正并行或全栈才需要子工作区图。

推荐优先级：脏工作区、独立验证或保护现有改动优先选择 worktree；并行、跨会话和全栈协作需要独立 worktree；main/master/detached HEAD 先建立任务分支；干净且已归属本任务的串行分支可以原地继续。规模大只增加提示，不单独把原地改成 worktree。最终仍由用户明确选择并记录原话。

`cross-session` 还需要任务级 session registry。主会话/controller 维护唯一的 `plan.md` 和公共裁决；子会话/worker 读取 brief、在自己的 worktree 中实现并回传 report。`Snn` 是会话身份，`Tnn` 是计划任务身份。controller lease、同一 worktree 的 write lease 和 `claimed_paths` 共同阻止第二个会话在同一 room 中覆盖已提交文件；已接线的 direct-edit hook 会在 Edit/Write/apply_patch 前拒绝未登记或未声明的路径。完整文档见[主会话协调的跨会话开发模式](cross-session-development.md)。

## 编号和中文命名

| 标识 | 作用 |
|---|---|
| `GSP-000018` | 项目内稳定工作项号；标题或分支变化不重编号 |
| `T01`、`T02` | 当前计划内的子任务；`T1` 与 `T01` 视为同一个数字身份 |
| `feature/GSP-000018-T02-前端界面` | 任务分支；子分支必须携带自己的父号和 T 号 |
| Commit SHA | 实际提交后观察记录；不能拿分支名替代 |
| Issue、PR | 可选外部关联；本地工作没有就留空 |

人读主题默认中文，分支类型使用 `feature/`、`fix/`、`docs/`、`test/`、`refactor/`、`chore/`、`research/`。主题不含空格、额外斜杠或 Git 非法字符；自动生成名会保留编号并截短主题。宿主不接受 Unicode 时需说明原因、显式选择 ASCII 名，不能静默改名。提交示例：`feat(GSP-000018): 完成后端接口`，正文记录 `GSP: GSP-000018` 和 `Task: T01`。

## 绑定、执行与恢复

以下 `<gsp>` 表示 `python <源码或安装目录>/scripts/gsp.py --project <控制根>`。示例中的 quote 必须替换为实际用户选择，命令文本本身不构成授权。

```text
<gsp> topology feature-work
<gsp> topology-select feature-work --mode worktree --quote "实际选择"
<gsp> topology-select feature-work --task T01 --mode worktree --base main --quote "实际子任务选择"
<gsp> observe-head feature-work --task T01 --reason "实际提交号、范围和核对结果"
<gsp> topology-select feature-work --mode worktree --rebind --branch feature/GSP-000018-恢复开发 --quote "核实后的重新绑定决定"
```

`--base` 接受实际存在的提交或分支，记录解析后的 SHA；显式分支记录其真实名称，纯 SHA 不猜测来源分支。分支可以从功能分支继续派生，但 Git 不维护“父分支树”，依赖仍由任务和 base SHA 记录。默认工作区在 `<控制根>/.gsp/worktrees/<分支名>`；自定义路径必须在控制根内并已被 Git 忽略。已有同任务绑定可以复用，换位置、分支或基线须显式 `--rebind`，并使旧验收失效。不会自动删除旧工作区。

状态、规格、计划、收据和锁只保存在 `control_root`；命令、输入指纹和源码范围校验针对绑定的 `workspace_root`。从受支持的 linked worktree 启动 CLI 时会寻找同一 Git common directory 下已初始化的主项目账本，不在每个 worktree 重建任务号。

多个现代任务可以各自冻结自己的 worktree；新任务不会解除旧任务的冻结。hooks 和 health 按调用目录对应的工作区查找任务，`active.json` 只作为没有匹配绑定时的回退。父目录的写者也不能借助目录包含关系修改另一个任务的 worktree。旧任务没有拓扑绑定时继续采用原有单活动冻结规则。

`health`、`status`、`resume` 和执行入口复核已绑定目录、分支、提交、Git 注册、基线及任务占用。`health` 的拓扑检查不改任务状态；发现缺失、漂移或冲突会输出阻塞风险及 `gsp-git` 路由。新任务未选择拓扑时返回 `GIT_TOPOLOGY_REQUIRED`。

Git 创建/切换前保存 inflight。中断后先调查实际 Git 状态和进程，写恢复报告，再 `recover-run --outcome not-run|failed`；随后按真实状态 `--rebind`，重新验证。恢复命令不会把未知执行伪造成成功，也不自动清理悬空 worktree。

## 前后端集成

父任务持有一个 GSP 号，T01 后端、T02 前端、T03 集成使用各自分支/worktree；T03 依赖两侧，负责最终跨边界测试。原地父目录可仅用于控制账本，源码写入仍在子工作区。

子任务分别测试并完成；提交获授权后，记录每个子任务实际 HEAD，再由获授权的集成者合并到 T03。门禁要求子任务源码干净、收据有效、集成 HEAD 包含每个子提交的祖先关系。没有汇合子代码的空集成树不能交付。只完成两侧单测不能代替集成测试。

`run --task all`、整体审查和最终交付针对集成工作区。任一子工作区改变后，旧 ready 不能继续有效。默认关联一个集成 PR；子 PR 按实际需要登记。当前不自动证明 cherry-pick/squash 后的新 SHA 来源关系，因此这种汇合方式不能自动通过本门禁。

## 能力边界

- 语言无关：Git 拓扑、任务和证据绑定不依赖 Python、Rust、TypeScript 等业务语言；检查命令由项目声明。
- Native 会话执行支持绑定 worktree。当前 headless driver 对独立 worktree/子工作区返回 `DRIVER_TOPOLOGY_UNSUPPORTED`，防止在错误目录运行后产生假证据；其原地流程继续使用阶段控制器。
- 真正非 Git 项目保留原工作流，但不具备分支隔离。损坏的 `.git`、Git 不可用和探测失败不属于该兼容例外。
- 工作区规则、CLI 写锁和宿主支持的编辑检查不等于文件系统沙箱，也不证明 agent 遵循了提示。父状态必须由协调者单写，不能让多个会话同时承担同一工作区的源码写者。
- `external-link --kind issue|pr --value ...` 只记录调用者提供的数字编号（例如 `#123`），不联网核验对象是否存在，不自动创建 PR。提交、推送、合并、发布和清理仍按精确授权执行。
- 本地测试与静态技能检查不能代替真实 Claude/Codex 触发、远端操作或业务验收。
