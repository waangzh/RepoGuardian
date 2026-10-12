# P1：最小审查协议垂直切片

范围：`task_review_v1` 单 worker、单 inspect 的离线切片。生产默认仍为 `legacy_units`；本阶段不启用真实付费评测、不切换默认值。

## 实现与入口

- `models/review_runtime.py` 定义冻结 RunSpec、Goal、WorkItem、inspect 专用 submit 与互斥 read；模型没有父记录、question/obligation 更新或全局完成状态字段。
- `review_task_protocol.py` 使用 Pydantic schema 生成协议目录，实际 Prompt 包含固定范围、RunSpec hash、未完成目标和活动证据原文。
- `OpenAICompatibleProvider.task_review_step` 复用真实 `PreparedModelRequest → RequestFit → RunBudget.reserve → SDK → settle` 链；新路径不调用旧 `review_step`。
- `InspectTaskStep.advance` 每次只发送一个模型请求或执行一次现有 `ScopedContextTool.file_read`，工具结果进入现有 Evidence Store，然后原任务再次获得请求机会。P1 只接入 file_read；其余三种工具在完整运行时阶段接入。
- `ReviewTaskExecutor` 逐项校验、固定范围覆盖和结果投影。未知目标、活动集外证据、旧 base/head、正文 hash 错误、未覆盖完整 Goal 范围均拒绝；冲突重复目标不后写覆盖；原结果保留，候选即使独立检查项非法仍可单独验收。结果身份使用 run/work/attempt/item index；同一 attempt 重放不重复候选。
- 自动格式纠正每个逻辑 work item 一次；计数保存在 WorkItem 中，服务端反馈只要求重交被拒项。证据/安全拒绝不触发格式纠正。usage 在解析之前结算，非法 JSON 不免计费。
- `TaskReviewRuntime.advance_one` 是 P1 的离线单 writer 路由，领取一步后让出；失败、取消分别保留。并发公平轮转和预算 deferred 等属于 P2。

## 生产契约与兼容

`REPOGUARDIAN_REVIEW_ENGINE` 默认 `legacy_units`。请求可显式带 engine，创建任务时保存于 JSON snapshot，无新增数据库列/迁移。缺 engine 的历史任务仍为 legacy，热更新配置不改变已保存引擎，模型赋值及仓库 save 边界均拒绝切换。新字段同步前端类型、API 调用、`.env.example` 和 README。

P1 新引擎尚无生产仓库准备、持久化运行状态和取消恢复适配器。生产入口显式返回失败原因 `task_review_v1_production_entry_not_enabled`，不退回旧图，不发送模型；`retry_unit` 显式拒绝旧 Unit 重试。设置为新引擎不代表生产功能已启用。本阶段可执行入口是离线 `TaskReviewRuntime → InspectTaskStep`，仅用 SDK stub 验证。

## 验收证据

实际 SDK stub 替换 `ChatOpenAI.ainvoke`，其余 Provider 链使用生产代码；覆盖 read 请求、真实工具归档、下次 Prompt 原文、submit 校验、request/attempt 对应账本、正常结算、非法 JSON 结算、预算不足发送为零。工具动作没有新增付费请求。固定快照正文与工作树不符时，拒绝归档。

拒绝/回归覆盖：归档 ID、旧快照、未知目标、正文篡改、范围不全、read 越界、重复 JSON key、旧协议字段、矛盾目标、原结果保留、候选原始 item index、同 attempt 幂等、持久 engine 冻结、旧任务恢复、生产路由不回退、单 writer。

验证：最终完整后端 `uv run pytest -q -o cache_dir=.pytest-runtime-p1-final2` 为 1134 passed、2 skipped（189.32 秒）；`uv run ruff check .` 通过；前端 `npm test` 21 项通过，`npm run build` 类型/构建通过，阶段独立 index 的 `git diff --cached --check` 通过。先前路由/图回归 34 项通过；最终 SDK/协议/路由聚焦集 25 项通过，包含使用原持久化 fixture 验证引擎热配置与非法 save。未运行真实模型；SDK stub 结果不证明检出质量、成本或耗时收益。P0 质量标签分歧和遗漏核验仍保留为后续质量门缺口，见 P0 记录。

P1 退出审计：单 inspect read→submit、逐项接纳、旧协议字段拒绝、活动证据和快照/范围拒绝、真实 Provider prepare/send/parse 与预算链、单 worker 均有对应源码和 SDK stub 回归。生产入口未启用属于 P1 明确边界，P2–P4 接入后才可运行完整产品审查。阶段提交采用独立 index，只包含本阶段文件与 `.env.example`/README 的新增配置行；不混入先前用户暂存修改，不提交运行产物，不推送。

## 后续阶段

P2 接入单运行队列、Unit 分组公平轮转、完整目标/页规划、预算 revision、CAS/artifact、六个崩溃边界、取消和缺口重试；P3 接入调查/独立核验；P4 完成报告与 API；P5 按冻结条件做真实对照。生产默认在正式验收前继续 legacy。
