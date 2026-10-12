# P3：明确调查与独立候选验证

P3 将 investigate / verify 接入 `task_review_v1` 的统一离线运行时。生产图、API、覆盖报告和前端接入属于 P4；默认引擎未切换，本阶段没有真实付费实验或质量收益结论。

## 协议和任务

- inspect 的逐项提交继续保留合法历史。investigate 使用服务端绑定的命题、必要范围与原文，返回 supported / refuted / insufficient_evidence；候选和后续线索分别验收，命题答案格式错误不会丢掉合法候选。
- verify 每次绑定一个候选，使用独立模型请求，只提交该候选的 supported / refuted / insufficient_evidence 判定。未知候选、不可见或失效证据、未引用的必要原文和已知反证都被拒绝。
- 调查被 refuted 是合法终态，不能强迫改成 supported/verified。调查可生成新候选，但不回填父 review_record；已回答调查只重新排队其明确绑定的 blocked 目标，仍需原 inspect 再次提交检查结果。
- file_read、file_find、code_search、file_read_diff 使用已有请求模型和受控工具。工具观察有界保留并进入后续 Prompt；搜索原文与固定 Git blob 核对后才归档。diff 渲染作为观察，不伪装成可引用的 Head 源码区间。非法 shell 动作没有执行路径。

## 候选顺序与证据

`review_task_derivation.py` 在合法结果后执行：候选原文验收 → 复用 EvidenceResolver 与 IssuePolicyService → 严格等价归并 → 必要原文准备 → 独立 verify 工作项 → 最终处置。没有调用旧 IssueVerifier 的批处理消费循环。

模型候选新增明确 affected_behavior、failure_scenario、recommendation 和可选 evidence_requests。旧最小候选仍可解析和保存，但缺少具体故障描述时保留 unresolved，不补造故障条件或发起可预见无效的验证。服务端仅用固定中性 confidence 适配旧 Policy；生成者的置信度没有进入独立验证 Prompt。

严格等价键保留大小写、原主张、受影响行为、故障条件、源码位置、范围、证据内容与快照；只忽略服务端来源 ID。相邻行号和相似文本不自动合并。归并记录保留全部 candidate/result 来源。验证复用键进一步绑定模型、供应商端点、协议、规则、输出额度、范围、必要证据和反证；动态预算余额与无关工作项 ID 不进入键。

缺少明确原文时，先排一代具体 investigation，并用独立受控工具动作准备证据；无合法读取路径时保持 unresolved/not_scheduled。未知、失败、缺失和 insufficient_evidence 不缓存为验证成功。已发送但 usage 未知的支持判定仍保留 unresolved；费用不会填零。

## 调度、风险与恢复

所有派生项进入现有 TaskReviewRuntime 队列，与初始 inspect 共用一个 ReviewRunBudget，没有新的局部消费池。新增命题去重绑定命题、范围、快照和证据版本；重复线索保留审计。确定性风险目录将当前 repository_graph 的跨 Unit calls/imports/test_of/configures 关系投影成具体命题和原范围绑定，不读取旧 Unit 完成状态作为新成功。

首版限制一代调查。调查返回的新调查线索保留 `not_scheduled / second_generation_disabled`，不会形成递归任务树；新候选可正常接受独立验证。派生工作项、命题/候选绑定、调查/验证结果、来源、复用缓存与失效事件都进入独立 runtime CAS 和 hash artifact。恢复重建同一队列，不重复已完成的请求或费用。

显式证据失效事件保留被撤销结果及原因，撤销依赖它的检查、页面结果、调查和验证缓存，不撤销独立目标。新有效证据改变验证前提后，重新计算复用键并通过新准入发送。

## 验证

聚焦测试通过 60 项，涵盖 P1/P2 回归和 P3 的严格归并、不同故障、Policy 拒绝、证据准备、反证校验、命题反驳、二代调查拒绝、未知 usage、受控工具、显式失效、blocked 续查和数据库重启。SDK stub 仅替换 SDK `ainvoke`；prepare、Provider、账本和数据库/文件持久化仍执行真实实现。

已核对新路径实际发送操作仅为 `task_review_v1`，任务种类为 inspect / investigate / verify；cross_unit_coordination、purpose_summary 和旧 review_step 均无新路径调用。完整后端回归通过：1174 passed、2 skipped，192.01 秒。Ruff 与 `git diff --check` 通过。没有前端变更，未重复运行前端构建。

P0 的三项语义标签分歧与 43 条参考意见语义核验缺口仍保留，尚未取得 P5 真实质量门证据。新结果通过协议和引用校验不能被解释为客观语义正确性证明。
