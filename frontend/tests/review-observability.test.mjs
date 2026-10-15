import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

const source = readFileSync(new URL("../src/utils/reviewObservability.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const ui = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);

test("缺失无效失败尝试保留旧记录时不会展示为本轮成功", () => {
  const record = { change_summary: "上次有效记录", target_checks: [{ status: "checked" }] };
  for (const status of ["missing", "invalid", "failed", "unknown"]) {
    const view = ui.unitRecordPresentation({ status: "unknown", record: null,
      last_valid_record: record, latest_attempt_status: status });
    assert.equal(view.record, record);
    assert.equal(view.retained, true);
    assert.equal(view.tone, "inconclusive");
    assert.match(view.label, /保留上次有效记录/);
  }
});

test("旧响应与尚无有效记录保持可读，新的有效记录替换历史投影", () => {
  assert.equal(ui.unitRecordPresentation(undefined).record, null);
  assert.equal(ui.unitRecordPresentation({ status: "unknown" }).label, "记录未知");
  const record = { target_checks: [{ status: "checked" }, { status: "not_checked" }] };
  assert.equal(ui.unitRecordPresentation({ status: "reported", record }).label, "1 / 2 个目标已检查");
  const current = ui.unitRecordPresentation({ status: "reported", record,
    last_valid_record: { target_checks: [] }, latest_attempt_status: "reported" });
  assert.equal(current.record, record);
  assert.equal(current.retained, false);
});

test("风险判定与执行状态分开，required不被描述为已确认缺陷", () => {
  const task = { status: "verifying_issues", cross_unit_risk: { decision: "required", execution_status: "not_implemented" }, coordination_plan: null };
  assert.equal(ui.crossUnitPresentation(task).decisionLabel, "需要跨组检查");
  assert.equal(ui.crossUnitPresentation(task).executionLabel, "等待协调");
  task.coordination_plan = { status: "validated" };
  assert.equal(ui.crossUnitPresentation(task).executionLabel, "定向补查进行中");
  task.status = "cancelled";
  assert.equal(ui.crossUnitPresentation(task).executionLabel, "已取消");
});

test("旧记录缺失不显示跳过，零问题不显示清洁", () => {
  assert.equal(ui.crossUnitPresentation({ status: "completed" }).decisionLabel, "筛查记录未知");
  assert.equal(ui.unitFindingLabel(undefined, 0), "等待审查");
  assert.equal(ui.unitFindingLabel({ status: "failed" }, 0), "检查未完成");
  assert.equal(ui.unitFindingLabel({ status: "completed" }, 0), "未报告问题");
});

test("无Hunk变更明确显示未覆盖与人工复核，保留文本候选计数", () => {
  const summary = { status: "unknown", latest_attempt_status: "not_executed", reason: "file_change_without_text_hunks" };
  assert.equal(ui.unitRecordPresentation(summary).label, "不支持自动审查，需人工复核");
  assert.equal(ui.unitFindingLabel({ status: "failed", terminal_reason: "unsupported_change" }, 0), "存在未覆盖变更，需人工复核");
  const mixed = { status: "completed", input_coverage: { reason: "file_change_without_text_hunks", target_coverage: "partial" } };
  assert.equal(ui.unitFindingLabel(mixed, 0), "存在未覆盖变更，需人工复核");
  assert.equal(ui.unitFindingLabel(mixed, 1), "1 条问题记录");
});

test("普通覆盖100%不能掩盖协调失败或整体完成情况未知", () => {
  const coverage = { coverage_rate: 1, unit_coverage_rate: 1, review_complete: false, coordination_status: "failed" };
  assert.equal(ui.coverageCompletionLabel(coverage), "整体审查未完整完成");
  assert.equal(ui.crossUnitPresentation({ status: "completed_with_warnings", coverage }).executionLabel, "协调失败");
  assert.equal(ui.coverageCompletionLabel({}), "整体完成情况未知");
  assert.equal(ui.coverageCompletionLabel({ review_complete: true }), "整体审查完整");
  assert.equal(ui.crossUnitPresentation({ status: "completed", coverage: { coordination_status: "unknown" }, cross_unit_risk: { decision: "required", execution_status: "failed" } }).executionLabel, "协调失败");
});

test("未生成、失败或取消的计划默认判定不伪装成已完成协调结论", () => {
  assert.equal(ui.coordinationDecisionLabel({ status: "proposed", decision: "uncertain" }), "判别尚未完成");
  assert.equal(ui.coordinationDecisionLabel({ status: "failed", decision: "required" }), "未形成完整协调结论");
  assert.equal(ui.coordinationDecisionLabel({ status: "cancelled", decision: "required" }), "协调已取消");
});

test("候选处理完成仍可未决，不推导为找到反证", () => {
  const result = ui.followupPresentation({ outcome: "unresolved", validation_status: "completed" }, "completed");
  assert.equal(result.label, "未决");
  assert.equal(result.validation, "候选处理已结束");
  assert.equal(ui.followupPresentation(undefined, "completed").label, "未执行或未保存结果");
});

test("无效补查与未执行和有效未决分别呈现", () => {
  const invalid = ui.followupPresentation({ outcome: "unresolved", validation_status: "completed",
    unit_result: { review_summary: { latest_attempt_status: "invalid" } } }, "completed");
  assert.equal(invalid.label, "审查记录无效");
  assert.equal(invalid.validation, "输出未通过校验，结论未采用");
  assert.equal(ui.planningQuestionLabels.pending, "尚未执行或未覆盖");
  assert.equal(ui.planningQuestionLabels.resolved, "已解决");
});

test("补查候选通过去重来源关联发布结果，不展示过滤项为确认问题", () => {
  const followup = { unit_result: { issues: [{ id: "candidate-a" }] } };
  const merged = { id: "published", status: "confirmed", source_issue_ids: ["candidate-a"] };
  const dismissed = { id: "candidate-a", status: "dismissed" };
  assert.deepEqual(ui.publishedFollowupIssues(followup, [merged, dismissed]), [merged]);
  assert.equal(ui.originalIssueUnit({ review_unit_id: "followup-1", source_review_unit_ids: ["u2", "u1"] }, ["u1", "u2"]), "u2");
  assert.equal(ui.publishedFollowupIssues(followup, [{ ...merged, status: "published" }]).length, 1);
});

test("Base与Head分别校验，缺少绑定保持未知", () => {
  const reference = { id: "a", file_path: "a.py", base_sha: "b", head_sha: "h" };
  assert.equal(ui.evidenceSnapshotState(reference, "b", "h"), "current");
  assert.equal(ui.evidenceSnapshotState(reference, "new-base", "h"), "stale");
  assert.equal(ui.evidenceSnapshotState(reference, "b", "new-head"), "stale");
  assert.equal(ui.evidenceSnapshotState({ id: "a", file_path: "a.py" }, "b", "h"), "unknown");
});

test("待核验、定位、确认、发布、过滤等状态计数覆盖整个候选批次", () => {
  const statuses = ["candidate", "evidence_resolved", "confirmed", "published", "unresolved", "needs_human", "dismissed"];
  const items = ui.issueStatusBreakdown({ unit_result: { issues: statuses.map((status) => ({ status })) } });
  assert.equal(items.reduce((sum, item) => sum + item.count, 0), 7);
  assert.equal(items.find((item) => item.label === "未确认").count, 1);
  assert.ok(!items.some((item) => item.label === "状态未记录"));
  assert.equal(items[0].label, "候选待核验");
});

test("未确认补查可打开证据，历史人工暂停记录不会继续等待", () => {
  const unresolved = { id: "uncertain", status: "unresolved" };
  const candidate = { id: "pending", status: "candidate" };
  const dismissed = { id: "filtered", status: "dismissed" };
  const followup = { unit_result: { issues: [unresolved, candidate, dismissed] } };
  assert.deepEqual(ui.publishedFollowupIssues(followup, [unresolved, candidate, dismissed]), [unresolved, candidate]);
  assert.equal(ui.isTerminal("waiting_for_human"), true);
});

test("零预算、实际用量超出预算和缺失成本保留正确语义", () => {
  assert.equal(ui.budgetPercent(0, 0), 0);
  assert.equal(ui.budgetPercent(1, 0), 100);
  assert.equal(ui.budgetPercent(120, 100), 100);
  assert.equal(ui.formatCost(null), "未提供");
  assert.equal(ui.formatCost(0), "$0.000000");
  assert.equal(ui.formatCost(12), "$0.000012");
});

test("证据目录包含原始与补查的引用，按ID去重并兼容旧快照", () => {
  const a = { id: "a", file_path: "a.py" };
  const b = { id: "b", file_path: "b.py" };
  assert.deepEqual(ui.evidenceCatalog({ review_unit_results: [{ review_summary: { evidence: [a] } }], followup_results: [{ unit_result: { review_summary: { evidence: [a, b] } } }] }), [a, b]);
  assert.deepEqual(ui.evidenceCatalog({ review_unit_results: [{}] }), []);
});
