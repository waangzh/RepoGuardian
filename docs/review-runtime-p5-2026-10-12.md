# P5：实验准备与真实对照

P0–P4 已本地提交。P5 默认仍保留 `legacy_units`，五个开发样本不能批准切换默认引擎。

本轮结论：**P5 筛选失败，停止付费扩展，不切换默认引擎，不进入留出集。** 实验底座及暴露的实现缺陷已修复并验证；模型仍漏掉独立有效缺陷，技术通过不等于质量通过。后续须另立不依赖参考答案的质量修复与冻结实验，当前整体改造目标保持未完成。

## 实验条件与组别

复用 r9 的五样本轨迹，不再付费重跑旧引擎。使用相同 `deepseek-flash`、SHA、80 行上下文规范 diff、完整 Git tracked 安全读取权限；并发 4、单请求 60 秒、最多 2 次发送尝试、输出 4096、样本 1800 秒，每样本统一上限 200 次 wire / 100 万 tokens。供应商实际 usage 与准入估算分别保留。

| 组 | 跨文件调查 | 新运行时的 Unit 期限 | 用途 |
| --- | --- | --- | --- |
| `protocol_180` | 暂停调度，线索仍保留，独立验证保留 | 180 秒 | 简化新协议的接入对照 |
| `full_180` | 开启 | 180 秒 | 在相同新协议和期限下观察调查增量 |
| `full_runtime` | 开启 | 无单 Unit 期限，保留样本期限 | 单独观察移除 Unit 期限的影响 |

三组都复用新队列、安全、预算、恢复和逐项验收底座；`protocol_180` 不是把新协议嵌入旧 Agent 循环。180 秒从该 Unit 的首个动作开始，跨分页、派生项和恢复不重置。此范围与旧验证器独立节点的生命周期并不完全等同；r9 与简化组的差异不能全部归因于 Prompt 字段减少。`full_180` 与 `full_runtime` 才是新运行时内单独改变期限的对照。

每组先运行 ComfyUI，核对真实 Provider、wire/usage、冻结范围、运行状态后才允许运行余下四个。三组各自使用新的 first / remaining 目录。首样本若没有实际工具事件，报告明确标为未触发；工具连接另由真实 Provider SDK 替身加受控文件读取回归覆盖，不冒称已在该付费样本验证。

冻结清单最多授权 15 个新样本、3000 次 wire、1500 万 tokens；这是三个组全部扩展的上限，不是预计消费。失败和取消仍保留在结果分母。重试或重复实验需要新的审计记录，不能覆盖旧目录或借新目录重置一个已运行样本的预算。

## 对照前修复

P4 将工具读取范围截断为最多 40 个文件，导致大仓库调用方取证权限与 r9 不同。新增运行将完整合法 tracked 路径集合冻结在运行规范中，任务只携带范围哈希和最多 40 个相关文件。Prompt 不反复携带仓库路径全集；全集只保存为共享内容寻址 artifact，恢复核对哈希及正文。读取、搜索结果、证据、调查与验证均使用同一冻结权限，realpath、敏感路径、tracked 和固定快照检查继续执行。

已创建的 P4 配置没有 `readable_scope_version`，恢复继续使用其原始范围，不静默扩权。新的配置冻结 `repository-tracked-v1`。实验在发送模型请求之前核对完整范围哈希。

`prepare-runtime-experiments` 在非 Reviewer 进程中静态核对旧结果、账本、规范 diff、固定 SHA、读取权限及模型窗口。Reviewer 只读无答案清单；`run` 仍派生干净子进程，不加载质量标签或参考答案。

## 可复现命令

以下命令在 `backend/` 执行；均不在被审查仓库运行代码。

```powershell
uv run python -m app.evaluation prepare-runtime-experiments --lock evaluation/aacr.lock.json --diff-lock evaluation/aacr.diff-v2.json --selection evaluation/smoke-v2-flash.json --baseline-run .repoguardian/evaluation-runs/smoke-20261012-flash-r9-first --baseline-run .repoguardian/evaluation-runs/smoke-20261012-flash-r9-remaining --source-roots .repoguardian/evaluation-runs/runtime-p0-r9-blind-preparation/scorer/source-roots.json --output .repoguardian/evaluation-runs/runtime-p5-plan-20261012-v3.json

uv run python -m app.evaluation run --lock evaluation/aacr.lock.json --diff-lock evaluation/aacr.diff-v2.json --experiment-plan .repoguardian/evaluation-runs/runtime-p5-plan-20261012-v3.json --experiment-group protocol_180 --experiment-stage first --sample-id comfyanonymous__ComfyUI@b55c224 --output .repoguardian/evaluation-runs/runtime-p5-protocol-180-first-20261012
```

清单或输出已存在时必须使用新路径。后四个需要 `--experiment-stage remaining`、严格的冻结样本顺序，以及同组 `first-gate.json` 的显式输入；禁止用另一组首样本通过代替。

## 验收与限制

硬门包括安全拒绝、冻结条件一致、wire 无漏记或重复结算、unknown 不填零、取消与恢复、旧任务权限兼容。质量门需要独立有效缺陷、误报、重复、无法判断与参考意见遗漏裁决，不能由 confirmed、引用正确或覆盖完成推导。

首轮收益门预先冻结为 tokens 至少下降 25%、固定 diff 未检查比例不增、独立有效缺陷不减、误报不增。单位有效缺陷成本在有效缺陷为零或质量尚未裁决时为未定义。范围不可比或用量未知时，不计算收益达标。

P0 的独立标注仍有语义分歧，43 条参考意见的语义/映射/遗漏尚未独立完成；本轮两个参考核验子代理均返回 429。该状态保留为未完成质量门，不能把候选校正合并结果当真值。留出集与正式阈值仍须在切换前冻结，覆盖语言、规模、负例、非文本变更、跨文件及证据不可得场景。

上述为实验启动时状态。用户随后授权的新独立质量评审聊天已完成第二轮核验：74 候选裁为 9 有效独立缺陷、40 误报、18 重复、7 无法判断；43 参考裁为 10 成立、29 不成立、4 无法判断。成立参考中 8 条由有效历史候选覆盖，2 条遗漏（Checkmate 语言列表清理、Ollama uint 转 int 上界）。新候选包为空，独立报告保留 10 条有效参考未发现，不把零候选当无缺陷。主线程另外验收候选 74/74 及参考 43/43 的固定源码引用与身份/映射，无拒绝项；这是引用验收与独立语义裁决的分别记录，不构成全量 recall 或泛化真值。

离线验证：后端全量 1198 passed、2 skipped；后续实验门和兼容修正的 82 项聚焦复验通过；Ruff 通过；前端 23 项测试及 vue-tsc/Vite build 通过。尚未做浏览器视觉验收。

固定范围对照从 r9 当前有效记录、快照一致的覆盖账本与完整 reviewed_ranges 提取；历史无效/失败/未知记录不算已检查。新组缺失、blocked、unsupported 仍在固定分母中。`compare-runtime-experiment` 输出逐样本与组内 tokens 对照；独立质量尚未完成时不输出通过切换结论。

最终运行清单 `runtime-p5-plan-20261012-v3.json` 的哈希为 `c1e923f2ec5675540ff0b948f75747ac07bb6c746bfb98671e794c679f920c93`。初次启动因评测入口显式覆盖默认模型窗口，在派发前被预检拒绝，未消耗模型请求。修正入口后按 r9 的真实模型注册表窗口继续。

v3 首样本在运行时准备原文期间失败：末尾空白物理行被文本拼接丢失，触发 `invalid_evidence_source_range`。记录为 0 wire、0 tokens，首样本门拒绝，不派发余下四个。修复原文和分页后，47 项相关复验通过。保留失败目录与派发回执，另冻结 v4 清单，哈希 `594ac5d9650c0668ab6ce1902d40045f2593ee2e8fe59b56a4bdb24f0b28970a`；运行入口新增实现指纹一致性核对，禁止同组中途改代码后继续派发。

v4 首样本真实消费 7 次 wire、26,683 tokens，对账 complete，无 unknown；执行技术失败，固定三个目标均未被接受为 examined。较低 tokens 不能作为效率门通过，固定未检查比例从 r9 的 1/3 上升为 3/3。日志显示输出回显 `type=json_object`、一次自动纠正后仍失败，以及读取未获得可用原文。首样本门拒绝，余下四个未派发。

随后在业务 Prompt 中提供绑定实际目标和证据 ID 的直接 JSON 示例，明确禁止 API 传输字段与包装；继续严格拒绝额外字段，不通过清洗模型输出掩盖契约错误。评测旁路保存新协议原始响应，报告工作项保留最后解析/工具观察。87 项相关复验与 Ruff 通过；另冻结 v5 重试清单，保留 v4 费用，顺序运行各组，任何首样本门失败都停止扩展。

v5 清单哈希 `f49a8e092e864e2fa94f8e53e719a44d10a564323c5bb790bd98acf7a11ecaee`。简化组首样本 3 次 wire、12,765 tokens，全部三目标接受为 examined，接入门通过；没有实际工具调用，不承担工具连接证明和语义正确性证明。随后四样本全部技术失败：Gemini CLI 4 次/31,815 tokens、Checkmate 3 次/9,736 tokens、Ollama 3 次/18,300 tokens、RAGFlow 4 次/16,005 tokens。该组五样本共 17 次 wire、88,621 tokens，对账 complete，固定范围门不通过，不能把相对 r9 的消费下降当成有效收益。

扩展轨迹定位到另一生产缺陷：Evidence Store 会按 80 行/4000 字符切规范块，新执行器却将完整读取正文配到首块引用哈希与区间，下一步 prepare 校验失败。修复为先生成规范块，再逐块持久保存、激活和验收；新增读取 100 行跨块后继续准备请求的回归。修复在冻结组结束后完成，不把不同实现的运行拼成同组成功。

已启动的顺序脚本在运行期间仍持有旧控制逻辑，扩展失败后进入完整组首样本。发现后停止调度父进程，保留已派发子进程的结算和输出，不继续派发完整组余下四个或无 Unit 期限组。该额外首样本也单列消费，不从失败结果中删除。

v5 完整组首样本最终 7 次 wire、36,530 tokens，对账 complete，但只接受 1/3 固定目标，接入门拒绝。所有已执行 P5 修复尝试共 31 次 wire、151,834 tokens（含 v4，v3 零消费）；失败与修复的成本均保留。旧新引擎对照暂未通过效率/覆盖/质量联合门。

v6 清单哈希 `6b0c2820fbca3c32175586e84555c82fb3293393b184920fc188f1398ef9558c`。跨块读取修复与显式新路径工具审计后，42 项聚焦复验及 Ruff 通过。首样本 4 次 wire、20,933 tokens，实际工具读取 1 次，3/3 目标接受，接入门通过。该结果只证明接入可继续扩展，不证明质量或泛化。此前最终全后端回归 1202 passed、2 skipped，后续跨块修复的 82 项复验通过。

v6 简化组余下四个样本均完成接入：Gemini CLI 4 次/32,058 tokens、Checkmate 4 次/14,602 tokens、Ollama 4 次/24,468 tokens、RAGFlow 7 次/38,117 tokens。五样本 23 次/130,178 tokens，用量 complete，逐样本固定范围均不劣于 r9。该组没有生成候选；模型声明 examined 不能证明无缺陷，成本下降不能掩盖漏检，质量门仍未通过。用户明确授权创建新的独立质量评审聊天，已派发 74 个隔离历史候选及 43 条未经信任参考意见，分阶段进行语义与遗漏裁决。

v6 完整组五样本共 81 次 wire、582,891 tokens；首样本 10 次/58,571 tokens，四次真实读取和三项调查，接入门通过。扩展结果为 Gemini CLI 13 次/127,829 tokens、Checkmate 5 次/18,135 tokens、Ollama 技术失败 33 次/257,556 tokens、RAGFlow 技术失败 20 次/120,800 tokens。全部用量可对账，固定范围不劣于 r9，但仍零候选。停止后续无 Unit 期限组派发。

实际 Prompt 核对又发现 P4 缺失规范变更正文：Head 原文与 diff 哈希不能让模型识别旧行、删除和新增。两个 v6 组不能证明同等证据输入下的协议收益。已补规范 hunk 双侧行、原始 hash 与目标绑定；容量分页只呈现当前 Head 范围对应的新增/删除/上下文行，不把双侧 diff 当可引用 Head 证据。旧 P4 任务仍使用冻结原输入。75 项相关复验通过，分页与删除行专项另行验证。

v7 首样本使用完整规范变更输入，3 次 wire、24,714 tokens，3/3 初始目标接受，但生成零候选。供应商理由把 `torch.empty(...).triu_(1)` 的上三角说成零，忽略未初始化内存与因果遮罩失效。独立历史盲审在 ComfyUI 裁为 1 个有效缺陷、2 个重复。因此 `quality-screening.json` 明确为 `failed / independent_valid_defects_lost`，不派发后四个或后续组。此拒绝门只在独立基线完整且新包确定为零时成立，不将未知、新标注缺失或 malformed 响应当零。

本轮全部真实尝试共 **138 次 wire / 889,617 tokens**，包含失败与修复重试；对账无未知、无漏记或重复结算。不得只报 v7 的 24,714 tokens 而隐藏调试消费。无 Unit 期限组未运行。

最终代码验证：全后端 **1205 passed、2 skipped**（239.60 秒）；后续新增质量拒绝门的 **11 项专项通过**；Ruff 与差异空白检查通过。前端仍为 23 项测试及类型/构建通过。分页删除行专项 **2 项通过**。本阶段实现和停止结论只提交本地，不推送。

## P0 标注分歧的主代理非盲静态复核

以下核验已看到历史标签，不作为新的独立盲审或评分真值，不覆盖原标注文件。

Checkmate 固定 Head `29e03a6294edfe43b49eba20fe49099cef601d34`：脚本 49–53 行的土耳其语注释不改变执行行为；现有材料没有建立强制注释语言约束，不能凭维护风格主张确认代码缺陷。脚本 64 行以 `process.cwd()/temp` 写入；实际 workflow 62–64 行在同一下载步骤先 `mkdir -p temp`，再从工作区根目录调用脚本；安装步骤的 `cd .github/scripts` 在前一个独立步骤，不会改变下载步骤的 cwd。因此“实际下载步骤写到错误 temp 目录”的主张缺少成立条件。

workflow 69–74 行在无匹配 glob 时仍以 Bash 默认未展开的字面量进入循环，`[ ! -s "$file" ]` 随后退出 1；“无匹配时循环不执行并通过验证”的校正理由不成立。仅静态读取固定 Git blob，未运行目标仓库脚本、安装或测试。三项冲突继续阻止把合并标签视为质量基线已经确定。
