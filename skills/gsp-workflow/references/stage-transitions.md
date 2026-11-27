<!-- stage-transition-contract -->
# 阶段推进

常规推进只调用一次 `advance`：同一业务操作完成当前可判定预检、必要版本复核和
门禁判定。`handoff` 仅用于需要主动预览下一阶段、阻塞项或责任转移的场合，不是
每次推进的固定前置命令。

真实副作用入口仍复核 actor、work、capability、current generation、lease、契约、
写集和资源版本。预检通过不会替代执行入口复核。失败时读取结构化 blockers、
`next_skill` 和恢复入口；不得手工改状态或把聊天说明当成交接证据。

未来阶段才产生的证据标为待产出；适用 Red/Green、最终 all、独立审查、verify 和
资源处置仍在其实际完成点强制执行。同一输入与阶段只能复用严格等价的真实结果。
