# RepoGuardian

> 基于固定源码证据、理解仓库上下文、严格只读的 AI Pull Request 审查 Agent。

输入 GitHub PR URL，选择范围和资源上限，即可启动审查。Reviewer 按需调查并即时保存候选，独立 Verifier 寻找反证、判断归因，最终提供问题、证据、运行范围和默认 LLM 变更总览。

**RepoGuardian Server 不执行目标仓库代码。** 不安装目标依赖，不运行目标测试、构建或脚本，不触发目标 CI，也不自动修复或写回补丁。

## 项目特点

- **确定性外层编排**：`ReviewOrchestrator` 管理有限工作项、并发、截止时间和恢复；LangChain `create_agent` 管理 Reviewer / Verifier 内部工具循环。
- **即时保留成果**：`submit_candidate` 在同一事务中保存候选和待核验工作项。后续 Reviewer 失败不会删除已经提交的候选。
- **来源与判断分离**：完整 Diff 行核对固定 Git blob 后可直接引用；搜索、关系索引、作者声明与讨论只是调查线索。`confirmed` 仍须经过服务端条件准入。
- **一个请求预算门禁**：模型调用前持久化预留，返回后幂等结算。并发 Reviewer 不能侵占已排队候选的最低核验机会；未知用量保守占用，默认总览单独保护一笔资源。
- **事实与表达分离**：先保存事实报告，再进行一次默认 LLM 总览。总览失败独立显示，不丢失问题、不改变事实终态，也不额外运行一致性 Agent。

支持 Python、TypeScript/JavaScript、Java、Go 和 Rust 的静态解析。Parser 按查询启动并复用已有隔离、缓存与降级能力；索引关系不等于编译器绑定，也不证明运行时路径。

## 快速开始

需要 Git、uv、Python 3.12+、Node.js 21+、npm，以及支持原生工具调用的模型服务。

```sh
git clone https://github.com/waangzh/RepoGuardian.git
cd RepoGuardian
cp .env.example backend/.env
cd backend
uv sync --extra test
```

在 `backend/.env` 配置模型：

```env
REPOGUARDIAN_PROVIDER=openai
OPENAI_API_KEY=your-api-key
OPENAI_BASE_URL=https://api.openai.com/v1
REPOGUARDIAN_MODEL=gpt-4.1-mini
```

在 `backend/` 初始化**新的空数据库**并启动：

```sh
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

默认数据库为 `backend/.repoguardian/reviews.db`，Schema 基线为 `20261031_0001`，不再使用独立 Checkpointer。若曾运行旧架构，先停止旧 Worker 与 Publisher，保存旧数据库、Checkpoint 和 Artifact，并核对所有 `uncertain` 发布；自定义数据库路径必须改到新的空库。旧数据库不会自动转换或删除，旧 Alembic 版本会被拒绝。不要用 `stamp` 伪装完成切换。

API 默认位于 `http://127.0.0.1:8000`；[健康检查](http://127.0.0.1:8000/health) 返回 `{"status":"ok"}`，[接口文档](http://127.0.0.1:8000/docs) 列出当前契约。

另开终端，在仓库根目录执行：

```sh
cd frontend
npm install
npm run dev
```

打开 Vite 输出的地址，默认是 `http://localhost:5173`。开发服务器将 `/api` 和 `/health` 代理到后端 `8000` 端口。

## 如何使用

1. 输入 PR 地址，选择 Quick / Standard / Deep、调查重点、文件范围及资源上限，直接创建审查。`POST /api/reviews/preview` 可选，不再强制预览确认。
2. 服务端固定数字仓库身份、声明 Base、唯一 merge base、Head 和计划成员；Reviewer 先处理高风险基础范围，并按需读取调用者、测试源码和相关 BASE 行为。
3. 候选保存后即可独立核验，不必等待所有 Reviewer 结束。核验结果为 `confirmed`、`refuted` 或 `insufficient`；缺失来源、必要字段或归因证据的确认不能成为可发布 Finding。
4. 在 **Overview / Changes / Findings / Activity / Publish** 查看总览、固定 Diff、来源、未决问题、工作项、材料缺口、用量和发布预览。人工注记与机器结论分开保存。
5. 完成工作项在恢复时复用，中断项从头重跑。重试沿用原预算和硬截止时间；总览可单独重试。中断的外部请求可能再次计费，不承诺模型执行 exactly-once。

创建请求必须带客户端生成的 `Idempotency-Key`。当前是本地单用户应用，键作用域为同一安装实例；相同键和规范化请求返回同一 Run，不重复创建收费任务。相同键但不同请求会冲突。主动重审必须使用新键。

执行状态为 `queued / running / completed / partial / failed / cancelled`。`completed` 表示计划所需材料已交付、调查正常结束且已准入候选都取得核验终态，**不表示穷尽所有缺陷**。`insufficient` 仍是未决问题；部分范围、失败、取消和未知用量不会显示成完整成功或零成本。

## 架构与运行边界

```text
固定快照 → 文件过滤 / 确定性初始规划
         → 正式任务按需语义分组 → 冻结 Unit / Hunk 成员
         → ReviewOrchestrator 有界派发
         → Reviewer + 按需 Context → 即时 Candidate
         → 独立 Verifier → 条件准入 Finding
         → 保存事实 → 默认一次 LLM 总览
         → 人工选择 / 预检 / 确认 → 独立 Publisher / Outbox / 对账
```

`app/review/` 中的 `domain.py`、`planner.py`、`context.py`、`agent.py`、`budget.py`、`store.py`、`findings.py`、`report.py` 和 `orchestrator.py` 是核心。现有 Git、DiffParser、Parser、源码安全读取、准备仓库、WorkerJob Lease、GitHub API 和 Publisher 继续复用。主 LangGraph 工作流、Goal / Assignment、固定角色份额、最终大提交和历史 Checkpoint 协议已经移除；LangChain 内部依赖的 LangGraph 仍由其依赖树提供。

Unit 以文件为完整性边界：1–3 个文件的小变更确定性分组；纳入文件达到 4 个且没有复用完成范围时，正式任务使用一次只含文件元数据和静态关系提示的 LLM 分组。预览不调用分组模型，因此不代表最终语义分组。结果经服务端去重、补齐遗漏、10 文件上限和上下文容量校验后，与工作项一起事务冻结；失败或恢复时已有请求状态不明，回退逐文件审查，不盲目重发收费请求。超大单文件保留一个 Unit，其材料继续分页交付，不因此丢弃变更。

累计输入/输出 Token 与整轮模型调用上限默认不设门禁；仍持久化每次请求的预留、实际用量和未知用量。可通过 `.env.example` 中的 `REPOGUARDIAN_REVIEW_MAX_*` 显式设置成本上限，已有运行保持创建时冻结的配置。单次上下文窗口默认 1M，须按实际执行模型能力配置；它不是建议填满的目标。单 Agent 调用/工具熔断、扫描量和总期限仍有效。连续工具轮次没有新信息时先警告，再关闭调查工具，最多允许两次受原预算约束的结构化收尾请求；仍不能交付结果时中断为未完成，实际证据缺口继续保留。已收到源码范围的重叠读取不算新进展；合法分页、BASE/HEAD 对照和新源码仍可推进调查。

Agent 保持 System Prompt 固定，预算、时间和收尾提示作为独立消息追加并保留在历史中，便于复用稳定的请求前缀。工具结果使用确定性压缩：首次源码正文保留，重复源码和检索结果引用此前收到的内容，证据句柄、侧别、行范围和分页控制信息仍可使用；完整来源继续归档。小 Unit 在收到全部变更材料、相关测试源码、搜索关联的消费者源码后会收到收尾提醒，仍可针对具体缺失前提继续补查。

活动页分别显示总输入、命中缓存输入、未命中缓存输入、输出和缓存命中率。缓存明细由供应商返回的用量计算；未返回明细或存在未知请求时，相应合计保持未知。命中缓存的输入仍计入整轮输入预算；Token 数量不等同于货币费用。

材料交付与工作项执行分别记录。只收到部分 Hunk、读取失败或未启动的范围保持不完整。结构性变更使用固定树条目作为变更材料，不把元数据当成缺陷源码证据；必要消费者仍由 Agent 调查。

总览只组织已经提供的固定来源与最终 Findings，不能新增裁决、改写严重程度或宣称执行过目标测试。引用有效不保证自然语言完全忠实；材料省略、失败和降级独立展示，发布预览保留人工检查。

总览材料优先取冻结计划中的实际 Diff 与对应 BASE/HEAD 原文，按审查单元分配容量后再补充相邻背景，不按调查来源 ID 的顺序截取。材料最多约 4500 个预估 Token，并扣除提示词和 Schema 开销以适配原有总览请求预留；具体省略范围保存在报告中。正文按“行为变化、直接效果、必要边界”组织，测试变化优先另段概括；段数和篇幅是写作目标，不是失败门槛。提示词、策略版本与材料共同决定复用身份；页面和发布正文支持安全行内代码与自然段。

对外报告先展示基于完整审查事实生成的结论，再展示变更总览。完整且无未决项、未发布问题或已确认问题时，建议进入正常合并评估，但仍须满足项目测试、CI 和审批；其他状态明确说明当前限制，不给出 clean 或无条件合并许可。选择发布哪些评论不改变总体审查结论。逐文件增删统计置于折叠详情。

## 增量审查

可启用签名校验的 `POST /api/webhooks/github`，仅处理 allowlist 中仓库自身分支的 `pull_request.synchronize`。Delivery 与同一 PR Base/Head 事件分别幂等，每个 PR 默认每小时最多自动创建 4 个 Run，限制原因会返回给调用方。新 Head 会停止旧运行，已提交事实继续保留，旧结果不能对新 Head 发布。

历史问题只是线索，进入本轮后要重新取得当前来源和独立核验。旧行号的内容不匹配时不把它重新包装为当前证据，不凭位置消失宣称已修复。

范围复用采用保守策略：相同基线与配置、可比较的 Head 历史、仅文档更新、完整 Hunk 成员一致且相关源码 blob 相同，才复用已完成范围，并显示来源 Run。源码、配置或测试变化优先调查变化单元并回退全量；基线变化、不可比较的历史和显式同快照重审也不复用。没有建设跨 Commit 语义依赖缓存。重新核验的历史主张进入总结，不猜测新的 Inline 行号。

## 交付到 GitHub

发布默认关闭，仅支持可信的本地单用户部署。Review Agent 没有 GitHub 写工具，Publisher 使用独立 PAT，绝不回退使用只读 `GITHUB_TOKEN`。

1. 配置独立 `REPOGUARDIAN_GITHUB_PUBLISH_TOKEN`、数字仓库 ID allowlist、PAT 用户数字 ID 和至少 32 字符的 `REPOGUARDIAN_PUBLICATION_API_TOKEN`。
2. 在 GitHub 核对 PAT 的 **Pull requests: write** grant 后，设置 `REPOGUARDIAN_GITHUB_PUBLISH_PULL_REQUESTS_WRITE_CONFIRMED=true`，再启用发布。完整字段见 [环境变量示例](.env.example)。
3. 在 Publish 页输入操作口令、选择 Finding 并预检，再查看实际 Summary / Inline、报告版本和部分发布提示，最后明确确认。操作口令只保留在页面内存，不是 GitHub PAT。
4. `uncertain` 必须先对账，不盲目重发 POST。Sticky Summary 不覆盖更新审查或人工编辑；精确 LEFT / RIGHT 映射失败时转入 Summary。

Publisher 独立复验规范化 Finding、核验准入、固定快照、来源内容和报告版本，不重跑审查。显式重生成总览会形成新的报告版本，已有预检与确认失效。发布状态与审查结论独立；远端短暂竞态和跨系统故障仍可能发生，不承诺 exactly-once。

## 配置与数据边界

[环境变量示例](.env.example) 是配置清单。模型端点、默认模型和记录保留天数可在设置页修改；已有 Run 的配置保持冻结。源码会发送到所配置的模型服务，使用前确认数据边界；LangSmith 默认关闭。

- 整轮成本主要由 `REPOGUARDIAN_REVIEW_MAX_INPUT_TOKENS`、`REPOGUARDIAN_REVIEW_MAX_OUTPUT_TOKENS` 和总期限控制；模型每次请求会获知扣除核验、总览保护份额后的共享余额，并在余额或时间接近边界时收尾。
- `REPOGUARDIAN_MODEL_CONTEXT_TOKENS` 限制请求上下文容量；预估包括消息、工具和结构化 Schema。使用分词估算，不能当成所有模型的精确计费上界。
- `REPOGUARDIAN_AGENT_MAX_CALLS` 与 `REPOGUARDIAN_REVIEW_MAX_MODEL_CALLS` 是调用次数熔断，默认分别为 48 与 64。quick/standard/deep 不再额外设置 6/12/20 次配额，深度只影响调查策略；显式配置更低上限仍会生效。
- `REPOGUARDIAN_AGENT_MAX_TOOL_CALLS` 继续限制单次 Agent 的工具调用，扫描量和工具超时约束保留；并发由 `REPOGUARDIAN_REVIEW_CONCURRENCY` 控制。
- 总时长由 `REPOGUARDIAN_REVIEW_TIMEOUT_SECONDS` 冻结。软截止停止新发现请求；硬截止阻止所有新模型请求，停止在途工作，只做合法的本地事实收尾。
- 请求前原子预留，未知用量保持未知并占用预留；恢复不清零。实际用量超过估算后如实记录，停止后续超预算请求。

SQLite 与本地 Artifact 是单用户持久化，不是多租户安全隔离。源码读取保留 tracked blob、路径、敏感文件、realpath / symlink、大小和调用预算约束；不向模型暴露 Shell、任意网络或发布权限。日志不输出源码、凭据和供应商异常正文。

## 开发与验证

后端使用 Python / FastAPI，前端使用 Vue 3 / TypeScript / Vite。开发测试可执行 RepoGuardian 自身代码，不能因此为产品增加目标代码执行权限。

```sh
cd backend
uv run pytest
uv run ruff check .
cd ../frontend
npm test
npm run build
cd ..
git diff --check
```

单元与集成测试覆盖固定源码、工具循环、预算、Lease、取消、恢复和 HTTP 模拟发布对账。模拟模型验证执行契约，不证明真实模型的误报、召回、成本收益或总览质量。本次未建设离线 Eval 平台；真实模型内容验收、浏览器验收、真实 GitHub 写入和具体数据库切换须单独进行。

阶段实施与调整记录见 [重构进度](docs/refactor-progress.md)。反馈问题时，通过 [GitHub Issue](https://github.com/waangzh/RepoGuardian/issues) 提供复现步骤、预期、实际结果和脱敏错误信息；不要提交 `.env`、Token、数据库或运行产物。

## 许可证

MIT
