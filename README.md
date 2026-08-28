# RepoGuardian

> 基于证据、理解仓库上下文、严格只读的 AI Pull Request 代码审查 Agent。

RepoGuardian 将 PR 拆分为边界明确的 Review Unit，通过安全的 Git-tracked repository 发现与有界读取补充上下文，再以可解析、可审计的 Evidence Chain 输出 Issue、Coverage 和 Run Manifest。

**RepoGuardian Server 永远不执行目标仓库代码。** 测试、构建和运行时验证统一委托给仓库自身的 GitHub Project CI，并与 Review lifecycle 异步解耦。

> [!IMPORTANT]
> 项目仍处于早期开发阶段。只读分析可识别 Python、TypeScript/JavaScript、Java、Go、Rust；Python 与 TS/JS 使用 Tree-sitter，其他语言按置信度降级到启发式索引。项目不提供本地 Sandbox，也尚未声明开源许可证。

## 核心能力

- **真正的 Unit Plan**：确定性拆分 Review Unit 后，模型输出结构化变更摘要、审查目标和风险假设；Plan 失败会降级为普通审查，不阻断任务。
- **证据化 Issue**：不直接信任模型行号，服务端解析 primary/supporting evidence、位置、来源和 resolution status；Issue 只能锚定当前 PR 的可评论变更文件。
- **Repository-aware 探索**：`file_find` / `code_search` 可发现整个安全的 Git-tracked repository；`file_read` 仍受敏感路径、realpath/symlink、大小、行数和 Unit 预算限制。
- **最小只读工具面**：Unit Agent 只使用 `file_find`、`code_search`、`file_read`、`file_read_diff`、`report_issue` 和 `task_done`。
- **分级多语言分析**：语言适配器统一产出符号、导入和调用引用；解析失败自动从 L2 降级到 L1/L0，Review Unit 仍可安全使用文件读取、路径查找和 diff 读取。
- **Selective Verifier**：确定性 evidence checks 优先，只对高风险、模糊、跨模块或低置信度问题追加模型验证；Verifier 不得提升 severity。
- **可审计、失败隔离**：Coverage / Run Manifest 记录文件、Unit、模型用量、耗时和确认问题；单个 Unit 或 Verifier 失败不会抹掉其他有效结果。
- **独立 Project CI**：只发送服务端注册的 profile、request ID 和 SHA 绑定信息，不发送模型生成的 shell command；Fork PR 默认不 dispatch。

## 工作流程

```text
GitHub PR
  → 确定性解析 diff、索引仓库并拆分 Review Units
  → 为复杂 Unit 生成风险与证据 Plan
  → 在安全 Git-tracked repository 中发现并有界读取 supporting context
  → 解析 Evidence、执行 Issue Policy、Selective Verifier 与去重
  → 输出 Coverage、Run Manifest 和 Review Report
```

Plan 是待验证的审查指导，不是已确认 Issue，也不是固定步骤队列。后续 Agent 可以根据工具反馈调整动作，并发现 Plan 之外的明确缺陷。

Preview 不调用模型、不运行目标代码，会显示文件和 Unit 范围、风险标签以及三种调用口径：Plan 调用数、典型预计调用数和预算上限。

Project CI 是独立异步状态机：Review 可以先完成，Validation 随后处于 Pending、Running 或终态。RepoGuardian 会校验 repository、request ID、head SHA、patch SHA、workflow/run identity 和结构化结果。

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

| 模式 | 主 Review lifecycle | 目标代码执行 |
| --- | --- | --- |
| `review` | 严格只读静态审查 | 不执行 |
| `review_and_suggest` | 旧 API 兼容值；按只读审查执行并给出迁移警告 | 不执行 |
| `review_suggest_and_validate` | 旧 API 兼容值；按只读审查执行并给出迁移警告 | 不执行 |

动态验证通过独立的 `project_ci` 或外部 `user_runner` 边界发起，不属于上述 Review critical path。`gvisor` 仅为已废弃、不可执行的旧请求占位；任何后端不可用时都不会回退到宿主机执行。

## 安全边界

- 模型没有 shell、terminal、package manager、build 或 test 工具，也不能向 Project CI 提交命令文本。
- Repository discovery 可以覆盖安全的 Git-tracked 文件；内容读取仍经过 containment、realpath/symlink、tracked、sensitive-path 与预算校验。
- Unchanged 文件可以成为 supporting evidence，但 Issue primary location 必须属于当前 Unit 的 changed/commentable files。
- Git 命令使用参数化 argv，并隔离 host Git config、credential prompt 和 external diff。
- RepoGuardian 不 commit、push、创建 PR 或写回 GitHub 评论。
- 外部动态验证前应确认 Project CI / UserRunner 的执行环境与凭据边界。
- SQLite 和本地 artifact 提供恢复能力，但不构成多租户或生产级安全隔离。

## 当前限制

- 只接收 GitHub Pull Request URL；不同语言的索引深度仍有差异。
- 不写回 GitHub Review、Check Run、suggestion 或 Draft PR。
- 不提供 Local Sandbox、Docker/gVisor/Firecracker 或通用命令执行能力。
- Metadata-only semantic grouping 和公开 review benchmark 尚未实现；当前 planner 仍以确定性分组和安全 fallback 为主。
- 模型结论仍需工程师结合 Evidence 和 Coverage 复核。

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
