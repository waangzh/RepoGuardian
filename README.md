# RepoGuardian

RepoGuardian 是一个面向 GitHub Pull Request 的智能代码审查 Agent 系统。它接收 PR URL，获取 PR 元数据，克隆仓库并生成 diff，通过 LangGraph 编排审查流水线，结合仓库索引和上下文检索，生成结构化问题列表与 Markdown 审查报告。

默认使用 `mock` Provider，可以在没有 LLM API Key 的情况下跑通完整流程，适合本地开发和演示。

## 目录

- [特性](#特性)
- [架构](#架构)
- [技术栈](#技术栈)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [API](#api)
- [项目结构](#项目结构)
- [测试](#测试)
- [当前边界](#当前边界)
- [贡献](#贡献)

## 特性

- GitHub PR URL 解析和 PR metadata 获取，支持可选 `GITHUB_TOKEN`
- 支持 base/head diff 生成，包括 fork PR 场景
- 基于 `unidiff` 解析变更文件、hunk、增删行
- 基于 LangGraph StateGraph 编排 7 个审查节点
- 基于 tree-sitter 建立文件级和符号级仓库索引
- 检索直接变更、调用方和测试文件等相关上下文
- 可插拔 LLM Provider：`mock`、`openai`、`deepseek`、`openai-compatible`
- 生成结构化审查问题和 Markdown 报告
- 通过 SSE 向前端实时推送任务进度
- 前端展示任务状态、PR 摘要、变更文件、上下文片段、问题列表和报告

## 架构

```text
GitHub PR URL
   |
   v
FastAPI /api/reviews
   |
   v
ReviewService
   |
   v
LangGraph StateGraph
   |
   +--> intake
   +--> repo_prepare
   +--> diff_parse
   +--> repo_index
   +--> context_retrieve
   +--> review
   +--> report
   |
   v
Vue 控制台 + SSE 进度流
```

核心约定：

- 状态载体是 `ReviewState`，节点间只传递 dict
- 工具通过 `state["_xxx"]` 注入，方便测试替换
- Git 和文件系统等阻塞操作使用 `asyncio.to_thread` 包装
- LLM 返回值必须经过 JSON 解析和 Pydantic schema 校验
- 任务状态当前保存在后端内存中，服务重启后不恢复

## 技术栈

| 层级 | 技术 |
| --- | --- |
| 后端 | Python 3.11+、FastAPI、Pydantic、LangGraph |
| 仓库分析 | Git、unidiff、tree-sitter、tree-sitter-python |
| 存储 | SQLite、SQLAlchemy、LangGraph checkpoint SQLite |
| 前端 | Vue 3、TypeScript、Vite、EventSource/SSE |
| 测试 | pytest、pytest-asyncio、vue-tsc、Vite build |

## 快速开始

### 1. 克隆项目

```powershell
git clone https://github.com/waangzh/RepoGuardian.git
cd RepoGuardian
```

### 2. 安装后端依赖

推荐使用项目提供的 conda 环境：

```powershell
conda env create -f environment.yml
conda activate repoguardian
```

如果环境已经存在，可以直接安装或更新后端包：

```powershell
conda activate repoguardian
python -m pip install -e .\backend[test]
```

### 3. 启动后端

```powershell
conda activate repoguardian
cd backend
copy ..\.env.example .env
uvicorn app.main:app --reload
```

后端默认地址：`http://127.0.0.1:8000`

健康检查：

```http
GET /health
```

### 4. 启动前端

在另一个终端运行：

```powershell
cd frontend
npm install
npm run dev
```

Vite 默认地址通常是：`http://localhost:5173`

### 5. 发起一次审查

在前端输入 GitHub PR URL，例如：

```text
https://github.com/owner/repo/pull/123
```

本地默认 `REPOGUARDIAN_PROVIDER=mock`，不需要真实 LLM Key。

## 配置说明

后端启动时会读取 `backend/.env`。可以从根目录的 `.env.example` 复制：

```powershell
cd backend
copy ..\.env.example .env
```

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `GITHUB_TOKEN` | 空 | GitHub API Token；不填也可访问公开仓库，但有更低 rate limit |
| `OPENAI_API_KEY` | 空 | OpenAI 兼容接口 Key；`mock` 模式不需要 |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容接口地址 |
| `REPOGUARDIAN_MODEL` | `gpt-4.1-mini` | 默认审查模型 |
| `REPOGUARDIAN_PROVIDER` | `mock` | Provider，可选 `mock`、`openai`、`deepseek`、`openai-compatible` |
| `REPOGUARDIAN_GIT_BIN` | `git` | Git 可执行文件路径或命令名 |
| `REPOGUARDIAN_WORKDIR` | `backend/.repoguardian/workspaces` | 临时克隆仓库目录 |
| `REPOGUARDIAN_DB_PATH` | `backend/.repoguardian/repoguardian.db` | 业务 SQLite 数据库路径 |
| `REPOGUARDIAN_CHECKPOINT_DB` | `backend/.repoguardian/checkpoints.db` | LangGraph checkpoint 数据库路径 |

### OpenAI 示例

```env
REPOGUARDIAN_PROVIDER=openai
OPENAI_API_KEY=你的 API Key
OPENAI_BASE_URL=https://api.openai.com/v1
REPOGUARDIAN_MODEL=gpt-4.1-mini
```

### DeepSeek 示例

```env
REPOGUARDIAN_PROVIDER=deepseek
OPENAI_API_KEY=你的 DeepSeek Key
OPENAI_BASE_URL=https://api.deepseek.com
REPOGUARDIAN_MODEL=deepseek-v4-pro
```

## API

### 创建审查任务

```http
POST /api/reviews
Content-Type: application/json

{
  "pr_url": "https://github.com/owner/repo/pull/123",
  "model": "可选模型名"
}
```

响应：

```json
{
  "task_id": "任务 ID",
  "status": "pending"
}
```

### 查询任务状态

```http
GET /api/reviews/{task_id}
```

返回任务状态、执行步骤、PR 信息、变更文件、上下文片段、审查问题和报告内容。

### 获取 Markdown 报告

```http
GET /api/reviews/{task_id}/report
```

### 订阅实时进度

```http
GET /api/reviews/{task_id}/stream
```

事件类型：

- `step_progress`：节点完成进度
- `done`：任务完成或失败
- `error`：任务不存在或事件流错误

## 项目结构

```text
RepoGuardian/
├─ backend/
│  ├─ app/
│  │  ├─ api/          # FastAPI 路由
│  │  ├─ agents/       # LLM Provider 和 ReviewAgent
│  │  ├─ core/         # 配置和数据库
│  │  ├─ graph/        # LangGraph 状态图、节点和 checkpoint
│  │  ├─ models/       # Pydantic 模型和 SQLAlchemy ORM
│  │  ├─ services/     # 业务编排和报告生成
│  │  └─ tools/        # GitHub、Git、diff、索引、代码搜索工具
│  └─ tests/           # 后端测试
├─ frontend/
│  └─ src/
│     ├─ api/          # API 调用封装
│     ├─ components/   # Vue 展示组件
│     └─ types/        # 前端类型定义
├─ environment.yml     # conda 环境
└─ README.md
```

## 测试

后端：

```powershell
conda activate repoguardian
cd backend
pytest
```

前端构建检查：

```powershell
cd frontend
npm run build
```

完整本地验证通常至少运行：

```powershell
conda activate repoguardian
cd backend
pytest
cd ..\frontend
npm run build
```

## 当前边界

当前版本聚焦 PR 审查、上下文检索和报告生成，暂不包含：

- 自动修复代码
- Docker 沙箱执行
- ruff、mypy、bandit 等静态分析集成
- GitHub review comment 写回
- draft PR 自动创建
- 跨进程或服务重启后的任务恢复

## 贡献

提交变更前建议：

1. 保持改动聚焦，避免无关重构。
2. 后端逻辑、Provider、工具类变更后运行 `cd backend; pytest`。
3. graph 节点变更后确认 `tests/test_review_pipeline.py` 通过。
4. 后端模型字段变更时，同步检查 `frontend/src/types/review.ts`。
5. 前端变更后运行 `cd frontend; npm run build`。

Commit message 使用中文 Conventional Commits 风格，例如：

```text
fix(review): 修复空 diff 解析异常
feat(review): 添加上下文检索面板
```

## 许可证

当前仓库尚未声明许可证。如需开源分发，请先补充 `LICENSE` 文件。
