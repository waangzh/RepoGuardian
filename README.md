# RepoGuardian

> 基于证据、理解仓库上下文、严格只读的 AI Pull Request 代码审查 Agent。

RepoGuardian 将 PR 拆分为边界明确的 Review Unit，独立探索上下文、记录实际检查范围，再通过跨 Unit 风险筛查与受限协调发现需要定向补查的契约问题。所有新增候选仍经过证据解析、策略检查和独立验证，最终输出 Issue、Coverage、协作记录和 Run Manifest。

**RepoGuardian Server 永远不执行目标仓库代码。** 测试、构建和运行时验证通过显式配置的 GitHub Project CI 或外部 User Runner 发起，与审查生命周期异步解耦；默认不启用动态验证。

[快速开始](#快速开始) · [工作流程](#工作流程) · [安全边界](#安全边界) · [反馈问题](https://github.com/waangzh/RepoGuardian/issues)

> [!IMPORTANT]
> 项目仍处于早期开发阶段。只读分析可识别 Python、TypeScript/JavaScript、Java、Go、Rust；Python 与 TS/JS 使用 Tree-sitter，其他语言按置信度降级到启发式索引。项目不提供本地 Sandbox，也尚未声明开源许可证。

## 核心能力

- **真正的 Unit Plan**：确定性拆分 Review Unit 后，模型输出结构化变更摘要、审查目标和风险假设；Plan 失败会降级为普通审查，不阻断任务。
- **结构化检查记录**：记录实际检查目标、假设核验、契约依赖、未决问题和证据引用；缺记录或非法引用保留为未知，未报告问题不等于代码正确。
- **证据化 Issue**：不直接信任模型行号，服务端解析 primary/supporting evidence、位置、来源、resolution status 和简洁审计摘要；Issue 只能锚定当前 PR 的可评论变更文件，不保存或展示隐藏 Chain-of-Thought。
- **Repository-aware 探索**：`file_find` / `code_search` 可发现整个安全的 Git-tracked repository；`file_read` 仍受敏感路径、realpath/symlink、大小、行数和 Unit 预算限制。
- **最小只读工具面**：Unit Agent 只使用 `file_find`、`code_search`、`file_read`、`file_read_diff`、`report_issue` 和 `task_done`；只有需要业务语义或安全决策时才使用结构化 `request_human` 控制动作。
- **分级多语言分析**：语言适配器统一产出符号、导入和调用引用；解析失败自动从 L2 降级到 L1/L0，Review Unit 仍可安全使用文件读取、路径查找和 diff 读取。
- **Selective Verifier**：确定性 evidence checks 优先，只对高风险、模糊、跨模块或低置信度问题追加模型验证；Verifier 不得提升 severity。
- **跨 Unit 定向补查**：确定性筛查输出 `required / uncertain / skip`，协调模型只提议计划，服务端校验范围、引用、重复任务和预算；补查候选按独立批次验证后参与全局汇总。
- **可审计、失败隔离**：Coverage / Run Manifest 分别报告 File Coverage 与 Unit Coverage，并记录 partial、终止原因、模型用量、耗时和确认问题；单个 Unit 或 Verifier 失败不会抹掉其他有效结果。
- **持久协调账本**：保存调用预留、计划、补查和验证批次，恢复时复用已完成结果；失败与传输重试计入共享预算，结果未知的调用不会自动重发。
- **独立 Project CI**：只发送服务端注册的 profile、request ID 和 SHA 绑定信息，不发送模型生成的 shell command；Fork PR 默认不 dispatch。

## 工作流程

![RepoGuardian 架构图](https://github.com/user-attachments/assets/e405f29f-0587-43b1-aac8-1f2a20ab1066)

当前主审查流程由 LangGraph 编排：接收 PR → 准备仓库、解析 diff、索引与项目检测 → 拆分 Review Units 并独立执行 → 解析证据、应用 Issue Policy、选择性验证 → 跨 Unit 风险筛查与受限协调 → 全局去重 → 生成报告。Unit 内部根据工具反馈探索上下文，主图负责调度、边界控制与结果聚合；协调模型负责提出需要跨组核验的问题。

Plan 是待验证的审查指导，不是已确认 Issue，也不是固定步骤队列。后续 Agent 可以根据工具反馈调整动作，并发现 Plan 之外的明确缺陷。

一个文件可能因大型 symbol/hunk 拆分而属于多个 Unit。只有所有 owning Units 都完整完成时，该文件才计为 `reviewed`；部分完成会标记为 `partial`。Unit 的状态和终止原因分开记录：模型、检索或诊断预算耗尽即使保留兼容状态 `completed`，也会被视为未完整完成，触发 warning 和 `completed_with_warnings`，并且不会进入 Evidence Pipeline、同任务 resume 成功集或跨任务复用缓存。

Preview 不调用模型、不运行目标代码，会显示文件和 Unit 范围、风险标签以及原始 Units 的三种调用口径：Plan 调用数、典型预计调用数和预算上限。跨 Unit 协调在审查后触发，可能产生额外调用，不包含在这份 Unit 预估中。

Project CI 是独立异步状态机：Review 可以先完成，Validation 随后处于 Pending、Running 或终态。RepoGuardian 会校验 repository、request ID、head SHA、patch SHA、workflow/run identity 和结构化结果。

### 跨 Unit 协作

当前默认自动执行风险筛查，并在需要时协调补查、合并验证结果，行为等同于 `auto`；当前版本没有 `off / shadow / auto` 模式选择或对应环境配置。

| 筛查结果 | 含义与后续处理 |
| --- | --- |
| `required` | 存在需要核验的契约、依赖或跨组覆盖疑点，生成受限补查计划；协调模型不能将其降级为跳过 |
| `uncertain` | 关系、索引或检查记录不足，进入受限关系判别；缺证据时保留未决 |
| `skip` | 当前规则允许跳过跨组协调，不代表整个 PR 已被证明安全 |

筛查结合当前变更、静态关联、Unit 声明的依赖和检查覆盖缺口；静态调用关系本身不等于本次变更存在契约风险，即使原始 Unit 没有报告 Issue，也可能需要补查。

协调最多提出 3 项补查，每项明确关联 Units、主文件、检查问题、反证目标和停止条件。服务端校验当前证据与关系引用、允许范围、重复任务和预算后执行，补查不能扩张到计划之外的读取范围。新增候选单独经过 Evidence → Issue Policy → Independent Verifier，随后进入全局去重；协调模型不能直接确认或删除问题。

协调、所有补查和补查 Verifier 共用任务级预算，默认上限为 **16 次模型调用、120,000 估算 token**，包含传输重试。失败、重启和恢复不返还已消耗预算；实际用量高于估算时补记。补查结果独立保存，不增加原始 Unit 覆盖率分母；零候选不构成反证，反证需要已完成的目标检查和相关 Unit 的证据。

### 检查记录与恢复

任务 API 返回结构化 JSON：`review_unit_results[].review_summary` 保存各 Unit 的检查记录与证据元数据，`cross_unit_risk` 保存筛查依据，`coordination_plan` 保存计划、预算和运行指标，`followup_results` 保存补查结果与批次验证状态。Markdown 报告用于阅读，结构化 JSON 用于程序处理；证据引用绑定 base/head SHA、文件、行范围、来源和内容哈希。

前端 **检查与协作** 页分别展示实际检查范围、风险理由、关联路径、补查问题、反证目标、候选处理状态和资源消耗，可以从补查发现跳转到代码证据或关联的原始 Unit。筛查判定、协调执行状态和 Issue 状态分别展示；`checked` 只表示检查过，模型置信度未经校准，缺失记录不会显示为已完成或无风险。

生产任务使用 SQLite 任务队列与 LangGraph checkpoint，协调调用和补查批次另有持久账本。恢复时复用已完成结果，不重复累加候选和指标；已发出但结果未知的调用保留未决。协调运行指纹绑定仓库快照、模型、协议版本和审查输入。页面区分本轮观测与任务累计预算；实际 usage 或成本缺失时保持未知。

## 快速开始

环境要求：Git、uv、Python 3.12+、Node.js 18.x、20.x 或 22+（与锁定的 Vite 6 兼容）、npm，以及一个 OpenAI 或 OpenAI 兼容服务的 API Key。以下命令使用 PowerShell。

### 1. 启动后端

```powershell
git clone https://github.com/waangzh/RepoGuardian.git
cd RepoGuardian
Copy-Item .env.example backend\.env
cd backend
uv sync --extra test
```

编辑 `backend/.env`：

```env
REPOGUARDIAN_PROVIDER=openai
OPENAI_API_KEY=your-api-key
OPENAI_BASE_URL=https://api.openai.com/v1
REPOGUARDIAN_MODEL=gpt-4.1-mini
```

```powershell
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

API 默认运行在 <http://127.0.0.1:8000>，完整接口见 [Swagger UI](http://127.0.0.1:8000/docs)。

### 2. 启动前端

在另一个终端中，从克隆后的 `RepoGuardian` 仓库根目录执行：

```powershell
cd frontend
npm install
npm run dev
```

打开 Vite 输出的地址（默认 <http://localhost:5173>）。前端开发服务器会将 `/api` 和 `/health` 请求代理到本机的 `8000` 端口，后端需保持运行。

### 3. 完成第一次审查

1. 输入可访问的 GitHub PR URL，先运行 **Preview**，检查文件范围、Review Units 和预计模型调用数。此步骤不调用模型，但需要访问 GitHub 并准备仓库。
2. 确认范围后启动审查，查看任务进度、Unit 状态与问题证据。
3. 在结果中结合 Evidence 和 Coverage 复核问题，打开 **检查与协作** 查看原始检查记录、跨组筛查与补查结论，再查看 Markdown 报告。出现 `completed_with_warnings` 时检查未完整完成的 Unit 和未决补查，不要把部分覆盖视为全部审查完成。

### 可选配置

配置写入 `backend/.env`，修改后重启后端；完整变量说明见 [`.env.example`](.env.example)。

| 场景 | 配置入口 | 默认行为 |
| --- | --- | --- |
| GitHub API 认证 | `GITHUB_TOKEN` | 未配置 Token |
| 兼容模型服务 | `OPENAI_BASE_URL`、`REPOGUARDIAN_PROVIDER`、`REPOGUARDIAN_MODEL` | OpenAI / `gpt-4.1-mini` |
| Unit 并发与超时 | `REPOGUARDIAN_REVIEW_UNIT_CONCURRENCY`、`REPOGUARDIAN_REVIEW_UNIT_TIMEOUT_SECONDS` | 并发 4，单 Unit 180 秒 |
| 模型用量成本 | `REPOGUARDIAN_MODEL_PRICING_JSON` | 未配置价格时成本未知；按 provider/model 设置每百万 token 的美元价格 |
| 外部动态验证 | `REPOGUARDIAN_DEFAULT_VALIDATION_BACKEND` 及对应 CI / Runner 配置 | `none`；需额外配置执行端 |
| LangSmith 追踪 | `REPOGUARDIAN_LANGSMITH_TRACING` | 关闭；启用后默认仍不上传审查正文 |

## 工作模式

| 模式 | 主 Review lifecycle | 目标代码执行 |
| --- | --- | --- |
| `review` | 严格只读静态审查 | 不执行 |
| `review_and_suggest` | 旧 API 兼容值；按只读审查执行并给出迁移警告 | 不执行 |
| `review_suggest_and_validate` | 旧 API 兼容值；按只读审查执行并给出迁移警告 | 不执行 |

动态验证通过独立的 `project_ci` 或外部 `user_runner` 边界发起，不属于上述 Review critical path。`gvisor` 仅为已废弃、不可执行的旧请求占位；任何后端不可用时都不会回退到宿主机执行。

## 安全边界

- 模型没有 shell、terminal、package manager、build 或 test 工具，也不能向 Project CI 提交命令文本。
- Repository discovery 可以覆盖安全的 Git-tracked 文件；内容读取仍经过 containment、realpath/symlink、tracked、sensitive-path 与预算校验。
- Sensitive-path policy 同时检查变更的新旧路径；例如将 `.env` 重命名为普通源码路径时，整个 diff 仍会在 Planner、Unit Executor 和 `file_read_diff` 三层被拒绝，不会发送给模型。
- Unchanged 文件可以成为 supporting evidence，但 Issue primary location 必须属于当前 Unit 的 changed/commentable files。
- 跨 Unit 补查使用关联 Units 的受限文件集合，计划引用必须匹配当前证据或索引关系，不能通过协调绕过敏感路径和读取预算限制。
- Git 命令使用参数化 argv，并隔离 host Git config、credential prompt 和 external diff。
- RepoGuardian 不 commit、push、创建 PR 或写回 GitHub 评论。
- 外部动态验证前应确认 Project CI / UserRunner 的执行环境与凭据边界。
- SQLite 和本地 artifact 提供恢复能力，但不构成多租户或生产级安全隔离。

## 当前限制

- 只接收 GitHub Pull Request URL；不同语言的索引深度仍有差异。
- 不写回 GitHub Review、Check Run、suggestion 或 Draft PR。
- 不提供 Local Sandbox、Docker/gVisor/Firecracker 或通用命令执行能力。
- Metadata-only semantic grouping 和公开 review benchmark 尚未实现；当前 planner 仍以确定性分组和安全 fallback 为主。
- 尚未完成真实样本上的协作效果对照与人工核验；不能据此声称跨模块召回提升、误报下降或成本收益。
- 模型结论仍需工程师结合 Evidence 和 Coverage 复核。

## 开发与验证

从仓库根目录执行：

```powershell
cd backend
uv run pytest
uv run ruff check .

cd ..\frontend
npm test
npm run build
```

配置项及默认值见 [`.env.example`](.env.example)。后端模型和前端类型必须保持同步，相关约束见 [AGENTS.md](AGENTS.md)。

阅读实现时可从以下入口开始：

| 入口 | 职责 |
| --- | --- |
| [`backend/app/graph/review_graph.py`](backend/app/graph/review_graph.py) | 主审查图与 Review Unit 阶段编排 |
| [`backend/app/services/review_service.py`](backend/app/services/review_service.py) | 任务创建、执行调度与恢复编排 |
| [`backend/app/services/review_unit_executor.py`](backend/app/services/review_unit_executor.py) | Unit 内的模型与只读工具反馈循环 |
| [`backend/app/services/cross_unit_risk.py`](backend/app/services/cross_unit_risk.py) | 确定性跨 Unit 风险筛查与关联依据 |
| [`backend/app/services/cross_unit_coordination.py`](backend/app/services/cross_unit_coordination.py) | 受限关系判别、计划校验、定向补查与批次验证 |
| [`backend/app/services/coordination_runtime.py`](backend/app/services/coordination_runtime.py) | 协调运行账本、共享预算与调用恢复 |
| [`backend/app/api/reviews.py`](backend/app/api/reviews.py) | Preview、任务查询、Unit 重试和进度接口 |
| [`frontend/src/App.vue`](frontend/src/App.vue) | 审查、历史记录、验证后端与设置页面入口 |
| [`frontend/src/components/review/ReviewViewer.vue`](frontend/src/components/review/ReviewViewer.vue) | 问题证据、覆盖导航与检查协作视图 |

遇到问题可提交 [GitHub Issue](https://github.com/waangzh/RepoGuardian/issues)，附上复现步骤、预期与实际结果，以及脱敏后的错误信息。提交代码变更前运行对应的后端检查或前端构建；不要提交 `.env`、密钥或任务运行产物。

## 许可证

当前仓库尚未包含 `LICENSE` 文件。在添加许可证前，请勿将代码视为已获得开源使用、修改或再分发授权。
