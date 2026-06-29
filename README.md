# RepoGuardian

RepoGuardian 是一个面向 GitHub Pull Request 的**智能代码审查 Agent 系统**。基于 LangGraph 状态图编排多个专业 Agent，理解仓库结构、检索代码上下文、执行 LLM 审查，生成结构化审查报告。前端控制台实时展示任务进度。

## 当前能力

- GitHub PR URL 解析 + PR metadata 获取，支持可选 `GITHUB_TOKEN`
- PR base/head diff 生成，支持 fork PR
- unidiff diff 解析
- LangGraph 状态图编排（7 节点流水线）
- tree-sitter AST 仓库索引（文件级 + 符号级）
- 代码上下文检索（调用方、被调用方、测试文件）
- 可插拔 LLM Provider：`mock`（零费用演示）/ `openai` / `deepseek` / `openai-compatible`
- Markdown 审查报告生成
- SSE 事件流实时推送任务进度
- SQLite 业务持久化 + LangGraph checkpoint
- FastAPI 后端 + Vue3 + TypeScript 前端控制台

## 架构概览

```
LangGraph StateGraph (ReviewState)
  intake → repo_prepare → diff_parse → repo_index → context_retrieve → review → report

后端: Python 3.11+ / FastAPI / LangGraph / SQLAlchemy / tree-sitter / unidiff
前端: Vue 3 / TypeScript / Vite / SSE EventSource
存储: SQLite (业务) + SQLite (checkpoint)
```

## 环境与启动

```powershell
conda env create -f environment.yml
conda activate repoguardian
python -m pip install -e .\backend[test]
```

### 后端

```powershell
conda activate repoguardian
cd backend
copy ..\.env.example .env
uvicorn app.main:app --reload
```

默认 `REPOGUARDIAN_PROVIDER=mock` 无需 LLM Key。真实审查配置示例：

```env
REPOGUARDIAN_PROVIDER=openai
OPENAI_API_KEY=你的 API Key
OPENAI_BASE_URL=https://api.openai.com/v1
REPOGUARDIAN_MODEL=gpt-4.1-mini
```

DeepSeek：

```env
REPOGUARDIAN_PROVIDER=deepseek
OPENAI_API_KEY=你的 DeepSeek Key
OPENAI_BASE_URL=https://api.deepseek.com
REPOGUARDIAN_MODEL=deepseek-v4-pro
```

### 前端

```powershell
cd frontend
npm install
npm run dev
```

## API

创建审查任务：

```http
POST /api/reviews
Content-Type: application/json

{"pr_url": "https://github.com/owner/repo/pull/123", "model": "可选模型名"}
```

查询任务：

```http
GET /api/reviews/{task_id}
```

获取 Markdown 报告：

```http
GET /api/reviews/{task_id}/report
```

SSE 实时进度流（新增）：

```http
GET /api/reviews/{task_id}/stream
```

## 测试

```powershell
conda activate repoguardian
cd backend && pytest

cd ..\frontend && npm run build
```

当前版本不包含：自动修复、Docker 沙箱、ruff/mypy/bandit 静态分析、GitHub 评论、draft PR。
