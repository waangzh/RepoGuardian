# 实施与验证

- [x] 新任务冻结稳定性契约，保留旧 Spec 与请求包身份。
- [x] 确定性准备完整符号与调用链必要源码，约束肯定结论。
- [x] 结构化缺失证据的有界补读与 checkpoint 恢复。
- [x] 共享预算收敛与实际采样参数绑定。
- [x] 显式未决调查恢复与同输入复用准入。
- [x] 聚焦 SDK、关系规划、预算、恢复、增量与旧协议回归。
- [x] 后端 Ruff、git diff --check，核对最终变更范围。

本轮不发起真实模型请求、不执行目标仓库代码、不提交或推送。

## 实际验证

- 后端最终聚焦组：`test_review_stability`、`test_incremental_review`、`test_task_risk_planner`、`test_review_task_protocol`、`test_pr_discussions` 共 113 项通过。
- 最后加固同快照配置完整比较及运行时重验后，单独重跑 `test_incremental_review.py`，39 项通过；与前一组存在重叠，不累加。
- 其他阶段已运行 Provider、预算、运行时调度、取消、检查点、预览作业/确认、Outcome 与 API 契约相关回归；未运行全量测试。
- `uv run ruff check .` 和 `git diff --check` 通过。
- 前端 `node --test --test-concurrency=1 tests/github-errors.test.mjs tests/preview-jobs.test.mjs`：8 项通过；`npm run build`（vue-tsc 与 Vite）通过。
- 修复前 HEAD 与当前代码的 24 组旧请求包逐字节一致；历史 PR #95 取证规划离线复现确认新增外层实现为必要范围。
- 期间新增测试夹具存在同文件边和缺少 PR/effective Base 输入的问题，已修正夹具并复验；未放宽生产校验。

现有环境警告：pytest 无法写入仓库 `.pytest_cache`，业务测试通过；前端构建保留超过 500 kB 的 bundle 提示。没有为消除警告删除缓存目录或改动打包策略。

## 交付边界

验证完成，未提交。`scripts/` 为用户授权忽略的并行工作，本任务未读取、修改或暂存。

真实模型多次独立运行的缺陷一致性、召回率与成本尚未评测；浏览器交互未运行。显式同快照复用目前提供 API/客户端可选参数，没有新增界面按钮。部分失败成果在原任务检查点内恢复，不提供跨失败任务的成果拼接。
