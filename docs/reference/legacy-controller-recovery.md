# 旧控制器无上下文时恢复

旧版本可能留下 active controller 与 workspace lease，但没有 SessionBinding。新版本默认 session-bind 会拒绝把它当成无控制器项目自动启动。

确认旧会话已结束且用户要求恢复后，显式调用：

```powershell
python scripts/gsp.py --project <项目绝对路径> session-bind <work UUID> --kind main_chat --host codex --context-out <绝对路径> --recover-legacy-controller --reason "用户恢复授权及原因"
```

仅无权威 controller owner、无已绑定会话、无其他活跃会话和租约时接受。该操作保留原控制器字段快照和恢复事件，降为只读、释放其旧租约，再允许新上下文正常 session-register。它不修改产品验收结果、不跳过 HEAD 检查，也不自动宣称旧宿主线程已停止；操作者须先核实。已有真实绑定应使用正常控制器 handoff/takeover，不适用此入口。

context-out 的绝对路径与项目边界检查失败不会释放旧租约。session-bind 在项目锁内执行；先持久保存旧会话只读降级，再签发新绑定，避免新 owner 已落盘却仍被旧租约挡住。降级后、签发前中断时，可以重新签发上下文；签发后尚未注册时，先核对并复用现有 context。重试保留第一次旧快照，不嵌套覆盖；旧快照保留在 sessions/index.json 的 legacy_recovery_snapshot。

回归入口：`python -m pytest tests/test_legacy_controller_recovery.py tests/test_session_cli.py -q`。2026-11-28 本地运行 22 passed；新增端到端用例另外定向验证通过。测试覆盖旧租约恢复、未显式恢复时拒绝、原快照保存、正常注册及后续 CLI 写入，并覆盖真实绑定与其他会话拒绝分支。此为源码修复，未自动升级全局技能安装。
