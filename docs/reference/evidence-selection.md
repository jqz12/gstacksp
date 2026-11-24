# 有效证据选择与失败屏障

`task-done`、`ready` 和交付校验不再只看最后一张回执。运行时按任务、验证类型和命令入口选择有效证据集合；原始回执与失败历史全部保留。实现正本为 `gsp/runtime.py` 的 `select_evidence` 与 `evidence_for`。

## 有效性条件

一张回执只有同时满足以下条件才进入有效集合：

- `task` 与查询任务一致；最终 `all` 回执不能顶替子任务证据。
- `kind` 为 `tests` 或 `check`。
- 回执记录的 `workspace_root` 与当前绑定工作区一致。
- 输入指纹与当前源内容一致。
- 契约摘要与当前设计、计划和任务定义一致。
- `verification_epoch` 不低于当前隔离纪元。
- 回执绑定的命令入口脚本摘要仍与当前文件一致。
- 指定 `entry` 时，命令入口摘要必须匹配。

不满足条件的回执仍在账本中，但会进入 `invalid_reasons`，不能用于完成或交付。

## 入口分组

入口摘要由 `kind` 和命令参数共同计算。同一任务可以同时保留：

- 多条 `tests` 入口；
- 多条 `check` 入口；
- 每个入口的历史成功与失败。

每个入口只取最新一张回执作为该入口的当前结论。`tests` 成功永远不会因为之后的 `check` 成功而消失。

## 失败屏障

同一任务、同一输入、契约、纪元和 workspace 下，任一入口的最新结论不是 `pass` 时，该入口阻止任务完成：

- 只有相同入口出现后续成功，才能解除。
- 其他 `check` 或 `tests` 入口的成功不能代为解除。
- 回执状态 `fail`、`failed-output`、`zero-tests`、`test-count-unknown`、`inputs-changed`、`scope-violation` 和 `not-run` 都不是有效成功。
- 输入、契约、纪元或 workspace 变化后，旧失败与旧成功一起退出当前有效集合；它们不会被重新解释为当前结论。

`task-done` 优先引用最新有效的 `tests` 回执；只有显式记录 `no_tests_reason` 时才引用 `check` 回执。检查回执仍保留在同一运行的 `state.evidence` 中。

## 错误码

| 错误 | 含义 |
|---|---|
| `EVIDENCE_MISSING` | 该任务没有任何回执 |
| `EVIDENCE_APPLICABLE_FAILURE` | 某个入口的最新有效结论仍是失败 |
| `EVIDENCE_WORKSPACE_CHANGED` | 没有当前 workspace 下的有效回执 |
| `EVIDENCE_CONTRACT_CHANGED` | 设计、计划或任务定义已变化 |
| `EVIDENCE_BEFORE_RECOVERY` | 回执早于当前隔离纪元 |
| `EVIDENCE_ENTRY_CHANGED` | 指定入口没有有效回执 |
| `COMMAND_INPUT_CHANGED` | 回执绑定的脚本内容已变化 |
| `EVIDENCE_STALE_OR_FAILED` | 输入变化或没有可用成功回执 |
| `EVIDENCE_TAMPERED` | 已选回执的日志摘要不符 |
| `EVIDENCE_INCOMPLETE` | 回执缺少必要的日志证明 |
| `TESTS_REQUIRED` | 只有 check 证据且未记录无测试理由 |

## 边界

- 选择函数不修改、不删除、不重写历史回执。
- 旧 schema 回执按已有字段参与选择；缺少的新增字段不会被补造。
- 日志摘要与命令输入摘要能发现篡改或内容变化，但不是回执签名，也不构成对恶意重写整个回执的密码学证明。
- `all` 回执只用于最终回归；它与子任务回执用途隔离。

## observe-head 幂等

`head_observation_change` 把一次观察分为身份变化和验证内容变化：

- HEAD、绑定根或分支完全不变，且验证输入仍有效：返回 `no-change`，不清理 review、不改变 ready、不追加历史、不重写状态文件。
- 只有 HEAD 变化：仍重新核对工作区、分支、base 祖先、契约、纪元、测试入口、回执完整性和集成关系。全部成立才保留内容证据，并追加身份历史。
- 工作区、分支绑定、脚本入口、契约、纪元、回执或集成关系变化：进入 `verification-changed`，保留原始证据但撤销 review；ready 状态转为 `needs-verification`。

子任务观察使用 `allow_head_change=True` 读取新 HEAD 下的内容证据，仅在观察窗口内绕过“绑定 HEAD 尚未更新”的顺序检查；更新完成后仍按新 HEAD 做后续命令校验。父任务在 ready 状态下先由原有交付自检重新核对内容，再决定是否保留 review。不能只比较 tree hash 或只更新 HEAD 就跳过交付门禁。
