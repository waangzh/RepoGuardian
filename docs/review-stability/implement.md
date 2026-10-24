# 实施与验证

- [x] 新任务冻结稳定性契约，保留旧 Spec 与请求包身份。
- [x] 确定性准备完整符号与调用链必要源码，约束肯定结论。
- [x] 结构化缺失证据的有界补读与 checkpoint 恢复。
- [x] 共享预算收敛与实际采样参数绑定。
- [x] 显式未决调查恢复与同输入复用准入。
- [x] 聚焦 SDK、关系规划、预算、恢复、增量与旧协议回归。
- [x] 后端 Ruff、git diff --check，核对最终变更范围。

首轮验证阶段未发起真实模型请求、未执行目标仓库代码；其实现随后经用户授权提交为 `7f7d318`，未推送。本次漏引修复的验证和交付状态见文末。

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

首轮验证完成后经用户授权提交。`scripts/` 为用户授权忽略的并行工作，本任务未读取、修改或暂存。

真实模型多次独立运行的缺陷一致性、召回率与成本尚未评测；浏览器交互未运行。显式同快照复用目前提供 API/客户端可选参数，没有新增界面按钮。部分失败成果在原任务检查点内恢复，不提供跨失败任务的成果拼接。

## 本次漏引修复

- [x] 新增调用范围元数据和 v8 规划，保留旧图及 v7 行为。
- [x] 提供准确的漏引差集反馈，保持一次纠错和原预算。
- [x] 区分调查未执行、已执行失败与未完成，补齐中文拒绝原因及前端统计。
- [x] 覆盖独立/同一行/多行嵌套调用、选择性漏引的成功和重复失败、取消及恢复回归。
- [x] 运行聚焦后端测试、Ruff、前端相关测试与构建、历史产物离线重放；不调用模型、不改业务库。

### 本轮实际验证

- 后端核心组 `test_review_citation_recovery`、`test_review_stability`、`test_task_risk_planner`、`test_python_call_binding`：96 项通过。
- 解析隔离、语言适配、索引/检索、多语言、预览确认、生产运行时、漏引恢复、报告、取消、API、关系报告及增量复用扩展组：229 项通过。
- 补充完整的漏引回归文件、v7/v8 生产检查点恢复、协议和静态定义提示：42 项通过；最后增加的“必引 ID 与范围缺失分别反馈”测试单独 1 项通过。各批次存在重叠，不累加。
- 前端 `node --test --test-concurrency=1 tests/task-runtime-presentation.test.mjs tests/task-runtime-ui.test.mjs`：11 项通过；`npm run build` 通过。
- 最终 `uv run ruff check .`、`git diff --check` 通过。
- 对 cd27c7ec 的四段归档源码核对哈希并静态重建，v7 的四个必要范围在 v8 下收紧为三个；原两次响应在旧规则下仍拒绝，新增反馈均准确识别漏引的 family_wise_z。
- 首轮测试暴露解析器升级遗漏请求 schema 字面量，已同步修正并通过上述回归；未放宽生产拒绝条件。

既有 pytest 缓存目录权限警告与 Vite 大 bundle 提示仍保留。未运行全量后端测试、真实模型审查或真实浏览器交互。本轮验证完成，已获用户授权进行本地提交；未推送或发布，未修改业务数据库和历史结论。

## 系统性稳定化实施

- [x] v2 精确主检查覆盖反馈、纠错分量隔离与调查未决交付。
- [x] 完整同输入发现复用、默认来源选择与全新审查选项。
- [x] 缺口恢复资格与界面入口，保留原期限、预算和未知状态。
- [x] 冻结轮次、确定性应用与派生、收尾调用/Token 保护及账本恢复。
- [x] 辅助一跳变量和多语言上下文预装载，不放宽必引实现。
- [x] 失败、取消、预算、重放、同输入正负结果、前端状态与公开契约回归。
- [x] Ruff、前端测试/构建、最终 diff 与规范同步；验证结束后用户授权本地提交，不推送。

### 系统性稳定化实际验证

- 最终后端组合组：`test_review_systemic_stability`、`test_review_continuation`、`test_review_citation_recovery`、`test_review_stability`、`test_task_runtime_p2/p3/p4`、`test_incremental_review`、`test_preview_confirmation`、`test_api_contract`，225 项通过。
- 传输失败顺序修正后的预算与边界组：系统性稳定化、续查、运行预算、Provider 预算、预算持久化与取消，61 项通过；与最终组合组存在重叠，不累加。
- 延迟反转同时覆盖正常响应与一次传输失败后的重试，比较最终队列、候选状态、收敛模式、调用与实际 Tokens；保护恢复、未知消费和取消均有回归。生产复用覆盖初始与派生锚点的 confirmed finding，验证问题及核验/发布依据没有丢失。
- 续查覆盖凭据一次消费、重复请求、Head/PR 背景变化、入队失败事务回滚、原预算不重置、期限/调用/费用/未知请求拒绝，以及最新取消记录不被旧完整结果掩盖。
- 前端 `npm test`：188 项通过；最后交互入口和错误提示调整后，`node --test --test-concurrency=1 tests/review-stability-ui.test.mjs tests/review-plan-ui.test.mjs tests/preview-jobs.test.mjs tests/review-restart.test.mjs tests/github-errors.test.mjs`：15 项通过；最终 `npm run build` 通过。
- 最终 `uv run ruff check .`、`git diff --check` 通过。Trellis 规范同步了 v2 的证据未决、冻结轮次、预算保护、同输入复用及续查契约。

没有运行全量后端测试、真实模型、多次独立生成一致性评测或真实浏览器交互；前端验证为请求、SSR、状态测试与类型构建。没有修改业务数据库、历史结论或目标仓库，没有推送或发布评论。既有 pytest 缓存目录权限警告、前端并行测试 WebSocket 端口提示和 Vite 大 bundle 提示保留，测试与构建退出成功。

验证完成，已获用户授权进行本地提交。静态一跳和多语言源码准备仍为有界辅助提示，不代表编译器绑定或完整数据流分析；收尾保护按当时请求估算，不保证实际请求增长后仍可准入。
