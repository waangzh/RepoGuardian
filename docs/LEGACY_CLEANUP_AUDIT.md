# 遗留代码与测试审计（2026-10-24）

审计基准：`master` 的 `13a8c5c`，审计开始时工作区干净。下表在删除前记录；位置均指基准提交，清理后的行号会变化。

## 方法与删除边界

- 使用 Python AST 建立 `backend/app` 与 `backend/tests` 的导入和符号引用候选，再核对当前磁盘源码、生产入口、历史数据消费者和精确文本引用。
- 前端核对 `main.ts:7` → `App.vue` 的显式组件导入；源码无 `import.meta.glob`、全局组件注册或 `resolveComponent` 动态加载这些候选。另检查 `frontend/tests` 的组件引用。
- 零引用只是候选依据。CLI 的 `python -m` 入口、解析子进程、动态 tokenizer、API 装饰器和序列化模型不能仅凭导入数删除。
- 保留旧任务的读取、报告、覆盖解释、队列拒绝及清理能力；保留当前任务冻结的协议/规划器恢复逻辑。未升级或迁移历史数据库。
- 不访问实际运行数据库、不执行被审查仓库代码、不调用付费模型；提交与推送另按用户明确授权执行。

## 删除前证据

| 删除对象（基准位置） | 调用与失效证据 | 保留的实际消费者/替代路径 |
| --- | --- | --- |
| `graph/builder.py:1` | 仅将 `build_review_graph` 原样重新导出；唯一调用方是 `services/review_service.py:14` | 调用方直接导入 `graph/review_graph.py:17` 的唯一生产图 |
| `services/review_service.py:108` 的 `command_executor` 参数 | 参数在 `:120` 立即 `del`；AST 核对全部 `ReviewService(...)` 调用，无该关键字或第六个位置参数 | 外部验证服务独立存在，主审查不接收目标代码执行器 |
| `services/review_input_context.py:41,187,290` | `build_retrieval_catalog`、`build_working_memory` 无外部调用；`input_snapshot` 仅由被删工作记忆调用 | 新运行时 `review_task_executor` / `task_context_pages` 自己维护目标、页面和证据 |
| `services/context_assembler.py:15` | `assemble_context` 唯一调用来自上述失效的 `build_working_memory`；其异常类没有其他消费者 | 当前请求通过 `PreparedModelRequest.estimate` 与 `ReviewRunBudget` 准入 |
| `services/model_request_budgeter.py:201` | `UnitRequestLedger` 无源码或测试引用 | 保留 `PreparedModelRequest`、`ModelContextBudget`、模型窗口配置、请求预算 ContextVar 和运行级持久账本；`pr_discussions.py:87,180` 仍使用 `ModelContextBudget`，复核后将其撤出删除候选 |
| `services/evidence_store.py:11,23,80,123,129,144,160` | `ContextAdmission`、`restore_store`、`evidence_priorities`、`archived_request_chunks`、`merge_stores`、`tool_result_metadata`、`store_catalog` 均无外部引用 | `review_task_step.py:12,108,383` 仍调用 `evidence_snapshot`、`put_chunks`、`store_chunks`，保留这三者 |
| `services/unit_coverage.py:31,75,86,132,280,289,301,306,348,357,361` | 旧 manifest 构造/活动集/写入/重建/层级输入只在这些函数内部互相引用，没有外部根调用；契约分类与分组辅助同样无调用方 | `review_manifest`、`review/unit_completion` 仍读取历史 manifest/ledger，保留 `unsupported_hunk_ids`、`manifest_file_changes`、`_covers`、`coverage_gaps` 与 `NO_HUNK_CHANGE_REASON` |
| `services/unit_review_summary.py:12` | `build_record_input` 唯一消费者是上行被删的两个构造函数，整个模块成为孤立依赖 | 保留 `review_input_context.build_context_evidence` 和 `make_evidence` 的当前证据路径 |
| `review_input_context.py:35` | `question_identity` 只被旧工作记忆和旧 ledger 写入函数引用，随其调用闭包一起退出 | 历史 ledger 的存量 question ID 仍原样读取 |
| `review/unit_completion.py:44,65,107,115` | `batch_target_coverage`、`batch_contract_coverage`、`has_unresolved_contracts`、`is_review_unit_execution_complete_with_warnings` 无源码/测试调用 | 保留报告和 repository 使用的严格完成、可复用与警告解释 |
| `review/language_rules.py:24,33,39,80,88,121` | 规则文本和 Markdown fence 辅助只供未接线的旧语言 prompt 构造；相关入口均无调用 | 保留 `:65 language_for_path`，当前确定性规划器仍依赖它 |
| `services/fingerprints.py:82`、`services/model_usage.py:32` | `unit_execution_fingerprint` 和 `annotate_usage` 无外部引用 | 保留实际使用的稳定哈希、Unit/Patch/Validation 指纹及用量聚合 |
| `graph/policies.py:16`、`graph/nodes/_events.py:25` | `consume_budget` 和 `append_event` 无外部引用 | 保留 intake 的预算还原及生产节点的 `append_step` |
| `graph/checkpointer.py:119,170` | `checkpoint_storage_stats` 与旧独立 Unit 图的 `unit_thread_config` 均无引用 | 保留根图恢复、历史 Unit thread 查找/删除和实际使用的压缩统计 |
| `evidence/branch.py:40` | `counterevidence_branch_error` 无源码/测试调用 | 保留当前 resolver 使用的 `branch_error` |
| `review/issue_audit.py:18,26,41` | Unit 审计上下文及旧 schema 输入/结果回调无调用；上下文变量仅被这组函数使用 | 保留评测仍使用的 `capture_issue_audit` / `audit_issue`，不能将旧评测资料直接删除 |
| `models/review.py:1091,1111,1252,1265,1292,1730,1740,2099,2112` | 旧候选输入、旧 verifier 请求/输出、旧去重输出、孤立定位模型及 coordination proposal 无当前调用或存储字段；内部依赖只限这些输入/输出适配器 | 保留历史领域对象 `ReviewIssue`、`EvidenceAnchor`、`CrossUnitCoordinationPlan` / `CrossUnitFollowupRequest` 等，以及模型恢复 allowlist 的现有机制 |
| `models/review_runtime.py:36` | 后端 `ReviewEngine` enum 无引用；持久化字段实际使用 `Literal`，前端同名类型是独立定义 | 保留 `ReviewTask.engine` / `ReviewRunManifest.engine` 的历史默认值与冻结规则 |
| `projects/adapter.py:21` 的 `ProjectAdapter` 别名 | 仅被 `projects/__init__.py` 再导出；无调用者，现有协议是 `ProjectValidationAdapter` | 注册表、Python adapter 和受控验证协议保留 |
| 前端 `ChangedFiles.vue:1`、`ContextPanel.vue:1`、`IssueList.vue:1`、`PatchPanel.vue:1`、`ValidationPanel.vue:1` | 源码及测试均无组件引用；`main.ts` 与 `App.vue` 不加载它们 | 当前 `ReviewWorkspace` 及各页面仍保留，历史报告组件和 API 类型不动 |
| 前端 `review/ReviewMetrics.vue:1`、`ReviewPreviewPanel.vue:1`、`ReviewUnitsPanel.vue:1` | 同样无源码/测试引用；是旧单页/预览呈现残留 | 当前 `PreviewPage`、工作区与 scope/progress 页面保留 |
| `services/report_service.py:57,87,92,127` | `_append_task_runtime_report`、`_report_text`、`_append_user_runtime_findings`、`_append_task_runtime_findings` 只在旧渲染函数之间相互引用；`ReportService.generate:34-37` 已走 `render_technical_audit`，无测试直接调用这些 helper | 删除前先将仍有价值的关系线索展示接入当前 renderer，并保留关系报告测试的全部断言 |

不把 `repoguardian_unit_input_mode` 当作死配置删除：它仍参与 Unit fingerprint，可能改变已有方案哈希。旧 prompt/planner 版本也仍影响冻结任务恢复，保留并列为后续迁移问题。

## 冗余测试的覆盖映射

以下删除基于断言与触发条件重复，不基于测试名称或失败结果：

| 删除测试（基准位置） | 覆盖它的保留测试 |
| --- | --- |
| `test_legacy_engine_retirement.py:63 test_legacy_coverage_version_is_still_accepted` | 同文件 `:42 test_old_snapshot_without_engine_defaults_to_legacy_units` 用同一显式 coverage version 经 `ReviewTask` 嵌套校验并断言值 |
| 同文件 `:88 test_legacy_task_retry_is_refused_with_explicit_reason` | 同文件 `:98 test_legacy_task_retry_refusal_precedes_runtime_report_check` 以相同无 runtime report 的旧任务调用重试；合并保留旧引擎和只读原因断言 |
| 同文件 `:108 test_new_tasks_cannot_choose_the_retired_engine` | `test_review_engine_routing.py:10,23` 覆盖两种请求默认值，以及参数化的旧引擎拒绝；后者还校验错误字段 |
| `test_review_engine_routing.py:81 test_historical_legacy_task_retry_is_rejected` | 上述保留的无 runtime report 的拒绝测试；另保留参数化的历史队列退役、任务不变、无图构建/预算读取回归 |

## 发现的真实缺陷

`evaluation/runtime_baseline_cli.py:43` 将汇总引擎固定为 `legacy_units`，但 `runtime_baseline.py:145` 已按 `task_review_v1` 分发当前产物。直接调用 CLI 处理当前产物会误标来源。

修复策略：每个 sample 保存实际 engine；汇总在单一引擎时使用该值，跨引擎时标为 `mixed`；缺省 engine 的旧产物继续标为 `legacy_units`。新增真实 JSON 文件输入的当前/历史/混合回归，并验证原来“计量未知不能写成零”的边界。

完整后端回归另外发现三个失败，相关 renderer 在清理前后未变化：

- `test_relationship_reporting.py:54`：关系线索的分组、成员 ID、调用位置和未核验说明只存在于无人调用的旧报告 helper，当前审计导出仅保留原始 JSON。恢复当前 renderer 的可读关系索引，原有测试及断言全部保留。
- `test_review_delivery.py:98`：`ChangeUnderstanding.diff_summary` 已有实际 Diff 文件数量，但当前简洁评论只显示实现/测试观察，遗漏范围事实。补充独立标注的 Diff 元数据，不能当作已验证行为；保留数量检查并沿用事实投影的“共变更 N 个文件”措辞（`test_review_synthesis.py:192` 同样使用该措辞）。
- `test_review_delivery.py:46`：测试还要求旧标题“代码观察”，而当前 renderer 和 `test_review_comment_editorial.py:31` 已约定“实现变化 / 测试核对”。保留该测试的作者声明隔离、目标核验不足及 HTML 安全断言，将旧标题断言改为当前标题，并增加缺少实现观察的断言；不恢复已经退役的三问式 UI。

## 验证记录

- 清理前聚焦基线：22 个后端测试文件，259 passed；新增基线引擎回归先复现 3 个失败，再修复。
- 清理后完整后端 `uv run pytest -q`：1497 passed、3 failed、1 skipped（836.31 秒）。三个失败的证据及修复见上一节，不把这一轮声称为全绿。
- 修复后运行 21 个受影响后端文件：441 passed（45.25 秒），包括关系报告、交付、基线、报告 renderer、发布/API、合成、Outcome 和引擎退役回归；没有再次重复整个后端套件。
- 后端 `uv run ruff check .`：通过。
- 前端 `node --test --test-concurrency=1 tests/*.test.mjs`：184 passed；保持全部前端测试。
- 前端 `npm run build`：vue-tsc 与 Vite 构建通过，235 modules transformed。
- AST 检查 1439 处本地 Python 导入目标，无悬空目标。保留定义的 AST 对比确认，初次清理阶段只改变构造参数、评测 CLI 提示和已复现的引擎来源逻辑；随后额外改动只涉及已复现的报告缺陷。
- `git diff --check`：通过。审计后按用户要求将本记录及交付、状态语言规范归档到 `docs/`，修正链接与忽略规则；提交按后续授权执行，不推送。

## 清理结果及剩余限制

- 审计阶段已跟踪源码/测试改动共 38 个文件，新增本审计记录；删除 3 个后端模块、8 个未接线 Vue 组件和 57 个后端顶层定义（含已删除模块中的定义），移除无用导入、常量和兼容导出。源码与测试差异为 +91 / -1984 行，净减少 1893 行，不计本审计文档、后续文档归档和本地规范。
- 合并 4 个重复测试，保留原有独立覆盖；新增 3 个引擎来源参数化用例和 1 个关系成员完整性/文本安全用例。另保留并更新 2 个旧文案断言，使其验证当前语义，而非依赖退役标题。
- 本地 `.trellis/spec/backend/` 的目录、证据与质量规范已同步；该目录被既有 Git 规则忽略，不会随普通源码提交。
- 明确保留旧任务读取、报告、覆盖解释、冻结协议和历史队列拒绝。旧 Unit 输入模式仍参与指纹；彻底取消历史模式应先制定迁移/失效策略。
- 单独复核跳过项：`test_review_tool_security.py::test_realpath_escape_is_rejected` 因 Windows 不允许创建符号链接而跳过；未关闭路径安全校验。
- 环境仍有 pytest 缓存目录权限警告；完整测试还提示 Starlette/httpx 弃用。前端 Node SSR 测试有 Vue 渲染警告（用例替换 Markdown renderer），构建提示主 chunk 超过 500 kB。本轮未清理缓存、升级依赖或修改测试以隐藏警告。
- 未执行真实浏览器、真实模型、GitHub 网络/发布或生产数据库迁移验证；未采集数值化行覆盖率，不声称覆盖率百分比不变。
- 两条只读子代理探索因服务端模型不可用而未启动，所有结论由主会话完成取证和验证。静态引用分析不保证发现全部动态/外部消费者，本次只删除高置信、已记录证据的依赖闭包。
