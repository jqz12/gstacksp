# 持久无人值守驱动器

用于用户明确要求离开会话后继续执行的任务。先确认 design、记录真实 unattended 授权和冻结选择。`<gsp>` 使用共同契约中的实际脚本路径；完整命令以 `--help` 为准。当前仅提供 Codex 适配，Claude 不会因安装技能而获得后台驱动。

1. 查看任务 status，核对设计批准、任务/计划和范围。已有计划须 seal-plan；没有计划时驱动器可从批准设计生成完整任务并封存。不要覆盖现有未封存任务，应先核对封存。
2. 将项目真实检查 argv 写入 JSON 数组，例如 `["python", "-B", "-m", "unittest", "-v"]`。使用绝对可执行路径更稳定。检查由调用者指定，模型不能将回复作为 shell 命令执行；测试输出须可识别且非零。
3. `drive-init <id> --checks <JSON绝对路径> --quote "实际启动依据" --max-dispatches 20 --timeout 900 --max-seconds 14400`。需要时显式 `--codex-bin <exe>`。`--check-kind check` 仅适合各任务已有具体无测试理由的检查。初始化固化 max_fix_rounds 和限制；不能重新初始化清空消耗。
4. `drive-start <id>` 返回独立进程身份；`drive-run <id>` 是前台控制器。`drive-status <id>` 查看最终状态与 gate；命令退出 0 代表查询/执行命令成功，不代表任务 completed。等待后台结果后看 `driver-report.md` 和 `delivery.md`。

状态顺序：plan → execute/任务检查 → verify/整体回归 → review → deliver。失败触发对应修复与重新验证。每个任务修复链、最终审查修复链分别消费 max_fix_rounds；初次实施不计，派发前即计数。绝对截止时间和派发数包含恢复前消耗。实际金额未知，派发上限不是费用预算。

`drive-next` 持久生成一次 UUID job，重复调用只返回待收结果，不再派发；`drive-submit --job <id> --result <指定响应路径> --exit-code <真实退出码>` 只适合集成实际执行器。不要手工编造 worker 结果或退出码。内置控制器自动完成两步。`--host-command` 是显式命令集成/测试入口，不代表真实 Codex。

普通 technical 问题带明确 recommendation 时记录推荐继续。重要设计、授权和依赖问题会生成单次 gate。核对真实用户答复后：`drive-answer <id> --gate <当前ID> --answer "答复" --quote "原话"`，然后 drive-start；不顺便授予设计变更或远端操作权限。旧 gate/旧 job 不能用作新结果。

worker 被杀、超时、控制文件变化、结果格式错误等均视为执行未知，不盲重跑。先检查 `drive-status`、attempt 日志、进程身份与实际文件，确认控制器和 worker 都已结束，写项目内恢复报告，再 `drive-recover <id> --evidence <报告绝对路径> --reason "调查结论"`，最后 drive-start。恢复保留旧 attempt、检查未知记录和计数，清除旧审查并重新验证；不自动批准新设计。不能把 unknown 用普通 gate-answer 放行。

`drive-stop <id> --quote "真实停止指示"` 使活跃控制器终止其 worker，并保留部分修改；恢复同上。已死控制器无法响应 stop，先核对记录的进程，避免误杀其他 PID。锁不按年龄偷取，恢复只能清理已核对死亡且属于该驱动器的锁。

Windows 后台使用隐藏、独立进程组并请求脱离父 Job；系统拒绝则报 DETACH_UNAVAILABLE，不能声称后台独立成功。关闭创建者与终止 worker 是不同情况；电脑关机/睡眠、驱动器自身崩溃后不会自动开机重启。Codex 使用现有登录/配置，-a never、只读计划/审查、workspace-write 实施，不使用权限绕过。控制文件校验是事后检测，工作区不是完整 OS 沙箱，不能保证项目外或忽略目录完全受控。

人读：status/resume返回的任务档案内 `driver-report.md`、delivery/review/decisions。AI 恢复：实际绑定的 spec/design与plan 及 `.gsp/runs/<id>/driver/state.json`、`gates/`、`attempts/<uuid>/`、`events.jsonl`。真实检查证据仍在 `.gsp/runs/<id>/evidence/`；日志只留本地，常见凭据模式脱敏不代表全面秘密扫描。


## 新后台计划的接纳（0.8.9）

新后台计划由 worker 返回完整正文与匹配的任务列表。worker 做内容自查；主控负责登记任务、结构检查与封存，不要求只读 worker 执行 GSP 记账命令。

主控在 accepted.json、正式计划与任务写入之前，生成含实际 Spec 链接的候选正文，按 UTF-8 1 MiB 上限和现有 check_plan 核对。失败保留原始 response.json，并写同 attempt 的 rejected.json（job、响应哈希、quality）；不写 accepted，不改变绑定计划、任务与 seal。job.plan_findings 将结构诊断交给下一次规划。

结构缺项自动进入 plan 修复链：初次不计修复，重试派发前记入 fix_rounds.plan，复用 max_fix_rounds、总派发数与绝对截止时间，重启不清零。有效修复接纳后清除当前反馈与 pending_fix，保留历史次数，正常实施不继续计作计划修复。超过预算停止并报告原因；不能手改计数解除。

实际写入后仍以 seal-plan --check-structure 核对绑定正文。已有任务不能自动清空重建；旧无开关 CLI、已有封存和历史任务保持兼容。结构结果始终 semantic_review=NOT_CHECKED，不证明迁移方案、接口含义或需求覆盖正确。

格式不可信、状态改变、进程/文件写入中断仍属于未知执行，需要按 drive-recover 调查恢复。存在 accepted.json 或 rejected.json 的未完成接纳禁止直接重放；标记后持久状态写入失败也不能当成普通计划缺项重试。保留原始响应不等于其已被接纳。

恢复会核验 pending attempt 的拒绝记录（job、响应哈希及实际结构诊断），恢复原有计划反馈与修复链，预算不清零；不匹配报 PLAN_REJECTION_MISMATCH，必须继续调查。若计划已封存而驱动状态写入中断，核对 seal 后才转 execute、清除临时计划修复标志，历史 fix_rounds 保留。
