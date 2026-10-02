import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

const source = readFileSync(new URL("../src/utils/reviewObservability.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const ui = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);

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
  const statuses = ["candidate", "evidence_resolved", "confirmed", "published", "needs_human", "dismissed"];
  const items = ui.issueStatusBreakdown({ unit_result: { issues: statuses.map((status) => ({ status })) } });
  assert.equal(items.reduce((sum, item) => sum + item.count, 0), 6);
  assert.equal(items[0].label, "候选待核验");
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
