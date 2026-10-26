# RepoGuardian

> 基于证据、理解仓库上下文、严格只读的 AI Pull Request 代码审查 Agent。

输入 GitHub PR URL，先了解变更并确认审查方案，再由 Agent 探索关联代码、核验候选问题，输出可追溯的审查结论、覆盖与限制，以及可复制到 PR 的交付摘要。

**RepoGuardian Server 永远不执行目标仓库代码。** 只进行只读静态审查；测试与构建请使用项目自身的 CI。

[快速开始](#快速开始) · [如何使用](#如何使用) · [开发与验证](#开发与验证) · [反馈问题](https://github.com/waangzh/RepoGuardian/issues)

## 项目特点

- **先确认方案，再调用模型**：选择深度、重点、文件范围和运行上限；预览只进行固定快照准备和静态索引，确认后才启动审查。
- **声明与证据分离**：PR 描述作为不可信背景，实际判断来自工具读取的固定源码；摘要不推断未经核验的作者目标已经实现。
- **原生多 Agent 编排**：LangGraph 动态派发 Behavior、Contract 与风险专家；LangChain 管理各自工具循环，独立 Verifier 核查候选与反证。
- **证据与人工处理分别记录**：候选问题经过证据解析、策略检查和独立核验；可查看代码位置、依据与限制，并填写原因进行人工确认、忽略或标记已处理。
- **结论、限制与交付可追溯**：分别呈现执行状态、问题证据与风险，提供评论、详细报告和技术记录；LangGraph 恢复已完成分支，不共享 Agent 消息历史。

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

本次为破坏性结构替换：默认业务库为 `.repoguardian/native-review.db`，Checkpointer 为 `.repoguardian/review-checkpoints.db`。旧数据库和运行产物不会自动读取、迁移或删除；已有自定义数据库路径时，应改用新的空数据库路径后执行迁移，不要对旧库执行 stamp。

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

1. 输入 GitHub PR 地址，选择深度、审查重点和文件范围，点击“生成审查方案”。服务端准备固定 Base/Head、Diff 和静态索引；预览不调用审查模型。
2. 核对方案中的 Reviewer 派发、源码检查范围、固定 SHA 和运行保护上限，然后确认启动。创建时再次读取 PR 元数据；过期、变更或未经确认的方案会被拒绝。
3. Behavior 与 Contract Reviewer 并行、风险路径增派 Specialist；候选进入服务端证据门禁和独立 Verifier，关键未决项最多进行一次定向调查。
4. 结果页分别显示范围覆盖、已确认问题、未决与运行限制。变更 Diff 与固定双侧文件上下文可查看；历史 PR 讨论仅作不可信上下文，不代替源码证据。
5. 报告和 GitHub 评论草稿只基于保存的验证结果。可人工注记但不会改变机器判定。失败且检查点可续时可恢复；取消后不会产生新的结果或发布请求。

### 交付到 GitHub（可选：复制评论 / 直接发布）

默认关闭。仅支持可信的本地单用户部署，发布接口要求本机连接及独立 Bearer 操作口令，不支持直接公开到公网。操作口令仅在页面内存中临时使用，不是 GitHub PAT。

1. 在 `backend/.env` 中配置独立的 `REPOGUARDIAN_GITHUB_PUBLISH_TOKEN`（Fine-grained PAT，仅授权目标仓库的 **Pull requests: write**）、数字仓库 ID allowlist、PAT 用户数字 ID，以及至少 32 字符的 `REPOGUARDIAN_PUBLICATION_API_TOKEN`。不会回退使用只读 `GITHUB_TOKEN`。
2. 在 GitHub 核对该 PAT 的权限后设置 `REPOGUARDIAN_GITHUB_PUBLISH_PULL_REQUESTS_WRITE_CONFIRMED=true`，再启用 `REPOGUARDIAN_GITHUB_PUBLISH_ENABLED=true`。预检还会读取 GitHub 身份与仓库写角色；仓库角色不等于 Token grant，实际写入的 401/403 仍会被明确拒绝。完整配置见 [环境变量示例](.env.example)。
3. 在 `backend/` 执行 `uv run alembic upgrade head`，再重启后端。在终态审查页进入 **交付到 GitHub**。未启用发布时页面只提供 **复制评论**（写入剪贴板，不写入 GitHub）。启用后输入操作口令、选择问题并预检；确认总结评论创建/更新、审查版本、代码行定位和已有评论后，二次确认写入。
4. 发布状态与 Review Outcome 独立。可通过发布历史重新查看状态及 GitHub 返回的链接。`uncertain` 使用 **检查远端发布状态**：先核对已发送批次，无法确认时保留未知并要求人工处理，不盲目重发 POST。

PR 总结评论复用同一 PR、同一发布身份的主评论；代码行评论按同一 Head 的严格内容和定位去重。过期 Base/Head、关闭 PR、未知作者或手工修改的摘要会阻止写入。无法精确映射到代码行的已核验问题会写入总结评论，并需要明确确认部分发布。跨系统写入并非原子事务，不承诺 exactly-once；API 版本复查后仍存在短暂远端竞态窗口。

### 结果解读与报告交付

审查完整性只说明源码范围是否已检查，不能从“0 个问题”推导为无缺陷。候选必须有真实工具读取产生的证据 ID、固定提交身份及可定位 HEAD 差异，独立核验不足时维持未决。未经确认的候选不得出现在发布草稿中；删除行和不可行内定位问题只能进入总结评论。未知 Token 用量不会显示为零。技术报告记录 Graph 分支、核验依据和限制，不包含共享的 Agent 消息历史。

## 配置

配置写入 `backend/.env`；[环境变量示例](.env.example) 包含只读 GitHub 凭据、模型端点、Agent 调用上限、超时、预览工作区和可选的发布权限。模型须支持原生工具调用与结构化输出。设置页可修改服务 URL、默认模型和记录保留天数；API Key 不在前端展示。

`REPOGUARDIAN_AGENT_MAX_CALLS` 和 `REPOGUARDIAN_AGENT_MAX_TOOL_CALLS` 分别限制每个 Agent 的模型与工具调用；`REPOGUARDIAN_REVIEW_TIMEOUT_SECONDS` 限制审查总时长；并发上限由 `REPOGUARDIAN_REVIEW_CONCURRENCY` 控制。供应商未返回用量时保持未知。更换模型/端点只影响新审查，已有任务保留冻结身份。

## 工作流程

GitHub PR 与用户方案 → 唯一比较基准与 HEAD 固定快照 → Diff/静态索引 → LangGraph `StateGraph` 的 `Send` 动态派发 → Behavior/Contract/Specialist 的 LangChain `create_agent` 自主只读取证 → 候选并集与可信证据门禁 → 独立 Verifier / 有界调查 → 确定性报告 → 显式 GitHub 发布预检与 Outbox。

Graph Checkpointer 负责执行节点恢复；业务数据库保存方案、生命周期、结果及 Lease；固定仓库快照与证据归档保存事实，发布 Outbox 保存投递和对账状态。外部模型与 GitHub 写入不承诺 exactly-once。再次审查会重新获取当前 PR 快照并进行独立核验，不复用旧结论。

## 安全边界

- 模型只能使用受限的只读工具，文件读取经过路径、符号链接、敏感文件与预算校验；补查不能绕过这些边界。
- RepoGuardian 不执行目标仓库的安装、测试或构建，不自动 commit、push、创建 PR。GitHub 评论写入只由独立 Publisher 在用户预检和确认后执行，Review Agent 没有写权限。
- 不提供任何动态执行入口：不发起 Project CI、不接收外部 Runner 结果，也不接受模型生成的命令。
- 审查所需的代码内容会发送给配置的模型服务。使用前确认模型服务与凭据的数据边界。
- 本地 SQLite 与运行产物用于持久化和恢复，不构成多租户或生产级安全隔离。

## 当前限制

- 只支持 GitHub PR 输入；可显式配置本地单用户手动 Summary / Inline 发布，不自动 Approve / Request Changes，不创建 Check Run 或修复 PR。
- 不提供本地 Sandbox、通用命令执行或外部验证集成能力。
- 超大变更可能因时间或模型调用限制而部分完成，需要结合覆盖范围复核。
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
- 修改配置时同步 `.env.example`；修改 API 响应时同步前端类型与调用方，覆盖输入校验与拒绝路径。本项目为个人项目，不为旧接口或旧数据保留兼容。
- 开发测试可执行本项目代码，RepoGuardian Server 仍不得执行被审查的目标仓库代码。
- 不提交 `.env`、API Key、Token、数据库或任务运行产物。

欢迎通过 Issue 或 Pull Request 反馈问题、改进文档与代码。提交 [GitHub Issue](https://github.com/waangzh/RepoGuardian/issues) 时附上复现步骤、预期与实际结果，以及脱敏后的错误信息。

## 许可证

MIT
