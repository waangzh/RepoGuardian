# P2：统一调度、容量分页和请求恢复

本阶段在 `task_review_v1` 离线入口实现统一运行时。生产入口仍明确拒绝尚未完成的引擎，默认仍是 `legacy_units`；生产规划、调查/验证和报告接入分别属于 P3/P4，未宣称已经上线。

## 调度与范围

- 一个运行队列拥有 ready/running/waiting_evidence/deferred_budget 和终态；并发可配置为 1–32，同一工作项不能被两个执行者领取。
- Unit 按稳定归属轮转；基础、续查、验证、调查有界轮转，同组未获得基础检查机会的工作项不会被新增调查越过。同类任务也轮转，避免低 ID 持续占用。
- 每次调度只推进一个模型请求或一个工具动作。唯一运行循环管理期限、停止原因和失效保护释放，没有新建 Unit、Verifier 或协调私有 drain。
- 所有真实发送继续走 PreparedModelRequest、真实 Provider SDK 边界与唯一 ReviewRunBudget。预算暂缓只在预算/义务 revision 改变后唤醒，消费余额不参与容量分页。
- `task_context_pages.py` 仅按完整源码行组织容量页，保存原 hunk、原目标区间和内容 hash。页面属于原工作项；不创建子 Unit、预算池或问题生命周期。所有页通过后才聚合原固定目标，首个页面通过不改变分母。
- 已接受结果保留在服务端，后续 Prompt 只列当前页未完成目标。Evidence Store 的归档原文与实际活动证据继续分离。

## 持久化与恢复

`task-review-v1:<run_id>` 的 SideEffect 保存冻结 RunSpec、完整目标、页面映射、队列、request/attempt 和 artifact 引用。Evidence Store、活动证据、合法历史、工具观察和原始响应保存到受控 hash artifact；数据库 CAS 不携带整个 Prompt。Artifact 写入使用同目录临时文件后原子替换，URI 支持含空格路径，读取保留原换行。

运行状态和预算是两份 CAS 记录，不宣称处于同一个事务。预算必须按 `recover_running=False` 加载，之后由 `TaskRuntimeCheckpoint.restore()` 按 attempt 对账，再重建队列。worker 的 runtime 与预算适配器必须采用相同 lease。

| 崩溃边界 | 恢复行为 |
| --- | --- |
| reserve 后未发送 | 只有账本仍为 reserved 且 sent=false 才释放；Provider 在 SDK 调用前持久化 dispatch 标记是未发送证明 |
| dispatch 后没有可信 usage | 保守记 unknown，保留预留；不重发旧 attempt，retry_unit 不重新排队 unknown |
| 响应已保存但 usage 未结算 | 使用 hash 校验后的响应 usage 对账，再幂等应用结果 |
| usage 已结算、结果未应用 | 重放已保存响应，不调用模型，不重复计费 |
| usage 已知、响应丢失 | 标 result_missing，消费保持已知；显式 retry 的新发送需要新准入和新 attempt |
| 结果已应用、队列未更新 | 从已保存结果和单步观察重建队列，不重复候选或费用 |
| CAS 冲突或 lease 丢失 | 检查点回到最后 committed 并失效；旧 worker 禁止继续写回 |

取消阻止后续调度和 Provider 的传输重试发送，释放未转换保护；已经在途的合法迟到响应仍可结算和保存。运行期限超时保留 unknown 用量。两次相同参数取证且观察相同后，继续同类读取被拒绝；仍允许基于已有证据提交，重复读取不无限续查。

`retry_unit` 只排该 Unit 的 pending/blocked 缺口，保留其他合法结果、预算、纠错次数和已完成页面。unknown 不作为免费重试。报告语义和 API 重试路由在 P4 接入，当前以新运行时方法验收。

## 验证与限制

SDK stub 只替换模型 SDK 的 `ainvoke`；prepare、发送准入、reserve/dispatch/settle、响应观察、逐项校验、CAS、SQLite、artifact 和队列恢复均执行真实代码。覆盖 reserve/orphan、dispatch、响应保存、usage 结算、结果应用、丢失响应、lease、取消、并发、预算唤醒、分页恢复和无进展停止。

聚焦测试通过 42 项。完整后端回归通过：1154 passed、2 skipped，215.34 秒。最后补充的预算实例一致性校验经过 20 项 P2 聚焦复验；Ruff 和 `git diff --check` 通过。本阶段未改前端，没有重复运行前端测试或构建。

本阶段没有真实付费模型对照，不提供成本下降或缺陷质量结论。P0 的三项语义标签分歧和 43 条参考意见未完成语义核验仍是 P5 质量门的前置缺口；引用匹配不等于语义正确性。
