# 经验事件规则

目标项目的 `docs/gsp/learnings.jsonl` 是唯一经验日志。旧版逐行经验无需迁移；新版追加 add/source/invalidate 事件，查询折叠出当前状态、来源和替代关系。CLI 写入原子替换，使用项目写锁；遇到损坏日志先调查，不静默跳过。

- 保存前查 `learn --query "关键词"`，默认只返回 active。
- `learn --text "结论及适用条件" --source "证据位置"`：Unicode、大小写、空白规范化后原文相同则复用 ID，追加不同来源；不会模糊合并结论。
- `learn --supersedes <ID> --text "新结论" --reason "原因" --source "新证据"`：旧项必须 active；新项替代旧项，保留原始历史。
- `learn --invalidate <ID> --reason "原因" --source "证据"`：旧项标 invalid，不再默认返回。
- `learn --history [--query "关键词"]`：包括 inactive 项、替代关系和事件。人核对历史时使用；AI 不把失效结论继续作为推荐。

失效/已替代内容重复保存会拒绝，不静默复活。一次性进度写任务记录，未解决问题写 TODO，用户偏好按 plan-tune；经验必须有可核实来源。不跨项目自动合并、不连接外部记忆、不把本地日志视为用户新授权。
