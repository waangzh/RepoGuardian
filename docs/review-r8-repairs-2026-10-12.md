# r8 后续修复

## 阶段一：完成判定与契约回填

同一证据的重复报告达到停止阈值时，仅在最新记录有效、快照和证据未变、目标及契约已完成、没有必要取证义务和覆盖缺口时正常结束。必要读取失败、记录失效、遗漏目标、未决问题和契约仍保留 unknown/partial。

服务端从当前有效父记录生成 origin obligation，绑定 Unit、完整快照、输入指纹、Manifest 和原始问题/依赖。协调模型只能选择目录中的 ID；补查通过 obligation_updates 提交带当前证据的明确闭合结论。回填生成新的父结果投影，保留原身份与记录历史，只更新绑定问题/依赖，不授予 hunk 或目标检查覆盖。

旧计划没有绑定 ID 时保持原行为；父记录 invalid/unknown、补查失败或未确认、快照变化、引用未知证据、更新跨 Unit 或引用未授权义务时不回填。新增字段可选，前端类型同步；输入与协调运行指纹升级，旧运行不会自动获得新协议能力。

验证：100 项相关回归通过；完整后端 1,056 passed、2 skipped；Ruff 和前端构建通过。测试使用本地桩，不调用真实模型或执行目标仓库代码。上述结果不表示 r8 历史产物已经改变，也不证明真实覆盖或质量提升。

## 阶段二：输出契约与一次有界纠错

原生单步输入提供稳定 target_catalog，模型可返回目标 ID，服务端恢复为原目标文本；旧精确名称继续有效，ID 与名称重复提交会拒绝。目标与假设状态枚举分开说明，示例使用实际合法状态；resolved 需要当前证据，superseded 必须引用有效且不同的 replacement。没有有效 replacement 时不生成转移，不放宽身份、证据或完成门。

每个 Unit/工作集最多尝试一次输出纠错，仅用于已返回且用量已知的 JSON/schema 或目标身份格式错误。纠错复用原证据与读取范围，只反馈有界校验信息，不回显原始非法响应。首轮有效候选和有效组件由服务端保留，纠错不能新增候选。成功输出仍经过完整引用与覆盖校验；失败继续保留 invalid/unknown。证据不足、网络错误、超时、取消、未知用量或没有预算时不启动格式纠错。

纠错使用同一请求预检、Unit ledger 和整轮账本，独立记录 usage 和 review_step_repair 操作；不提高整轮预算。补查允许一次普通诊断加一次纠错，禁止把额外额度用于普通重复审查。既有传输重试仍遵守原政策，每次发送分别计量。取消时保留首轮已观测 usage 与候选快照，继续传播取消。

纠错次数标记进入可恢复的 Unit 结果；恢复同一输入时不重置次数，保留既有候选，精确重复主张仅合并来源并建立 canonical 审计。纠错预检同时检查真实请求 token 额度与调用额度，未发送的预算拒绝不计入审查次数。失败工作集恢复使用当前共享额度，不采用旧的临时 holdback；旧失败的派生 unknown 不作为当前输入省略，实际遗漏目标/上下文仍保留。

官方资料支持把可恢复校验错误反馈给模型后重试，但不保证语义正确。当前使用 JSON mode，不切换端点、不增加框架依赖；DeepSeek strict tool schema 的 Beta 端点能力未在本项目实测。

- [DeepSeek JSON Output](https://api-docs.deepseek.com/zh-cn/guides/json_mode/)
- [DeepSeek Tool Calls strict mode](https://api-docs.deepseek.com/guides/tool_calls/)
- [LangChain Structured Output](https://docs.langchain.com/oss/python/langchain/structured-output)
- [Pydantic AI Output validators / ModelRetry](https://ai.pydantic.dev/output/)

回归覆盖目标 ID 归一化与重复身份、非法动作/目标状态/问题迁移/JSON 的一次纠错、首轮候选不丢不重、重复错误停止、传输失败、实际任务取消、未知用量不重试、Unit 与共享预算拒绝，以及两轮实测 usage 各计一次。完整后端 1,074 passed、2 skipped；Ruff、前端构建和差异空白检查通过。独立提交快照结果以交付说明为准。没有启动新一轮付费验收，真实 schema 成功率、完整覆盖和总消耗仍需后续冻结验收验证。
