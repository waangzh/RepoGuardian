# RepoGuardian

> 基于证据、理解仓库上下文、严格只读的 AI Pull Request 代码审查 Agent。

输入 GitHub PR URL，先了解变更并确认审查方案，再由 Agent 探索关联代码、核验候选问题，输出可追溯的审查结论、覆盖与限制，以及可复制到 PR 的交付摘要。

**RepoGuardian Server 永远不执行目标仓库代码。** 默认进行只读静态审查；测试与构建需另行配置 GitHub Project CI 或外部 User Runner。

[快速开始](#快速开始) · [如何使用](#如何使用) · [开发与验证](#开发与验证) · [反馈问题](https://github.com/waangzh/RepoGuardian/issues)

## 项目特点

- **先了解变更，再决定如何审查**：首先从 GitHub 获取变更概览，无需等待克隆和索引；准备完整方案后选择深度、重点、范围、模型与限额，确认前不调用模型。
- **区分作者目标与实际行为**：结合 PR 描述和只读代码搜索，检查声明的目标与实际变更是否吻合；摘要保留证据和未确认项，不用作者描述替代代码事实。
- **目标驱动的分工检查**：将变更规划为审查目标与工作项，在固定快照上独立检查；对证据缺口派生受限调查与独立核验。
- **证据与人工处理分别记录**：候选问题经过证据解析、策略检查和独立核验；可查看代码位置、依据与限制，并填写原因进行人工确认、忽略或标记已处理。
- **结论、限制与交付可追溯**：分别呈现执行状态、问题证据与风险，提供 PR 交付摘要、详细报告和技术审计；任务与检查记录持久化，恢复时复用兼容的已完成结果。

可识别 Python、TypeScript/JavaScript、Java、Go 和 Rust，均支持 Tree-sitter L2 声明、导入与调用语法索引。解析器不可用或源码存在语法错误时显式降级到 L1 启发式索引，文件索引保留诊断；L2 不代表编译器级类型绑定或调用关系已核验。

## 快速开始

准备 Git、[uv](https://docs.astral.sh/uv/getting-started/installation/)、Python 3.12+、Node.js（推荐 22+）和 npm，以及一个 OpenAI 或 OpenAI 兼容服务的 API Key。

### 1. 启动后端

```sh
git clone https://github.com/waangzh/RepoGuardian.git
cd RepoGuardian
```

复制配置文件：

**Windows（PowerShell）**

```powershell
Copy-Item .env.example backend\.env
```

**Linux（Bash）**

```bash
cp .env.example backend/.env
```

安装后端依赖：

```sh
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

然后在 `backend/` 目录下执行迁移并启动服务。

```sh
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

API 默认运行在 `http://127.0.0.1:8000`。访问 [健康检查](http://127.0.0.1:8000/health)，应返回 `{"status":"ok"}`；接口文档见 [Swagger UI](http://127.0.0.1:8000/docs)。

### 2. 启动前端

在另一个终端中，从克隆后的 `RepoGuardian` 仓库根目录执行：

```sh
cd frontend
npm install
npm run dev
```

打开 Vite 输出的地址（默认 `http://localhost:5173`），应看到 **新建审查** 页面。前端开发服务器会将 `/api` 和 `/health` 请求代理到本机的 `8000` 端口，后端需保持运行。

## 如何使用

1. 在 **新建审查** 输入可访问的 GitHub PR URL，点击 **预览 PR 变更**。这一步只读取 GitHub 元数据与文件统计，不克隆、不索引、不调用模型；概览最多显示 100 个文件，列表不完整时会提示。
2. 点击 **准备完整审查方案**，查看仓库准备、索引和方案生成进度，再选择审查深度、重点、全部变更或自定义文件/模块，以及执行模型、调用次数、Token 和运行时间上限。完整预览仍不调用模型，可取消；本浏览器刷新后可恢复查询进度。
3. 修改配置后点击 **更新方案**（页面会就地显示服务端准备进度），再点击 **开始 AI 审查**。待确认方案有效期为 15 分钟，绑定 PR 快照与配置；确认时会重新读取 PR 元数据校验，匹配后直接使用保存的方案。PR 提交、标题、正文或关键配置变化时，须重新预览。
4. 在 **结果概览** 先看结论与下一步行动，再查看作者目标、实际变更和证据限制。主导航以 **结果概览**、**问题与证据**、**审查报告** 为主要任务；**覆盖与限制**、**变更文件**、**技术记录** 归入“补充证据”，用于追踪未检查目标、代码 Diff、关联调查与实际用量。**交付到 GitHub** 是操作入口，从审查报告的 PR 评论草稿进入，不与静态信息页平级。
5. 审查结束后，在 **问题与证据** 结合代码依据复核发现，填写原因后可执行 **人工确认**、**忽略**、**已处理** 或 **重新打开**。人工处理记录不改变机器证据状态或 GitHub 发布资格。
6. 在 **审查报告** 切换 **PR 评论草稿**、**详细审查报告** 与供排障追溯的 **技术审计导出**（区别于主导航查看运行步骤与计量的 **技术记录**），复制或导出 Markdown。评论可选择简洁或详细模式；高优先级问题始终保留证据和触发场景，审查缺口会前置说明。PR Summary 专用于 GitHub 发布流程。在 **审查历史** 中可按 PR 链接、仓库、标题或任务 ID 搜索并重新打开记录。

审查自动执行至最终报告，不会暂停等待人工回答。证据不足、验证失败和缺少业务规则的内容保留为未确认问题或覆盖缺口；由你在原 PR 决定是否接受、修改或合并，也可自行将报告复制到评论。

正式审查默认参考同一 PR 的普通评论、Review 正文和行内讨论；预览阶段不调用摘要模型。
历史原文按任务冻结，过长讨论按预算摘要，并支持回读来源。平台“已解决”或作者声称修复不代表代码已核验。
历史问题复核与当前 Diff 结论、覆盖率分开；历史未核验项单独显示。重复意见仍保留风险和发布资格，
默认不选入评论，可手动纳入。采集受 30 秒、500 个讨论根、2,000 条评论和 8 MiB 限制，缺失与截断会提示。
摘要最多两次且不超过任务调用及 Token 上限的 10%，历史核验使用剩余预算。
服务端可用 `REPOGUARDIAN_PR_DISCUSSIONS_ENABLED=false` 对新任务关闭；旧任务恢复不升级输入协议。
设置页可保存“审查时参考 PR 历史讨论”的浏览器默认偏好，单次方案可在高级设置覆盖；
关闭时不采集、不向模型提供也不复核历史讨论，结果会明确本次未纳入。选择随方案冻结，
修改默认偏好不影响已有任务；服务端禁用时不能由用户开启。
PR 评论默认保留已核验的当前风险，普通未核实历史讨论留在详细报告；
评论页可选择展示简短历史核实补充摘要，具有明确当前风险线索的待核实项仍保留提醒。
新审查可在已有作者目标对照中生成中文目标归纳；无法归纳时明确保留标题或描述来源。
评论分别展示实现变化与测试代码静态核对，测试是否执行以绑定当前提交的验证记录为准。

大型仓库首次准备完整方案仍需克隆和建立索引；相同固定快照的成功准备结果可复用。准备缓存保留 24 小时并受容量限制，未命中或校验失败时会重新准备；已保存方案的确认不依赖准备缓存，也不会再次克隆、索引或规划。方案仍遵守 15 分钟确认期限。浏览器存储被禁用或清除时，无法在刷新后自动找回完整预览。

升级时先停止后端，在 `backend/` 执行 `uv run alembic upgrade head`，再启动后端。历史审查记录可继续查看；旧的未消费预览若缺少当前校验信息，需要重新生成方案。

### 审查方案与默认偏好

| 审查深度 | 检查策略 |
| --- | --- |
| 快速初筛 | 聚焦直接变更的行为，限制扩展调查，先做快速筛查。 |
| 标准审查 | 检查变更行为、调用关系与关联契约，按具体线索继续调查。 |
| 深度核验 | 扩大调查容量，主动寻找反证，检查边界条件、兼容性与相关测试代码。 |

所有深度均保留独立证据核验，不执行目标仓库代码。深度卡片展示调用次数、Token 和时限上限；这些是上限，不是预计消耗。切换单次方案的深度会应用当前浏览器保存的对应配置，未自定义字段使用服务端预设，仍可进一步调整并重新校验保存。

审查重点支持正确性、安全、性能、兼容性及全部，影响检查优先级与模型指令，不会隐藏其他类别的具体缺陷。自定义文件或模块只控制主审目标，未选变更会标记未主审；关联上下文仍可在受控的只读权限内读取，敏感路径和越界链接继续拒绝。

限额由后端执行，覆盖检查、调查、核验与重试。达到调用、Token、费用或运行时间限制时，保留已完成成果，并返回未检查范围与终止原因；恢复和重试不重新延长运行期限。费用上限通过服务端配置，当前界面不提供费用设置。

在 **系统设置** 点击展开 **默认审查深度**，可选择快速初筛、标准审查或深度核验，初始为标准审查。每档可分别编辑模型调用次数、Token 和运行时间上限，点击 **保存此档** 生效；留空使用对应服务端预设，**恢复预设** 仅重置该档。调用次数和 Token 可为 0，运行时间须为正整数，不能超过当前服务端上限；服务端收紧上限后，超限配置须修改或恢复后才能带入新方案。历史讨论审查使用开启／关闭选项，服务端未开放时为关闭。主题、信息密度和审查偏好仅保存在当前浏览器，不跨设备同步；默认偏好用于新方案首次预览或主动切换深度，单次方案中的调整不会改写默认偏好，设置变化也不影响已有方案和任务。浏览器存储不可用时保留本页会话内配置并提示。**更新方案** 保存的是本次 PR 的待确认方案，不是永久复用的方案模板。

### 交付到 GitHub（可选：复制评论 / 直接发布）

默认关闭。仅支持可信的本地单用户部署，发布接口要求本机连接及独立 Bearer 操作口令，不支持直接公开到公网。操作口令仅在页面内存中临时使用，不是 GitHub PAT。

1. 在 `backend/.env` 中配置独立的 `REPOGUARDIAN_GITHUB_PUBLISH_TOKEN`（Fine-grained PAT，仅授权目标仓库的 **Pull requests: write**）、数字仓库 ID allowlist、PAT 用户数字 ID，以及至少 32 字符的 `REPOGUARDIAN_PUBLICATION_API_TOKEN`。不会回退使用只读 `GITHUB_TOKEN`。
2. 在 GitHub 核对该 PAT 的权限后设置 `REPOGUARDIAN_GITHUB_PUBLISH_PULL_REQUESTS_WRITE_CONFIRMED=true`，再启用 `REPOGUARDIAN_GITHUB_PUBLISH_ENABLED=true`。预检还会读取 GitHub 身份与仓库写角色；仓库角色不等于 Token grant，实际写入的 401/403 仍会被明确拒绝。完整配置见 [环境变量示例](.env.example)。
3. 在 `backend/` 执行 `uv run alembic upgrade head`，再重启后端。在终态审查页进入 **交付到 GitHub**。未启用发布时页面只提供 **复制评论**（写入剪贴板，不写入 GitHub）。启用后输入操作口令、选择问题并预检；确认总结评论创建/更新、审查版本、代码行定位和已有评论后，二次确认写入。
4. 发布状态与 Review Outcome 独立。可通过发布历史重新查看状态及 GitHub 返回的链接。`uncertain` 使用 **检查远端发布状态**：先核对已发送批次，无法确认时保留未知并要求人工处理，不盲目重发 POST。

PR 总结评论复用同一 PR、同一发布身份的主评论；代码行评论按同一 Head 的严格内容和定位去重。过期 Base/Head、关闭 PR、未知作者或手工修改的摘要会阻止写入。无法精确映射到代码行的已核验问题会写入总结评论，并需要明确确认部分发布。跨系统写入并非原子事务，不承诺 exactly-once；API 版本复查后仍存在短暂远端竞态窗口。

### 结果解读与报告交付

概览的结论分为 **本次检查未发现需在合并前处理的问题**、**发现改进建议，暂无高优先级问题**、**发现高优先级问题，建议修复后复审**、**还有约定检查未完成**、**疑点需要进一步确认**，以及动态检查未通过、审查中断和用户停止等情景。标题说明审查事实与覆盖范围，下一步建议单独给出；否定结论始终带“本次检查”或“已检查范围内”的限定。高优先级问题按 RepoGuardian 审查策略建议在合并前处理，不代表 GitHub Mergeability 或分支保护会阻止合并。它们是基于当前快照的审查建议，不代替测试、业务验收或人工合并决定。完整词表与替换规则见 [面向用户的状态语言规范](STATUS-LANGUAGE.md)。

- **覆盖情况**：覆盖按审查目标统计；只有该文件的全部目标均为 `examined` 才计为已审查。目标完成比例不是代码或测试覆盖率。`partial`、`blocked` 表示目标尚未完整检查；`completed_with_warnings` 还需结合具体原因核对，不能直接推断范围完整或证据充分。
- **问题与检查状态**：`checked` 仅表示检查过，零 Issue 不代表代码正确；缺失证据、未执行补查和未知结果需要人工复核。
- **调用与成本**：初始预览分组与资源上限不代表最终调用数或费用；实际用量还包括派生调查、核验与重试。未配置模型价格或实际用量缺失时，费用保持未知，不能视为零成本或最终账单。

| 报告类型 | 适用场景 |
| --- | --- |
| PR 交付摘要 | 面向 PR 作者和 Reviewer，包含变更概述、问题、未决事项与建议；可选择精简或详细内容。 |
| 详细审查报告 | 复核变更理解、问题依据、触发场景、影响与建议，并跳转到代码和证据。 |
| 技术审计 | 排查执行、目标、调查和证据记录，查看结构化摘要并按需展开原始字段。 |

新生成的 PR 变更概述只说明主要改动、适用条件和行为效果，不固定追加测试变化、测试函数名或断言清单；以测试改动为主的 PR 可在同一概述中说明其目的。测试源码观察保留在内部详情中，实际执行情况由验证记录单独说明。已冻结的发布计划保留原正文，避免恢复或重试时静默改写。

编辑摘要经过结构、来源引用和快照绑定检查，不用关键词推断自然语言结论是否成立，也不代表每句话已经完成源码语义核验。严重度、测试状态和合并判定仍由权威审查记录提供；摘要中的外部文本在输出时转义，预览继续经过 HTML 清洗。

复制或导出 **PR 交付摘要** 前会核对远端 Head；有新提交或无法确认时，需明确确认并保留快照限制说明。详细报告支持历史导出。人工处理、机器证据与 GitHub 投递分别记录，人工确认不会自动将候选问题变成已核验或已发布。

## 配置

配置写入 `backend/.env`，修改后重启后端；完整变量说明见 [环境变量示例](.env.example)。以下为常用配置：

系统设置页也可以在本地单用户部署中修改补丁大小、记录保留天数，以及 OpenAI-compatible 模型服务 URL 和默认模型名；API Key 仍只从后端环境变量读取，不在页面显示。修改后只影响新建审查，已有任务继续使用冻结配置。

```env
GITHUB_TOKEN="" # GitHub 读取凭据，私有仓库需授权访问；与发布凭据分开
REPOGUARDIAN_PROVIDER=openai # 模型供应商标识，与所用服务匹配
OPENAI_API_KEY=your-api-key # 模型服务的 API Key
OPENAI_BASE_URL=https://api.openai.com/v1 # 可替换为 OpenAI 兼容服务地址
REPOGUARDIAN_MODEL=gpt-4.1-mini # 使用该服务实际支持的模型名
REPOGUARDIAN_RUN_MAX_CALLS=200 # 整次运行的模型请求尝试上限，包含核验与重试
REPOGUARDIAN_RUN_MAX_TOKENS=1000000 # 整次运行的累计 Token 上限
REPOGUARDIAN_TASK_REVIEW_TIMEOUT_SECONDS=1800 # 整次运行时限，单位为秒
REPOGUARDIAN_MODEL_REQUEST_TIMEOUT_SECONDS=60 # 单次模型 HTTP 请求超时，单位为秒
REPOGUARDIAN_MODEL_REQUEST_PROFILES={} # 按模型名覆盖单次请求窗口，默认使用自动解析
REPOGUARDIAN_DEFAULT_VALIDATION_BACKEND=none # 默认不发起外部动态验证
REPOGUARDIAN_INCREMENTAL_REVIEW_ENABLED=true # 复用前序未变更文件的 Unit 结论
REPOGUARDIAN_INCREMENTAL_REVIEW_WEBHOOK_ENABLED=false # synchronize 事件自动创建增量审查
REPOGUARDIAN_INCREMENTAL_REVIEW_WEBHOOK_REPOSITORIES= # 自动审查仓库 allowlist，如 owner/repo
REPOGUARDIAN_GITHUB_PUBLISH_ENABLED=false # 手动发布需另配独立凭据、范围与操作口令
REPOGUARDIAN_LANGSMITH_TRACING=false # 按需启用 LangSmith 追踪，需另配追踪凭据
REPOGUARDIAN_LANGSMITH_INCLUDE_CONTENT=false # 启用追踪时，默认不上传代码、提示词和模型输出
```

- **模型窗口与运行预算是两个概念**：接入自定义模型或非标准服务地址时，可用 `REPOGUARDIAN_MODEL_REQUEST_PROFILES` 按实际模型名指定窗口，或用 `REPOGUARDIAN_MODEL_REQUEST_PROFILE` 设置通用窗口。`context_window` 是单次请求可容纳的 Token 数；`input_output_shared=true` 表示输入与输出共用该窗口，需要为输出和安全余量预留空间。窗口应按供应商实际限制设置，调大整次运行预算不能解决单次请求超长。
- **预算包含核验与重试**：`REPOGUARDIAN_RUN_MAX_CALLS` 按实际发送尝试计数，`REPOGUARDIAN_RUN_MAX_TOKENS` 覆盖整次运行，`REPOGUARDIAN_TASK_REVIEW_TIMEOUT_SECONDS` 限制运行时间。各深度据此生成资源预设，单次方案不能超过服务端上限；恢复或重试继续使用原预算和期限。
- **价格与费用上限使用不同单位**：`REPOGUARDIAN_MODEL_PRICING_JSON` 按 `provider:model` 配置输入、输出及可选缓存输入价格，单位为美元/百万 Token；`REPOGUARDIAN_RUN_MAX_COST_MICROUSD` 则以微美元设置费用上限，1 USD = 1,000,000 微美元。启用费用上限需要可信的模型价格；实际用量缺失时费用保持未知，不能当作零成本。
- **选择验证后端不等于执行端已就绪**：`REPOGUARDIAN_DEFAULT_VALIDATION_BACKEND` 默认是 `none`。改为 `project_ci` 或 `user_runner` 后仍需配置对应工作流或外部 Runner；不可用时不会回退到 RepoGuardian Server 执行目标仓库代码。CI 状态与审查完成状态分别记录。

## 工作流程

![RepoGuardian 架构图](https://github.com/user-attachments/assets/e405f29f-0587-43b1-aac8-1f2a20ab1066)

读取 PR 变更概览 → 准备仓库与索引 → 预览分组并校验保存方案 → 确认方案与 PR 快照 → 按冻结配置执行目标检查、关联调查和独立核验 → 返回结论、覆盖与限制、实际用量与报告。

审查 Diff 使用 PR 的唯一 merge base 到 Head 的三点比较，避免把 Base 分支独有提交混入本次变更。无法确定唯一 merge base 时明确失败，不退化为两点比较；审查记录保留声明 Base 与实际 Diff 基准，便于追溯。

同一 PR 再次审查时支持**增量审查**：当前序 Head 是当前 Head 的祖先、内容基准与审查策略一致，且 PR 描述未变化时，可复用前序完整且无待复核问题的目标覆盖记录。主审文件、关联文件、实际读取依赖或相关图关系发生变化的 Unit 会重新审查；变化路径包含已经恢复到 Base、因而不再出现在完整 PR Diff 中的文件。复用仍保留逐 hunk 覆盖分母和来源任务，不重复计算前序模型用量。前序存在问题或关联调查、使用过仓库搜索或符号发现、缺少依赖记录、本次包含历史讨论、自定义文件范围、历史改写、内容基准或审查策略变化时，会按适用范围重新检查或退回完整审查，并在预览中说明原因。前序问题不会自动沿用已确认或可发布状态。可用 `REPOGUARDIAN_INCREMENTAL_REVIEW_ENABLED=false` 关闭。

配置 `REPOGUARDIAN_INCREMENTAL_REVIEW_WEBHOOK_ENABLED=true`、`REPOGUARDIAN_GITHUB_WEBHOOK_SECRET` 与仓库 allowlist 后，GitHub `pull_request synchronize` 事件会通过 `POST /api/incremental/webhook` 自动创建审查，并在满足条件时采用增量复用；同一 delivery 幂等，fork PR、草稿与已合并/关闭 PR 不触发。事件中的仓库、PR 地址和 Base/Head 必须与确认快照一致，过时事件不会审查另一个 Head；回执的 `completed` 表示任务创建完成，不代表审查完成。

动态验证独立于审查流程，Review 完成时 CI 仍可能运行中。新请求使用 `review`；旧 API 模式值仅作兼容，仍按只读审查处理。

## 安全边界

- 模型只能使用受限的只读工具，文件读取经过路径、符号链接、敏感文件与预算校验；补查不能绕过这些边界。
- RepoGuardian 不执行目标仓库的安装、测试或构建，不自动 commit、push、创建 PR。GitHub 评论写入只由独立 Publisher 在用户预检和确认后执行，Review Agent 没有写权限。
- 动态验证只通过显式配置的 Project CI / User Runner 发起，不接受模型生成的 shell 命令；执行端不可用时不会回退到宿主机。Fork PR 默认不触发 Project CI。
- 审查所需的代码内容会发送给配置的模型服务。使用前确认模型服务、外部执行端与凭据的数据边界。
- 本地 SQLite 与运行产物用于持久化和恢复，不构成多租户或生产级安全隔离。

## 当前限制

- 只支持 GitHub PR 输入；可显式配置本地单用户手动 Summary / Inline 发布，不自动 Approve / Request Changes，不创建 Check Run 或修复 PR。
- 不提供本地 Sandbox 或通用命令执行能力。
- 超大变更可能因输入、时间或模型预算限制而部分完成，需要结合覆盖范围复核。
- 尚未完成真实样本上的协作效果对照与人工核验，不承诺召回、误报或成本收益。

## 开发与验证

后端使用 Python、FastAPI 与 LangGraph，前端使用 Vue 3、TypeScript 与 Vite。先按快速开始安装依赖；本地开发分别运行后端与前端，代码修改后会自动重载。

检查命令如下，从仓库根目录开始执行；按改动范围选择适用项：

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

- 后端逻辑变更运行相关 pytest 测试与 Ruff，可在 `uv run pytest` 后指定测试文件；前端变更运行相关测试与 `npm run build`（包含类型检查）。
- 默认选择与改动直接相关的测试；文档变更核对命令、字段、链接和最终差异，无需运行业务测试。上面的 `uv run pytest` 不带路径时会执行完整后端测试集。
- 修改配置时同步 `.env.example`；修改 API 响应时同步前端类型与调用方，覆盖输入校验、拒绝路径和旧数据兼容性。
- 开发测试可执行本项目代码，RepoGuardian Server 仍不得执行被审查的目标仓库代码。
- 不提交 `.env`、API Key、Token、数据库或任务运行产物。

欢迎通过 Issue 或 Pull Request 反馈问题、改进文档与代码。提交 [GitHub Issue](https://github.com/waangzh/RepoGuardian/issues) 时附上复现步骤、预期与实际结果，以及脱敏后的错误信息。

## 许可证

MIT
