# Codex 全局 hooks

## 宿主能力与 Windows 启动修复

本地编排的 Codex 才能派发本地命令 hooks；云端编排即使能操作本机，也不支持用户本地 command hooks。依据：[官方 hooks 文档](https://learn.chatgpt.com/docs/hooks)。没有本地事件时必须先确认宿主编排方式，不能要求用户反复信任或重跑业务。CLI/IDE/桌面的不同运行模式分别验收，不根据界面名称推断支持。

Windows 注册改用自有 `launch.ps1`（带 UTF-8 BOM），显式 PowerShell -File 启动，再用调用运算符执行固定 Python 路径；避免原先两个引号路径在 PowerShell 中被当作表达式。启动测试覆盖实际注册命令，不再只直接调用Python。注册命令发生变化后需要用户在原生宿主重新审查；安装器不写信任。命令启动测试仍不是原生事件派发测试。

hooks-status 显示本地/云端支持说明；hooks-report 的 audit_events_observed 与 coverage_state 显示是否读到审计事件。零事件是 not_observed，不能当完整覆盖。native_host_trigger 保持 NOT_CHECKED：手动协议或自动测试也能生成事件，需要结合宿主来源另行验证，不能仅靠计数宣称原生触发。

GSP 的用户级 hooks 与技能安装独立维护，作用于当前 Windows 用户。只有 cwd 能找到 `.gsp/project.json` 的项目才检查或记录；不会给普通项目自动初始化 GSP。

## 安装与维护

从源码包运行，或使用技能随附的 `<gsp>`：

```powershell
python scripts/gsp.py hooks-install --dry-run
python scripts/gsp.py hooks-install
python scripts/gsp.py hooks-status
python scripts/gsp.py hooks-upgrade
python scripts/gsp.py hooks-uninstall --dry-run
python scripts/gsp.py hooks-uninstall
python scripts/gsp.py hooks-recover --dry-run
python scripts/gsp.py hooks-recover
```

默认 profile 为 `~/.codex`，可用 `--profile` 指定测试 profile。install 可重复执行，upgrade 要求已有归属清单。维护仅替换哈希匹配的自有运行时与注册项；未知条目保留，损坏 JSON、用户改动或遗留事务拒绝继续。

注册文件 `~/.codex/hooks.json`；自包含运行时 `~/.codex/gstacksp/hooks/`；归属 `gstacksp/hooks-install.json`；事务 `gstacksp/hooks-transaction.json`；备份 `gstacksp/hook-backups/<uuid>/`。备份不是自动清理对象。恢复回滚本次事务，不能覆盖事务后的人为修改，不会移除活跃或未知持有者的锁。

**宿主信任不由安装器代办。** 新增或改变 hook 后，在原生 Codex `/hooks` 查看来源、命令和事件并审查信任。随后重启/开启新会话。当前桌面应用的具体审查入口以实际 UI 为准，CLI `/hooks` 是可用检查路径。不要使用信任绕过参数来冒充真实启用。

项目级、用户级和插件 hooks 可以共同加载；本功能不会禁用已有通知器，也不会默认加载旧 GS。插件清单继续保留空 hooks，避免用户安装与插件重复注册；全局启用使用上述显式入口。

## 实际行为

| 事件 | 行为 |
|---|---|
| SessionStart | 注入活动任务状态和已校验的文档路径，提醒核对恢复 |
| UserPromptSubmit | 记录本轮发生了用户输入，不保存输入正文 |
| PreToolUse | 记录尝试；对已冻结活动任务的直接编辑做路径检查 |
| PostToolUse | 记录结果事件到达，不把返回视为验收成功 |
| Stop | 记录回合结束，不自动续跑或宣布完成 |
| Interrupt / SessionEnd | 尽力记录结束，不清锁、不修改批准和进度 |

直接 `apply_patch` 检查 Add/Update/Delete/Move 的所有路径；路径检查复用 GSP scope，包括流程文件例外。解析失败或越界拒绝，不自动改变范围。新增文件沿现有祖先解析物理路径；这仍不能防止检查后链接被其他进程替换。

Direct-edit 的任务归属以补丁目标路径对应的 source worktree 为准。控制根 `cwd` 只用于定位项目和流程文档回退。目标属于当前任务的子 worktree 时，hook 使用父状态和子任务拓扑继续检查 scope 与 lease；目标属于其他任务、多个任务或未绑定 worktree 时拒绝。

长命令或 stdout/stderr 被重定向时，记录日志路径、PID/exec session、状态/退出码来源和预期产物。相同空 `write_stdin` 最多等待两次。第三次前读取日志、检查进程、退出码或产物。Codex wait-guard 在受信 hook 收到事件时拒绝第三次空等待，并记录 `coverage=wait-guard`。等待参数变化不会重置预算。

**Shell、write_stdin、特殊工具及宿主工具存在覆盖缺口。** 任意脚本、命令和 MCP 副作用不能通过路径字符串完整判断。hook 自身失败或未受信任也不能宣称保护生效。冻结不是沙箱；继续使用宿主权限、GSP `run` 证据与事后范围检测。

## 实验记录

```text
目标项目/.gsp/audits/codex-hooks/<会话标识哈希>/
  events-000001.jsonl
  coverage.json
  audit.lock
```

仅保存事件、时间、GSP 版本、任务、哈希化调用标识、工具名、检查判定和覆盖范围，不保存提示词、命令、工具结果正文、环境变量或原生 transcript 内容。锁用 OS 文件锁，与任务 writer.lock 独立。审计不可用会报告覆盖降级，不阻塞普通读取；明确的编辑拒绝仍保留。

单次输入上限 1 MiB，日志分片 4 MiB，每会话 32 MiB；限额后标 truncated。离线汇总最多 1000 会话，每会话最多 10 分片；它不等于完整会话归档。

```powershell
python E:\GstackSP\scripts\gsp.py --project E:\WXAssistant hooks-report
```

汇总 attempts、results_observed、unknown_attempts、denied、replayed_events、incomplete。重复工具事件在汇总时按会话、turn、agent、call、event 去重；原始记录保留。没有配对结果的非拒绝尝试视为 unknown，不能推断命令没执行或失败。缺调用标识不能可靠配对，标记 incomplete。

代码差异、真实命令证据和用户验收仍是复核依据。日志不证明 AI 阅读并遵循技能，也不防本机写权限篡改。原生会话格式不稳定，需要导入时另行适配。

## 验证边界

专项测试与部署协议冒烟只证明对应本地行为。真实宿主事件覆盖、信任状态、长时间运行、压缩/恢复和 UI 行为需另行采集。`hooks-status` 即使 integrity_ok 为 true，live_host 仍为 NOT_CHECKED；不能拿文件存在替代触发证据。

## 技能升级后的 hooks 衔接

Codex 用户技能 install/upgrade（包括 dry-run）返回 hooks 检查结果，保持独立维护。hooks-status 的 maintenance 展示 source_version、changed_files、removed_files、registrations_changed 与 next_command。

| state | 含义 | 下一动作 |
|---|---|---|
| current | 来源与自有安装相同 | 按需要验证原生触发 |
| version-label-update | 只有版本标签差异 | 按维护授权同步版本 |
| runtime-update | 自有公共运行时文件发生变化 | hooks-upgrade，保留备份 |
| registration-update | 事件或注册发生变化 | 升级后在宿主审查变化并验证 |
| recovery-required | 遗留事务，优先于来源升级 | 先核对持有者与事务，再 hooks-recover |
| not-installed | 尚未安装 | 仅在取得安装授权后 hooks-install |
| source-unavailable | 来源不可比较 | 从当前完整包运行检查 |
| repair-required | 自有文件或注册完整性异常 | 核对修改与事务，必要时恢复 |

来源是执行命令所用包，不是网络最新版本。版本相同也比较文件，旧来源也显示真实差异；使用当前发布包维护。报告仅给下一动作，不自动升级。公共运行时变化也需按宿主实际信任要求检查，注册相同不等于原生触发已验证。
