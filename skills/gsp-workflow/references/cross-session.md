<!-- cross-session-contract -->
# 跨会话与资源恢复

单会话仍是默认路径。`cross-session` 由主会话维护唯一总体计划，Codex 原生聊天、
线程、代理、历史、压缩/恢复、模型参数、Goal 与 worktree 生命周期继续作为宿主事实；
Goal 仅在用户明确创建目标时使用。GSP 只保存业务关联、主体代际、租约和必要证据。
`drive-*` 仅作为用户明确要求的 legacy 兼容入口，不属于默认流程。

调度以一份完整 TaskContract 为权威，包含本任务全文、全部适用全局约束、依赖接口、
验收和验证入口。派发视图由该契约派生，不复制第二份正文，也不要求普通执行者反复
读取整个总体计划。同一问题修复续原执行上下文；独立 SDD 任务创建独立上下文。

主会话显式使用 Sol；正式 executor、reviewer 全部请求 `group/deepseek-2` / `max`。
本版仅接入扁平正式会话；子 agent 派生、嵌套 helper 暂不接入。执行 CLI 显式关闭
native multi_agent。项目三个执行槽由全部正式执行/审查会话共用，legacy Codex worker
也占槽；未知进程终态不能自动释放。宿主模型请求、实际可观测模型与 unknown 分开报告。
会话和派发使用项目原子唯一 UID，`Snn`/`Dnnn` 是有工作项作用域的兼容别名；
`Tnn` 是工作项内的任务身份。正式报告替换前
移入 `sessions/history/`。Codex 线程 ID、branch、worktree、Commit SHA、Issue/PR
和 GSP 编号分别记录。

共享资源以项目级持久登记和短事务租约保护。实际调用者必须匹配 work、role、
capability、controller generation 和 lease。Writes 是并行规划声明，不是目录硬白名单；
真实冲突返回 `SESSION_PATH_CONFLICT`，旧代际和
未知进程结果 fail closed。Shell、外部编辑器和未接线的宿主入口标为
`NOT_CHECKED`，不能把事后检查冒称编辑前保护。

恢复先查询已持久 request 和真实文件/进程/worktree 结果，再继续未完成步骤。
只有可独立验证的并行写入才增加工作区；有序任务复用已授权 group，每个独立 SDD
任务仍有新的执行上下文和独立审查，同一问题修复精确续原上下文，不使用 `--last`。
原生树由 Codex create/archive 配对，缺回执保持 pending；旧 managed 树沿用原 owner，
bound 树只 release。
任务接受与资源清理是两个状态：`session-accept` 登记接受；干净的 legacy managed worktree
由 owner 清理，bound worktree 只释放绑定；失败或未知结果保持
`cleanup-pending`，使用 `session-clean --retry` 只续做未完成步骤。缺失路径不等于
已完成物理删除。认知压缩不强制重写全仓摘要或重新注册租约。

导航默认 `status <id> --brief` 或 `resume <id> --brief`，只读且 validation=NOT_CHECKED。
不带 brief 的旧 resume 保留严格校验语义。`schedule preview` 只显示扁平就绪批次；
真正派发通过项目槽位与游标 CAS 再核对，不能把预览当成租约。`schedule usage --id <id>`
汇总有唯一响应身份的原生 usage，cached/reasoning 是子集，未知用量不是零。

原生树动作使用 `native-workspace <id> create|archive|restore|confirm|status
--request-uid <uid> --payload <JSON>`：先保存意图，再请求实际 Codex 工具，最后确认
精确宿主回执。归档不等于合并，恢复后仍须复核 attachment、common-dir 与 HEAD。
`evidence-collect`/`evidence-verify` 只处理原件、manifest 和 Git 可达性，结果
quality_status=NOT_EVALUATED，不能替代最终 all、独立规格/质量审查或验收。

需要架构检查的改动先用 `python -m gsp.architecture` 形成项目/局部事实和待评审基线，
通过 `architecture-policy <id> --baseline <相对路径> --scope <范围> --reason <理由>
--quote <实际授权>` 绑定，再正常封存计划。历史债务、未知覆盖和输入版本保留；
新违规在验收前阻断，执行期仍可运行 Red/诊断。范围不是编辑白名单；范围外实际
改动也纳入依赖影响分析。未知语言需要输入绑定的独立评审，不能冒称静态覆盖。
