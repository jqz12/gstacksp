# 文档治理契约

本契约适配产物位置，不压缩 SP 正文。新任务固定在 docs/work/<年份>/<编号-初始标题>/，身份和 record.json 可随 Git 保存；.gsp/runs 保留本机执行状态与证据。标题变化、搁置与关闭不搬目录。

## 想法与开发

- 用户明确要保留想法：`<gsp> work-new --title "标题" --brief "目标" --source "真实来源"`，只产生 brief 与身份记录，不等于开发授权。
- 继续讨论：`work-stage <编号或UUID> design --reason "原因"`，产生 spec；暂停用 paused，取消用 cancelled，记录理由。
- 开始开发：`new <运行ID> --title "标题" --work <编号或UUID>`，仍须真实 approve，不用治理阶段制造批准。没有已有想法时直接 new。
- approve 后生成计划模板；task-add/seal-plan、执行、review、ready 沿用完整方法和证据门禁。文档按需产生，不提前制造整套空报告。
- work-show 查看档案；work-update 修改标题/短摘要/模块，不改稳定目录。关闭用 work-stage delivered/closed --reason ... --closure-condition ...，须当前 run 真正 ready；该条件由调用者依据真实约定归因，不是用户验收认证。
- 交付以后新需求建新项，用 work-link --relation fixes|extends|supersedes|depends|derived-from|duplicate --reason ... 关联。关闭后的记录保留历史语义。

## 查找与模块

`module-set --name ... [--id UUID] [--alias ...] [--path ...] [--parent UUID]`。身份不随改名或代码移动变化；parent 表示前身来源，适用于拆分/合并的追溯线索，不自动继承所有旧约束。模块职责由 AI 核实，不从 Git rename 直接推断。

`docs-search --query ... [--module UUID] [--related 工作项] [--limit 10] [--offset 0]`。最多 50 条，默认最多两层模块前身，返回摘要与命中理由；不是全部历史完整召回。检索结果阶段只是记录，关联执行任务要 resume 验证。跨工作树/分支核对索引上下文；外部新增或编辑后用 `docs-index --rebuild` 完整刷新。索引损坏时同命令重建，不删除治理正本。pending 事务用 docs-recover，存在外部内容冲突不得强行覆盖。

## 目录职责与旧项目

product 当前行为/架构，reference 接口协议，howto 操作，work 任务，research 研究，archive 旧历史，ops 运行验证，gsp 工作流入口/报告；按需创建。根 README、TODO、工具需要的 DESIGN 等可保留入口。旧目录先原位盘点和绑定，最终分批统一，不无限新旧双写。

已有规格/计划可通过 documents-adopt 原位关联（项目 docs 内 Markdown），原批准和验证撤销，原文保留。未确认的既有 APPROVED 文字不是新授权。一个旧文件混合多阶段时先保留原文、记录章节线索，程序尚不自动验证章节有效性或还原实施历史。

普通 Markdown 归位使用 docs-move --source ... --destination ... 默认只预览，明确 --apply 才改；保留旧入口链接、更新支持的内联链接、备份与持久事务。引用样式/HTML/外部工具需人工核对，执行绑定材料和在场驱动会拒绝通用搬迁；不要绕过拒绝。中断用 docs-move-recover，冲突先调查。

## 健康检查

交付前由 AI 核对本次修改涉及的模块、接口与目录变化，必要时维护已有当前说明并更新模块映射，再做最终验证；不能测试后改说明还复用旧证据。不影响说明时记录理由，不强制每个小任务生成模块文档。CLI 不自动推断代码职责或把历史设计升级为现状。

health --documents-only 只读检查目标，默认写目标 docs/gsp/reports/document-governance/<唯一运行ID>/。--report-dir 可把报告写到用户指定位置，--documents 合并原安装检查。输出 report.md 与完整 report.json；不覆盖历史，latest.json 只是入口。规则区分完整性错误、候选改进和 NOT_CHECKED；无断链或无字节重复不证明文档语义正确。发现多余资料必须核实用途，不能自动删。

交付时对本任务相关产物核对绑定与当前说明；全量 health 是显式审计，不每个状态写入扫描全库。长 TODO 历史归档、自动语义查重、章节级治理与线上文档发布仍需独立能力，不声称本版全自动实现。

当前验证默认排除 docs/work、docs/superpowers 和 docs/gsp 的工作流档案；本任务 spec/plan/审查另有哈希绑定。product/reference/howto、源码与 TODO 仍进输入指纹。如果历史档案本身是测试或发布目标，不能套用默认排除宣称覆盖，需使用显式文件检查并在报告说明边界。
