# 审查内核迁移 P0：离线基线准备

状态：P0 轨迹、计量和候选标注材料已提交；质量标签仍有语义分歧，不能将引用校验通过解释为质量门通过。P1 实施进展见 `review-runtime-p1-2026-10-12.md`，生产默认保持旧引擎。

P1 已开始实现离线垂直切片，但尚未宣称 P1 完成：新协议模块和单 writer runtime 已通过聚焦测试，生产任务创建、持久化恢复、Provider 真实发送和旧任务引擎冻结仍待后续切入。

## 已落地材料

- `backend/app/evaluation/runtime_baseline.py`：按 wire 的 request/attempt、usage ID、request hash 与预算 attempt 对账；重复、缺失和矛盾分别保留，不把未知用量填零。
- `backend/app/evaluation/runtime_baseline_cli.py`：只读取显式指定的已有样本产物，生成独立计量报告、盲审包和评分者身份映射；不导入 Provider、不读取参考答案、不执行目标仓库代码。
- `backend/tests/fixtures/review_runtime_r9/observations.json`：从 r9 两批五个产物提取的有限状态投影，保留原产物 SHA256、快照、153 次 wire 的身份及计量字段、对应账本，以及四类迁移风险的必要状态。没有复制完整 Prompt、运行日志或工作区绝对路径。
- `backend/tests/test_runtime_baseline.py`：准备计量损坏拒绝、未知用量、盲审隔离、五样本对账、Checkmate 校验器最小复现与四类观察不变量的回归。
- `backend/app/evaluation/runtime_adjudication.py`、`runtime_adjudication_cli.py` 和 `backend/tests/test_runtime_adjudication.py`：逐项验收外部质量标注，在评分者登记的临时仓库读取固定 Git blob，核对路径、快照、行号和逐字源码。拒绝敏感路径、越界、旧快照、伪造原文与 symlink/submodule；未知或重复身份不后写覆盖，重复关系的有效缺陷来源被拒绝时传播拒绝。
- `backend/app/models/review_runtime.py`：`task_review_v1` 的冻结运行规格、Goal/WorkItem、read/submit 判别联合和结果模型。
- `backend/app/services/review_task_executor.py`、`review_task_protocol.py`：单任务逐项解析、活动证据绑定、互斥 read/submit、稳定 `PreparedModelRequest` 以及局部失败投影。
- `backend/app/services/review_runtime.py` 的 `TaskReviewRuntime`：P1 单 writer ready/running/completed 投影；旧 `ReviewRuntime` 队列仍保留给兼容路径。

fixture 的 `replay_mode=observed_state_only`、`raw_response_available=false` 是必要边界：r9 wire 完成事件没有完整响应正文，不能声称逐字重放原模型输出。Checkmate 复现测试使用真实证据目录与快照，重建触发相同异常的最小输入；重建的目标和问题 ID 明确使用 `reconstructed` 前缀，不冒充原模型输出。

## 当前真实证据

通过 PowerShell 只读 JSON 和实际 Python 离线入口分别核算，五样本的 wire 完成事件、usage ID 数量和运行账本分别一致：

| 样本 | wire 完成请求 | 实测 tokens | generated 候选 | verifier dismissed |
| --- | ---: | ---: | ---: | ---: |
| ComfyUI | 16 | 224,293 | 3 | 1 |
| Checkmate | 36 | 284,910 | 25 | 8 |
| Gemini CLI | 59 | 996,050 | 33 | 4 |
| RAGFlow | 23 | 361,680 | 10 | 5 |
| Ollama | 19 | 288,307 | 3 | 2 |
| 合计 | 153 | 2,155,240 | 74 | 20 |

这是原始产物的计量与状态核验，不是新协议的真实效果评测。generated 与 dismissed 是管线事件，均不是质量标签。

首轮聚焦测试暴露了一个基线分类错误：Gemini 的 10 个 `admission_rejected` 事件被误作 wire 响应。已按生产 Provider 的发送前审计契约修复，并加入拒绝事件与发送事件冲突的拒绝路径测试。最终报告明确分开 153 次发送与 10 次准入拒绝。

四类状态证据：

1. Checkmate f1/f2 的 `latest_attempt_status=invalid`，原因为 `question update requires a visible prior question`；产物缺少被拒原始 record。生产校验位置为 `unit_review_summary.validate_record`。
2. Gemini 两个父 Unit 共八页；警告保存 `run_budget_admission_rejected` 和明确覆盖缺口；运行账本实际消费 996,050 tokens，未把拒绝调用记成真实请求。
3. Ollama 两个有分页父 Unit 保留 `summary_status=unknown`，子页包含 `no_new_context`；分页中的局部合法检查不等于父范围完整。
4. ComfyUI 父 Unit 的最新原因是 `validated_followup_contract_closure`，f1 的绑定义务更新为 `verified`，但其 outcome 仍 `unresolved`；父契约闭合与补查终态分开。

## 盲审与质量门

盲审包从所有 generated 候选提取主张及证据，包含后续 verifier 丢弃项；评分者映射保存在另一目录，不交给 Reviewer。包中没有 confirmed/dismissed、confidence、Verifier 判定及参考答案正文。源码坐标来自候选记录，完整行为契约仍需读取固定 SHA 的源文件核验。

允许独立标签 `valid_defect`、`false_positive`、`duplicate`、`cannot_determine`；尚未标注的候选保留 `not_reviewed`。有效缺陷和误报要求独立代码证据，重复必须绑定同包中独立有效缺陷。不匹配参考意见不能自动算误报，confirmed 不能成为质量标签。当前尚未完成独立语义盲审和遗漏问题代码核验，因此不报告 precision/recall，不以 74 个候选或 20 个丢弃项声称质量门通过。

已在 `backend/` 实际执行下面的离线命令，生成目录保留在本地；重跑须使用另一全新目录，不覆盖既有输出：

```powershell
uv run python -m app.evaluation.runtime_baseline_cli `
  .repoguardian/evaluation-runs/smoke-20261012-flash-r9-first/0000.json `
  .repoguardian/evaluation-runs/smoke-20261012-flash-r9-remaining/0000.json `
  .repoguardian/evaluation-runs/smoke-20261012-flash-r9-remaining/0001.json `
  .repoguardian/evaluation-runs/smoke-20261012-flash-r9-remaining/0002.json `
  .repoguardian/evaluation-runs/smoke-20261012-flash-r9-remaining/0003.json `
  --output .repoguardian/evaluation-runs/runtime-p0-r9-baseline
uv run pytest tests/test_runtime_baseline.py -q -o cache_dir=.pytest-runtime-p0
uv run pytest -q -o cache_dir=.pytest-runtime-p0
uv run ruff check .
```

生成目录包含候选主张，只留本地，不提交。`blind/packets.json` 供独立评审，`scorer/pipeline-map.json` 与 `accounting.json` 留给评分者。CLI 的隔离是材料分离，不是 OS 权限沙箱。

质量标注验收入口（在 `backend/`；输出文件不得存在）：

```powershell
uv run python -m app.evaluation.runtime_adjudication_cli `
  --packets .repoguardian/evaluation-runs/runtime-p0-r9-baseline/blind/packets.json `
  --annotations .repoguardian/evaluation-runs/runtime-p0-r9-baseline/blind/independent-annotations.json `
  --source-roots .repoguardian/evaluation-runs/runtime-p0-r9-blind-preparation/scorer/source-roots.json `
  --workspace-root .repoguardian/evaluation-runs `
  --reviewer-id independent-reviewer --blind `
  --output .repoguardian/evaluation-runs/runtime-p0-r9-baseline/scorer/independent-adjudication.json
```

`--blind` 由评分者根据实际隔离流程指定，不能由模型标注自我授予。其缺省为非盲态；全部非盲态标注即使源码匹配，也不能关闭独立盲审门。验收只证明引用的原文与快照一致，不证明语义标签正确。部分合法标注可保留，其余仍未审查；未提交不能冒充 cannot_determine。

## 验证与后续

最初 uv 的沙箱执行被拒绝，执行申请又遇到自动审批服务 404；这些失败没有执行测试。会话权限配置更新后，已通过正常执行申请在沙箱外运行项目 uv 环境，没有修改解释器权限或替换包管理器。

实际验证：当前聚焦回归 33 项通过；完整后端回归 1107 项通过、2 项跳过（187.04 秒）；后端 Ruff 通过。新 Python 入口输出五样本 153 次 wire / 2,155,240 tokens，逐样本完整性均为 complete，另有 10 次准入拒绝。74 个盲审候选与 74 个评分映射身份一致，全部保持未标注。新增源码验收入口实际核对了一条主代理非盲态预核验标注，报告仍为 pending，不计入独立质量成绩。

P1 聚焦验证：新协议、单 writer 调度、局部失败和 prepare→parse stub 链共 38 项通过；Ruff 通过。测试覆盖未知 Goal/target、旧证据、越界 read、冲突重复目标、错误 writer、read/submit 互斥及快照绑定。未运行 Provider、未改变生产 ReviewService 路由。

在 Python 可执行前，用 PowerShell 另准备了 `runtime-p0-r9-blind-preparation/` 材料，随后比对其 74 个 blind_id 与 Python 的正式包全部一致。该准备操作不当作 Python 测试证据。

独立只读代理仍因 429 未执行，不能当作核验。主代理对 ComfyUI/Ollama 六个候选完成非盲态源码抽查，并将证据保存在正式输出的 `scorer/static-source-spot-checks.json`；目标工作树 head 和 clean 状态已核对，没有执行目标代码。Ollama 的 uint 比较和既有新增边界测试提供具体反证，ComfyUI 的浮点掩码传递提供后续语义复核路径。这些材料不生成独立质量标签，不推进质量验收门。

用户已明确授权新建独立盲审聊天，以当前默认模型替代失效子代理。独立聊天读取 74 个候选包及五份冻结源码，不读取 scorer、参考答案、r9 终态或父线程预核验笔记；只允许保存派生标注。已确认聊天处于实际运行状态，尚未收到标注结果。

评分者专用的 `scorer/reference-code-preparation.json` 已按冻结数据哈希生成：43 条参考意见的原位置范围可从固定快照读取。其语义有效性、候选对应关系和遗漏状态全部未判断，recall 为 null；原文坐标可读不等于参考意见是真实缺陷。参考正文没有交给独立盲审聊天。

独立盲审聊天覆盖 74 项，初始标注 13 valid_defect、22 false_positive、28 duplicate、11 cannot_determine。主线通过 `runtime_adjudication_cli` 核对精确源码后，发现 5 个区间文本不匹配；没有接受其余正确项被拒绝的风险，建立独立校正聊天逐项修正。这 5 项为两条 false_positive 和三条 valid_defect。合并后的盲审标签为 16 valid_defect、19 false_positive、28 duplicate、11 cannot_determine；74/74 已标注、0 拒绝，所有 duplicate 均指向同包 valid_defect。正式验收器以 `--blind` 重新核对固定 Git blob 的逐字文本、路径、行号、side 和 SHA，结果 `adjudication_status=complete`、`rejected=0`。精确性校验本身通过不等于语义正确性证明；标签来自独立静态审查，运行代码与独立事实复核未进行。

本轮覆盖五个历史 smoke 样本中的候选，不是 AACR 全数据集评估。评分者只准备了这五样本的 43 条正参考意见源码范围；目前每条的语义有效性、候选匹配与遗漏状态仍为 `not_reviewed`，负样本 0 条完成审查。因此不计算 recall、precision 或质量收益，不满足扩大评测/切换默认引擎条件。已生成的盲审、评分和原始 r9 产物包含评测资料，不提交。

后续复核修正：三项源码文本修正同时改变了语义标签（注释语言、process.cwd()、无匹配 glob），原评审为 false_positive，补充评审为 valid_defect。不得将“补充评审引用更精确”当作“补充语义判断必然正确”。尤其在默认 Bash 中，未匹配 `temp/*.json` 保留字面量，已有 `-s` 检查会失败；补充标注中“循环不执行”的主张缺少依据。三项保留评审分歧，原始及补充产物均保留，暂不计为有效缺陷/误报定论。合并标签计数仅是原始评审标签统计；`adjudication_status=complete` 仅表示身份/引用验收完整。仍需明确评分规则、对分歧逐项裁决及遗漏语义核验，才能形成可用于 P5 的质量基线。P0 先前“质量门已通过”的表述过强，已在此纠正。
