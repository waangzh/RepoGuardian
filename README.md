# RepoGuardian

> 面向 GitHub Pull Request 的 AI 代码审查与受控修复工作台。

RepoGuardian 将 PR 拆分为边界明确的 Review Unit，为复杂 Unit 生成结构化风险计划，并在受控工具循环中完成证据化审查、候选补丁和可选验证。

默认 `review` 模式只读：**不运行目标仓库代码，不生成补丁，也不会提交、推送或写回真实仓库**。

> [!IMPORTANT]
> 项目仍处于早期开发阶段，当前主要支持 Python 项目，尚未提供容器沙箱，也尚未声明开源许可证。

## 核心能力

- **真正的 Unit Plan**：确定性拆分 Review Unit 后，模型输出结构化变更摘要、审查目标和风险假设；Plan 失败会降级为普通审查，不阻断任务。
- **证据化 Issue**：不直接信任模型行号，服务端依据 Base/Head diff、代码片段和 anchor 重新定位证据。
- **受控 Agent**：模型输出经过 JSON 与 Pydantic 校验，文件范围、工具权限和调用预算由服务端控制。
- **安全的候选修复**：只为符合策略的 confirmed Issue 生成受限补丁，并在隔离的干净 Head 上检查。
- **可观测、可恢复**：持久化任务、Unit Plan、Issue、Patch 和验证结果；前端展示 Plan 状态、摘要、风险假设及模型用量。

## 工作流程

```text
GitHub PR
  → 确定性解析 diff、索引仓库并拆分 Review Units
  → 为复杂 Unit 生成风险与证据 Plan
  → 在 Unit 范围内动态检索、审查并确认 Issue
  → 按模式生成候选补丁、执行可选验证并输出报告
```

Plan 是待验证的审查指导，不是已确认 Issue，也不是固定步骤队列。后续 Agent 可以根据工具反馈调整动作，并发现 Plan 之外的明确缺陷。

Preview 不调用模型、不运行目标代码，会显示文件和 Unit 范围、风险标签以及三种调用口径：Plan 调用数、典型预计调用数和预算上限。

## 快速开始

环境要求：Git、uv、Node.js 18+、npm，以及一个 OpenAI 或 OpenAI 兼容服务的 API Key。

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

在另一个终端执行：

```powershell
cd frontend
npm install
npm run dev
```

打开 Vite 输出的地址，输入 GitHub PR URL。建议先运行 **Preview**，再创建审查任务。

## 工作模式

| 模式 | 候选补丁 | 目标代码执行 |
| --- | --- | --- |
| `review` | 不生成 | 不执行 |
| `review_and_suggest` | 显式开启后生成，只做可应用性检查 | 不执行 |
| `review_suggest_and_validate` | 显式开启后生成 | 仅由选定的验证后端决定 |

可用验证后端包括 `none`、`user_runner`、`project_ci`；`gvisor` 当前仅为不可执行占位。验证后端不可用时不会静默回退到宿主机执行。

## 安全边界

- 模型不能提交任意 shell 命令，只能选择服务端允许的 action 和命令 ID。
- Plan、上下文检索和 Issue 证据不能越过当前 Review Unit 的文件范围。
- 补丁只应用于任务临时 clone，不会 commit、push、创建 PR 或写回 GitHub 评论。
- 默认模式不执行目标代码；启用外部验证前应确认执行环境与凭据边界。
- SQLite 和本地 artifact 提供恢复能力，但不构成多租户或生产级安全隔离。

## 当前限制

- 只接收 GitHub Pull Request URL，主要支持 Python 项目。
- 不写回 GitHub Review、Check Run、suggestion 或 Draft PR。
- 未提供 Docker 沙箱、默认网络隔离或硬资源配额。
- 当前没有公开 benchmark，模型结论和修复建议仍需工程师复核。

## 开发与验证

```powershell
cd backend
uv run pytest
uv run ruff check .

cd ..\frontend
npm run build
```

配置项及默认值见 [`.env.example`](.env.example)。后端模型和前端类型必须保持同步，相关约束见 [AGENTS.md](AGENTS.md)。

## 许可证

当前仓库尚未包含 `LICENSE` 文件。在添加许可证前，请勿将代码视为已获得开源使用、修改或再分发授权。
