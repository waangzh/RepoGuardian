# P4：生产入口、版本化报告与兼容

本阶段将 `task_review_v1` 接入独立生产图：准备固定仓库 → 规范 diff / 静态索引 / 计划 → 一个 task_review_runtime 节点 → 确定性报告。新路径不调用旧 Unit、Verifier、协调和语义去重循环，也不生成付费 PR 目的摘要。默认配置仍为 `legacy_units`，等待 P5 真实对照门。

## 固定配置与源码范围

任务创建时保存模型/供应商/端点、窗口、预算、并发、运行期限、请求期限和工具限制；Repository 拒绝覆盖已有配置或引擎。API、服务、评测分别按冻结引擎路由。生产节点使用唯一活动 ReviewRunBudget，不再创建第二个账本实例；新引擎预算以 recover_running=False 加载，由 runtime 检查点先对账。

目标按服务端计划的原 hunk 区间生成，原文通过 tracked/realpath/sensitive 检查后从固定 Git SHA 读取，进入本次活动证据。原实现草稿中的空内容占位和 1→文件末尾目标已替换。RequestFit 容量不足才按完整行分页；消费余额不参与分页。删除、二进制、无 hunk、排除文件仍有 file_change/unsupported 行，不能从分母消失。本版对这些非 Head 文本变化保守报告 unsupported，没有冒充影响检查完成。

## 恢复、取消与重试

- 数据库和 Provider/Git 依赖通过 LangGraph runtime context 注入，不写进 checkpoint；runtime 和 budget 使用同一 worker lease。
- 中断恢复保留准备阶段的 PR、SHA、diff 和计划。临时仓库不存在时按已保存 PR SHA 重新克隆，不获取当前 PR 替代旧范围；图 checkpoint 引擎不一致会拒绝。
- 新引擎保留终态图 checkpoint 供缺口重试；旧引擎保留原清理行为。
- 绝对运行截止时间保存在 runtime CAS 中，恢复和缺口重试不重新获得整份运行时间；期限结束后仅保留对账和合法历史。
- 单 Unit 重试 API 返回专用 TaskRuntimeRetryResult，只排明确 pending/blocked 缺口，未知请求不作为免费重试；重试不重跑已完成目标，也不重置账本、纠错或合法历史。响应包含实际缺口 work_item_id，前端同步支持新旧两种返回。
- 用户取消事务撤销 worker 权限，未结算在途保守记 unknown，释放未转换保护，保存可读取消报告；取消后旧 worker 的迟到写回被 CAS/取消边界拒绝。未收到可信 usage 时 total_tokens 保留 null。

## 报告和前端

新增 task_runtime_report，coverage_version=`task-goals-v1`，分开执行状态、终止原因、固定范围、调查答案、未调度线索、confirmed/dismissed/unresolved 候选和计量完整性。Manifest 标注 engine、coverage_version、usage_integrity，并直接采用任务级范围和唯一账本数据；恢复重放保留 Provider usage 记录，不伪造新模型调用。

新引擎 Markdown 报告与前端概览/检查面板展示这些维度和缺口重试，不使用旧协调的绿色完成状态。旧任务缺少新增字段时继续显示 legacy；旧 Unit 缓存没有新成功升级路径。空 findings 仍保留范围和缺口说明。

实时 task_runtime_progress 通过单步队列输出并持久化供 SSE 查询；新引擎报告尚未生成时显示待记录，不使用旧缓存覆盖率。

配置新增 `REPOGUARDIAN_TASK_REVIEW_TIMEOUT_SECONDS=1800`，与旧单 Unit 180 秒期限分别说明。`.env.example` 和 README 同步，用户已有配置与说明修改保留。

## 验证

新增真实 Git fixture、SDK ainvoke 替身、SQLite SideEffect/Artifact、真实 LangGraph 节点及服务 stream 的 P4 回归。验证当前 Prompt 原文、共享账本、重启不发送、冻结配置、非文本分母、旧读取、固定快照缺口重试、取消与评测路由。34 项入口/持久化聚焦测试通过；此前 P1–P3 和报告回归通过。前端新增版本和 unknown 重试边界测试，23 项通过，vue-tsc/Vite build 通过。

全回归曾暴露旧协调中断测试的时序假设：SDK 进入等待不代表异步进度已写入任务快照。现明确等待计划进度落库后注入 shutdown，继续验证原有恢复、不重发和 unknown 断言；13 项新旧入口聚焦复验通过。

完整后端回归通过：1186 passed、2 skipped，216.27 秒。最后补充的评测代理浅拷贝/冻结窗口修正经过 P4、旧评测和预算评测共 31 项复验，全部通过。Ruff 与 `git diff --check` 通过。前端 23 项测试和 vue-tsc/Vite build 通过。未进行浏览器视觉验收。

没有付费模型调用、目标仓库动态执行或远端推送。P5 真实实验及 P0 剩余语义质量核验仍未完成，不能由协议测试推导缺陷质量收益。
