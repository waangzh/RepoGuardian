# RepoGuardian

> 基于证据、理解仓库上下文、严格只读的 AI Pull Request 代码审查 Agent。

输入 GitHub PR URL，预览变更并确认审查方案，再由 Agent 探索仓库上下文、核验候选问题，输出带代码证据、检查覆盖情况和实际用量的审查报告。

**RepoGuardian Server 永远不执行目标仓库代码。** 默认进行只读静态审查；测试与构建需另行配置 GitHub Project CI 或外部 User Runner。

[快速开始](#快速开始) · [如何使用](#如何使用) · [开发与验证](#开发与验证) · [反馈问题](https://github.com/waangzh/RepoGuardian/issues)

## 项目特点

- **先预览，再确认方案**：预览变更文件、初始审查分组与风险标签，选择深度、重点、主审范围、模型和资源限额；保存的方案绑定 PR 快照，预览阶段不调用模型。
- **结合仓库上下文**：Agent 使用只读搜索与文件读取理解关联代码；PR 标题和正文提供背景，但不能替代代码证据。
- **目标驱动的分工检查**：将变更规划为审查目标与工作项，在固定快照上独立检查；对证据缺口派生受限调查与独立核验。
- **证据与人工处理分别记录**：候选问题经过证据解析、策略检查和独立核验；可查看代码位置、依据与限制，并填写原因进行人工确认、忽略或标记已处理。
- **过程可观察、任务可恢复**：查看检查范围、未决问题、模型用量和耗时；持久化任务与检查记录，恢复时复用兼容的已完成结果。

可识别 Python、TypeScript/JavaScript、Java、Go 和 Rust。Python 与 TS/JS 使用 Tree-sitter，其他语言目前采用启发式索引，分析深度有所不同。

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

API 默认运行在 <http://127.0.0.1:8000>。访问 [健康检查](http://127.0.0.1:8000/health)，应返回 `{"status":"ok"}`；接口文档见 [Swagger UI](http://127.0.0.1:8000/docs)。

### 2. 启动前端

在另一个终端中，从克隆后的 `RepoGuardian` 仓库根目录执行：

```sh
cd frontend
npm install
npm run dev
```

打开 Vite 输出的地址（默认 <http://localhost:5173>）。前端开发服务器会将 `/api` 和 `/health` 请求代理到本机的 `8000` 端口，后端需保持运行。

## 如何使用

1. 在 **新建审查** 输入可访问的 GitHub PR URL，点击 **分析 PR**，查看仓库准备、索引、生成方案等阶段与耗时，再查看变更文件、风险标签和初始分组。预览在服务端执行，刷新后本浏览器会按保存的预览标识恢复查询；可显式取消。预览需要访问 GitHub，但不调用模型。
2. 在 **确认审查方案** 中选择深度、审查重点、全部变更或自定义文件/模块，以及模型调用次数、Token 和运行时间上限；可在 **高级模型设置** 中选择执行模型。
3. 修改配置后点击 **校验并保存方案**，再点击 **确认方案并开始审查**。保存的待确认方案有效期为 15 分钟，绑定 Head 与实际 Diff 基准 SHA；过期、快照或关键策略变化时须重新预览，不能直接使用旧方案启动任务。
4. 在 **结果概览** 查看进度和结论，通过 **覆盖与限制**、**变更文件** 和 **技术记录** 检查实际覆盖、证据、调查结果与未检查项。
5. 审查结束后，在 **问题与证据** 结合代码依据复核发现，填写原因后可执行 **人工确认**、**忽略**、**已处理** 或 **重新打开**。人工处理记录不改变机器证据状态或 GitHub 发布资格。
6. 在 **审查报告** 切换用户报告、PR Summary 草稿与完整审计，复制或导出 Markdown；在 **审查历史** 中按 PR 链接、仓库、标题或任务 ID 搜索全部历史并重新打开记录。

审查自动执行至最终报告，不会暂停等待人工回答。证据不足、验证失败和缺少业务规则的内容保留为未确认问题或覆盖缺口；由你在原 PR 决定是否接受、修改或合并，也可自行将报告复制到评论。

大型仓库首次预览仍需克隆和建立索引。相同仓库、Base/Head 与输入协议的成功准备结果可复用，调整方案或确认时仍会获取最新 PR 信息并重新校验。准备缓存保留 24 小时，最多 4 份、每份压缩存储不超过 32 MiB、展开不超过 512 MiB；缓存未命中或校验失败会重新准备，不代表审查失败。方案本身仍遵守 15 分钟确认期限。浏览器存储被禁用或清除时，无法在刷新后自动找回预览。

升级包含预览作业功能的版本时，先停止后端，在 `backend/` 执行 `uv run alembic upgrade head`，再启动后端；不要只刷新前端。旧审查记录与旧预览凭据保留。

### 审查方案与默认偏好

| 审查深度 | 检查策略 |
| --- | --- |
| 快速初筛 | 聚焦直接变更的行为，限制扩展调查，先做快速筛查。 |
| 标准审查 | 检查变更行为、调用关系与关联契约，按具体线索继续调查。 |
| 深度核验 | 扩大调查容量，主动寻找反证，检查边界条件、兼容性与相关测试代码。 |

所有深度均保留独立证据核验，不执行目标仓库代码。深度卡片展示当前服务端提供的调用次数、Token 和时限预设；这些是上限，不是预计消耗。切换单次方案的深度会应用对应资源预设，仍可进一步调整并重新校验保存。

审查重点支持正确性、安全、性能、兼容性及全部，影响检查优先级与模型指令，不会隐藏其他类别的具体缺陷。自定义文件或模块只控制主审目标，未选变更会标记未主审；关联上下文仍可在受控的只读权限内读取，敏感路径和越界链接继续拒绝。

限额由后端执行，覆盖检查、调查、核验与重试。达到调用、Token、费用或运行时间限制时，保留已完成成果，并返回未检查范围与终止原因；恢复和重试不重新延长运行期限。费用上限通过服务端配置，当前界面不提供费用设置。

在 **系统设置** 点击展开 **默认审查深度**，可选择跟随服务端默认或固定深度。主题、信息密度和默认深度仅保存在当前浏览器，不跨设备同步；默认深度用于新方案首次预览，单次方案中的调整不会改写默认偏好，也不影响已有方案和任务。**校验并保存方案** 保存的是本次 PR 的待确认方案，不是永久复用的方案模板。

### 手动发布到 GitHub（可选）

默认关闭。仅支持可信的本地单用户部署，发布接口要求本机连接及独立 Bearer 操作口令，不支持直接公开到公网。操作口令仅在页面内存中临时使用，不是 GitHub PAT。

1. 在 `backend/.env` 中配置独立的 `REPOGUARDIAN_GITHUB_PUBLISH_TOKEN`（Fine-grained PAT，仅授权目标仓库的 **Pull requests: write**）、数字仓库 ID allowlist、PAT 用户数字 ID，以及至少 32 字符的 `REPOGUARDIAN_PUBLICATION_API_TOKEN`。不会回退使用只读 `GITHUB_TOKEN`。
2. 在 GitHub 核对该 PAT 的权限后设置 `REPOGUARDIAN_GITHUB_PUBLISH_PULL_REQUESTS_WRITE_CONFIRMED=true`，再启用 `REPOGUARDIAN_GITHUB_PUBLISH_ENABLED=true`。预检还会读取 GitHub 身份与仓库写角色；仓库角色不等于 Token grant，实际写入的 401/403 仍会被明确拒绝。完整配置见 [`.env.example`](.env.example)。
3. 在 `backend/` 执行 `uv run alembic upgrade head`，再重启后端。在终态审查页展开 **发布到 GitHub**，输入操作口令、选择问题并预检。确认 Summary 创建/更新、Head、Inline 行映射和已有评论后，二次确认写入。
4. 发布状态与 Review Outcome 独立。可通过发布历史重新查看状态及 GitHub 返回的链接。`uncertain` 使用 **检查远端发布状态**：先核对已发送批次，无法确认时保留未知并要求人工处理，不盲目重发 POST。

Summary 复用同一 PR、同一发布身份的主评论；Inline 按同一 Head 的严格内容和定位去重。过期 Base/Head、关闭 PR、未知作者或手工修改的摘要会阻止写入。无法精确映射的已核验问题可降为 Summary-only，需要明确确认部分发布。跨系统写入并非原子事务，不承诺 exactly-once；API 版本复查后仍存在短暂远端竞态窗口。

解读结果时注意：

- **覆盖情况**：覆盖按审查目标统计；只有该文件的全部目标均为 `examined` 才计为已审查。`partial`、`blocked` 或 `completed_with_warnings` 表示仍有未完整完成的检查。
- **问题与检查状态**：`checked` 仅表示检查过，零 Issue 不代表代码正确；缺失证据、未执行补查和未知结果需要人工复核。
- **调用与成本**：初始预览分组与资源上限不代表最终调用数或费用；实际用量还包括派生调查、核验与重试。未配置模型价格或实际用量缺失时，费用保持未知，不能视为零成本或最终账单。

## 配置

配置写入 `backend/.env`，修改后重启后端；完整变量说明见 [`.env.example`](.env.example)。

- **模型窗口与运行预算是两个概念**：接入自定义模型或非标准服务地址时，可用 `REPOGUARDIAN_MODEL_REQUEST_PROFILES` 按实际模型名指定窗口，或用 `REPOGUARDIAN_MODEL_REQUEST_PROFILE` 设置通用窗口。`context_window` 是单次请求可容纳的 Token 数；`input_output_shared=true` 表示输入与输出共用该窗口，需要为输出和安全余量预留空间。窗口应按供应商实际限制设置，调大整次运行预算不能解决单次请求超长。
- **预算包含核验与重试**：`REPOGUARDIAN_RUN_MAX_CALLS` 按实际发送尝试计数，`REPOGUARDIAN_RUN_MAX_TOKENS` 覆盖整次运行，`REPOGUARDIAN_TASK_REVIEW_TIMEOUT_SECONDS` 限制运行时间。各深度据此生成资源预设，单次方案不能超过服务端上限；恢复或重试继续使用原预算和期限。
- **价格与费用上限使用不同单位**：`REPOGUARDIAN_MODEL_PRICING_JSON` 按 `provider:model` 配置输入、输出及可选缓存输入价格，单位为美元/百万 Token；`REPOGUARDIAN_RUN_MAX_COST_MICROUSD` 则以微美元设置费用上限，1 USD = 1,000,000 微美元。启用费用上限需要可信的模型价格；实际用量缺失时费用保持未知，不能当作零成本。
- **选择验证后端不等于执行端已就绪**：`REPOGUARDIAN_DEFAULT_VALIDATION_BACKEND` 默认是 `none`。改为 `project_ci` 或 `user_runner` 后仍需配置对应工作流或外部 Runner；不可用时不会回退到 RepoGuardian Server 执行目标仓库代码。CI 状态与审查完成状态分别记录。

## 工作流程

![RepoGuardian 架构图](https://github.com/user-attachments/assets/e405f29f-0587-43b1-aac8-1f2a20ab1066)

准备 PR 与仓库 → 解析变更、建立索引 → 预览分组并校验保存方案 → 确认方案与 PR 快照 → 按冻结配置规划目标与执行检查、派生调查和独立核验 → 返回覆盖、结论、实际用量与报告。

审查内容以 PR 声明 base 与 head 的唯一 merge base 为基准，等价于 GitHub PR 的三点比较；声明 base 仅保留 PR 身份。无法确定唯一 merge base（无共同祖先或存在多个候选）时明确失败，不退化为两点比较——两点比较会把 base 分支独有提交当作本 PR 的反向变更交给审查。生产、Preview、CLI 与评测共用同一 Diff 生成函数与策略版本，`run_manifest` 同时记录 `base_sha` 与 `effective_base_sha`。

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

提交变更前，从仓库根目录执行对应检查：

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

- 后端逻辑变更运行 pytest 与 Ruff；前端变更运行测试、类型检查与构建。
- 修改配置时同步 `.env.example`；修改 API 响应时同步前端类型与调用方，覆盖输入校验、拒绝路径和旧数据兼容性。
- 开发测试可执行本项目代码，RepoGuardian Server 仍不得执行被审查的目标仓库代码。
- 不提交 `.env`、API Key、Token、数据库或任务运行产物。

欢迎通过 Issue 或 Pull Request 反馈问题、改进文档与代码。提交 [GitHub Issue](https://github.com/waangzh/RepoGuardian/issues) 时附上复现步骤、预期与实际结果，以及脱敏后的错误信息。

## 许可证

MIT
