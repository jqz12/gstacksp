# 规格与计划治理

## 当前默认

新任务由new创建工作项，档案固定在docs/work/YYYY/MM/GSP-<全局编号>-<初始标题>/。年月取创建时间、月份两位，跨月继续/改标题/关闭不搬家。编号全项目连续；UUID是稳定身份。CLI运行ID由调用者选择，与工作项编号不同。

work-new先保存brief.md和record.json；design阶段创建spec.md，AI填写设计；用户真实批准后approve绑定哈希并生成plan.md模板。新布局不是五份同时生成。任务中有决定才追加decisions.md；审查者写review.md后CLI登记；ready起草delivery.md。各任务独立规格与计划，不把T1/T2实施步骤变成额外规格。

status.documents和resume.read_next返回实际正本。检索默认10条、最多50条，可offset分页；按模块和显式关系查摘要，再精读相关全文。索引是可重建缓存，外部编辑用docs-index --rebuild；没有语义召回保证。

## 旧布局兼容

旧年目录docs/work/YYYY/<档案>/、旧docs/superpowers/specs/plans，以及无documents字段的docs/gsp/tasks/<id>/design.md/plan.md继续读取。不会因升级自动搬迁、撤销原批准或制造新授权。旧模板一次五份是旧布局行为，不是当前默认。

## 显式关联迁移

先从status/resume核对真实路径、当前内容与是否在场执行；保全原文、核对新正本与引用。已有项目docs内Markdown可用documents-adopt <id> --spec <相对路径> --plan <相对路径> --reason <依据>关联。它不是通用移动许可，未知执行/驱动或其他任务占用会拒绝，不得手改状态绕过。

关联变更保留document_history，撤销当前批准、计划封存、审查及交付有效性；完成任务变为needs-verification，旧证据留历史。按真实对话核实变更范围、重新批准/封存/相关验证，再最终all与审查。无需重写正确实现，不从文档自称APPROVED推断授权。

普通资料搬迁见 [共同治理契约](document-governance.md) 和 [执行规则](document-governance-execution.md)。交付实际目录、差异、阅读入口及技能清单见 [交付与衔接](output-handoff.md)。
