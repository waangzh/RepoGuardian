# RepoGuardian

> 基于证据、理解仓库上下文、严格只读的 AI Pull Request 代码审查 Agent。

输入 GitHub PR URL，先预览审查范围，再由 Agent 探索仓库上下文、核验候选问题，输出带代码证据、检查覆盖情况和协作记录的审查报告。

**RepoGuardian Server 永远不执行目标仓库代码。** 默认进行只读静态审查；测试与构建需另行配置 GitHub Project CI 或外部 User Runner。

[快速开始](#快速开始) · [如何使用](#如何使用) · [开发与验证](#开发与验证) · [反馈问题](https://github.com/waangzh/RepoGuardian/issues)

## 项目特点

- **先预览，再审查**：Preview 展示变更文件、审查单元（Review Unit）、风险标签和预计模型调用数，预览阶段不调用模型。
- **结合仓库上下文**：Agent 使用只读搜索与文件读取理解关联代码；PR 标题和正文提供背景，但不能替代代码证据。
- **分工审查与跨组补查**：将变更拆分为独立 Unit，记录检查目标；对契约依赖、结论冲突或覆盖缺口进行受限补查。
- **证据可追溯**：候选问题经过证据解析、策略检查和独立验证，结果展示代码位置、依据与处理状态。
- **过程可观察、任务可恢复**：查看检查范围、未决问题、模型用量和耗时；持久化任务与检查记录，恢复时复用兼容的已完成结果。

可识别 Python、TypeScript/JavaScript、Java、Go 和 Rust。Python 与 TS/JS 使用 Tree-sitter，其他语言目前采用启发式索引，分析深度有所不同。

## 快速开始

准备 Git、[uv](https://docs.astral.sh/uv/getting-started/installation/)、Python 3.12+、Node.js（推荐 22+）和 npm，以及一个 OpenAI 或 OpenAI 兼容服务的 API Key。以下命令使用 PowerShell。

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

API 默认运行在 <http://127.0.0.1:8000>。访问 [健康检查](http://127.0.0.1:8000/health)，应返回 `{"status":"ok"}`；接口文档见 [Swagger UI](http://127.0.0.1:8000/docs)。

### 2. 启动前端

在另一个终端中，从克隆后的 `RepoGuardian` 仓库根目录执行：

```powershell
cd frontend
npm install
npm run dev
```

打开 Vite 输出的地址（默认 <http://localhost:5173>）。前端开发服务器会将 `/api` 和 `/health` 请求代理到本机的 `8000` 端口，后端需保持运行。

## 如何使用

1. 输入可访问的 GitHub PR URL，点击 **Preview**，检查文件范围、Review Units 和预计模型调用数。预览需要访问 GitHub 并准备仓库。
2. 确认范围后启动审查，查看任务进度，并在 **审查** 中结合代码证据复核问题。
3. 打开 **检查与协作**，查看实际检查目标、跨组风险、补查结果和未决问题；在 **文件** 中检查覆盖范围。
4. 查看或复制原始 Markdown 报告，也可从历史记录重新打开任务。

解读结果时注意：

- **覆盖情况**：文件可能由多个 Unit 共同审查；只有相关 Unit 均完整完成，文件才计为已审查。`partial` 或 `completed_with_warnings` 表示仍有未完整完成的检查。
- **问题与检查状态**：`checked` 仅表示检查过，零 Issue 不代表代码正确；缺失证据、未执行补查和未知结果需要人工复核。
- **调用与成本**：Preview 的 Unit 调用预估不包含审查后的跨组协调。成本未配置或实际用量缺失时保持未知，预估不能视为最终账单。

## 配置

配置写入 `backend/.env`，修改后重启后端；完整变量说明见 [`.env.example`](.env.example)。

```env
GITHUB_TOKEN="" # 可选：GitHub API 认证，按所需权限配置，注意 API 限流
OPENAI_API_KEY=your-api-key # 模型服务 API Key，不要提交真实密钥
OPENAI_BASE_URL=https://api.openai.com/v1 # 自定义服务时修改；DeepSeek 示例见 .env.example
REPOGUARDIAN_PROVIDER=openai # 自定义服务时与 API Key、Base URL、模型名一并设置
REPOGUARDIAN_MODEL=gpt-4.1-mini # 实际请求的模型名

REPOGUARDIAN_REVIEW_UNIT_CONCURRENCY=4 # Unit 并发数
REPOGUARDIAN_REVIEW_UNIT_TIMEOUT_SECONDS=180 # 单 Unit 超时，单位秒

REPOGUARDIAN_MODEL_REQUEST_PROFILES={} # JSON：实际模型名到窗口配置的映射，优先于通用配置
# REPOGUARDIAN_MODEL_REQUEST_PROFILE={"context_window":16384,"safety_margin_tokens":512,"input_output_shared":true} # 可选通用覆盖，启用后优先于内置注册表；按供应商实际限制设置
REPOGUARDIAN_MODEL_PRICING_JSON={} # 按 provider:model 配置每百万 token 的美元价格；未配置或用量缺失时成本未知

REPOGUARDIAN_DEFAULT_VALIDATION_BACKEND=none # 可选 project_ci / user_runner，需另行配置执行端
REPOGUARDIAN_LANGSMITH_TRACING=false # 默认关闭追踪；启用后默认仍不上传审查正文
```

## 工作流程

![RepoGuardian 架构图](https://github.com/user-attachments/assets/e405f29f-0587-43b1-aac8-1f2a20ab1066)

准备 PR 与仓库 → 解析变更、建立索引 → 拆分 Review Units 并独立审查 → 解析证据、检查策略并验证候选 → 跨 Unit 风险筛查与定向补查 → 去重并生成报告。

动态验证独立于审查流程，Review 完成时 CI 仍可能运行中。新请求使用 `review`；旧 API 模式值仅作兼容，仍按只读审查处理。

## 安全边界

- 模型只能使用受限的只读工具，文件读取经过路径、符号链接、敏感文件与预算校验；补查不能绕过这些边界。
- RepoGuardian 不执行目标仓库的安装、测试或构建，不自动 commit、push、创建 PR 或写回 GitHub 评论。
- 动态验证只通过显式配置的 Project CI / User Runner 发起，不接受模型生成的 shell 命令；执行端不可用时不会回退到宿主机。Fork PR 默认不触发 Project CI。
- 审查所需的代码内容会发送给配置的模型服务。使用前确认模型服务、外部执行端与凭据的数据边界。
- 本地 SQLite 与运行产物用于持久化和恢复，不构成多租户或生产级安全隔离。

## 当前限制

- 只支持 GitHub PR 输入，暂不写回 GitHub Review、Check Run 或修复 PR。
- 不提供本地 Sandbox 或通用命令执行能力。
- 超大变更可能因输入、时间或模型预算限制而部分完成，需要结合覆盖范围复核。
- 尚未完成真实样本上的协作效果对照与人工核验，不承诺召回、误报或成本收益。

## 开发与验证

后端使用 Python、FastAPI 与 LangGraph，前端使用 Vue 3、TypeScript 与 Vite。先按快速开始安装依赖；本地开发分别运行后端与前端，代码修改后会自动重载。

提交变更前，从仓库根目录执行对应检查：

```powershell
cd backend
uv run pytest
uv run ruff check .

cd ..\frontend
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
