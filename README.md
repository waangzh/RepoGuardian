# RepoGuardian

RepoGuardian 是一个面向 GitHub Pull Request 的智能代码审查助手。当前版本支持输入 PR URL，由后端获取 PR 信息、克隆仓库、生成并解析 diff，调用可插拔 LLM Provider 生成结构化审查问题和 Markdown 报告，前端展示任务状态、变更文件、问题列表和报告。

## 当前能力

- GitHub PR URL 解析
- GitHub PR metadata 获取，支持可选 `GITHUB_TOKEN`
- PR base/head diff 生成，支持 fork PR
- `unidiff` diff 解析
- 可插拔 LLM Provider
  - `mock`：无 API Key 时演示完整审查闭环
  - `openai`：OpenAI 兼容 Chat Completions API`r`n  - `deepseek`：DeepSeek OpenAI 兼容接口
- FastAPI 后端接口
- Vue3 + TypeScript 前端控制台
- Markdown 审查报告生成
- 可通过 `REPOGUARDIAN_GIT_BIN` 指定 Git 可执行文件

当前版本不包含自动修复、AST 上下文索引、ruff/pytest/bandit、Docker 沙箱、GitHub 评论或 draft PR。

## 后端环境

推荐使用 conda 管理 Python 环境。项目根目录提供了 `environment.yml`：

```powershell
conda env create -f environment.yml
conda activate repoguardian
```

如果环境已经存在，可以更新依赖。

在项目根目录 `D:\Code\RepoGuardian` 运行：

```powershell
conda activate repoguardian
python -m pip install -e .\backend[test]
```

如果你已经在 `backend` 目录，也就是提示符类似 `(repoguardian) PS D:\Code\RepoGuardian\backend>`，运行：

```powershell
conda activate repoguardian
python -m pip install -e .[test]
```

## 后端启动

```powershell
conda activate repoguardian
cd backend
copy ..\.env.example .env
uvicorn app.main:app --reload
```

默认 `REPOGUARDIAN_PROVIDER=mock`，不需要 LLM Key。要启用真实审查，编辑 `backend/.env`。

OpenAI 示例：

```env
REPOGUARDIAN_PROVIDER=openai
OPENAI_API_KEY=你的 API Key
OPENAI_BASE_URL=https://api.openai.com/v1
REPOGUARDIAN_MODEL=gpt-4.1-mini
```

DeepSeek 示例：

```env
REPOGUARDIAN_PROVIDER=deepseek
OPENAI_API_KEY=你的 DeepSeek API Key
OPENAI_BASE_URL=https://api.deepseek.com
REPOGUARDIAN_MODEL=deepseek-v4-pro
```

## 前端启动

```powershell
cd frontend
npm install
npm run dev
```

浏览器打开 Vite 输出的地址，通常是 `http://localhost:5173`。

## API

创建审查任务：

```http
POST /api/reviews
Content-Type: application/json

{
  "pr_url": "https://github.com/owner/repo/pull/123",
  "model": "可选模型名"
}
```

查询任务：

```http
GET /api/reviews/{task_id}
```

获取 Markdown 报告：

```http
GET /api/reviews/{task_id}/report
```

## 测试

```powershell
conda activate repoguardian
cd backend
pytest

cd ..\frontend
npm run build
```