import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";
const source = readFileSync(new URL("../src/utils/taskRuntimePresentation.ts", import.meta.url), "utf8");
const javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const { runtimeReport, runtimeLabel, retryableRuntimeUnits, relationshipGroups, relationshipEvidence, investigationRelationships, taskRuntimeSummary, runtimeFindingRows, filterRuntimeFindings, runtimeWarnings, reviewCompletionMessage } = await import(`data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`);
test("新覆盖不能由旧缓存或旧引擎自动升级", () => {
  const report = { coverage_version: "task-goals-v1" };
  assert.equal(runtimeReport({ task_runtime_report: report }), null);
  assert.equal(runtimeReport({ engine: "legacy_units", task_runtime_report: report }), null);
  assert.equal(runtimeReport({ engine: "task_review_v1", task_runtime_report: report }), report);
});

test("关系分组保留每条身份、符号、位置和未决原因", () => {
  const relation = { type: "calls", source_file: "a.py", target_file: "b.py", source: "a.py::first",
    target: "b.py::value", resolution_status: "unresolved", resolution_basis: "shadowed",
    call_sites: [{ line: 7, expression: "value" }] };
  const values = [{ lead_id: "one", status: "not_scheduled", reason: "unverified", relationship: relation, related_goal_ids: ["goal"] },
    { lead_id: "two", status: "not_scheduled", reason: "unverified", relationship: { ...relation, source: "a.py::second" } }];
  const original = structuredClone(values);
  const groups = relationshipGroups(values);
  assert.equal(groups.length, 1);
  assert.deepEqual(groups[0].members, values);
  assert.match(relationshipEvidence(values[0]), /one.*a.py::first.*7: value.*unresolved.*shadowed.*goal/);
  assert.match(relationshipEvidence(values[1]), /two.*a.py::second/);
  assert.deepEqual(values, original);
  assert.equal(relationshipGroups([...values, { ...values[0], status: "duplicate" }]).length, 2);
  assert.equal(relationshipGroups([...values, { ...values[0], reason: "budget" }]).length, 2);
  assert.deepEqual(investigationRelationships({ relationship_leads: values }), values);
});

test("旧报告缺失关系字段时不猜测绑定和合并，未知保持可见", () => {
  assert.deepEqual(relationshipGroups(), []);
  assert.equal(relationshipGroups([{ proposition: "旧描述" }, { proposition: "旧描述" }]).length, 2);
  assert.match(relationshipEvidence({}), /未知调用方.*未知目标.*unknown/);
  assert.deepEqual(investigationRelationships({}), []);
});
test("未知用量与缺口分别显示，unknown 工作项不能免费重试", () => {
  const report = { initial_scope_coverage: [{ goal_id: "g", status: "not_examined" }],
    work_items: [{ unit_id: "u1", state: "unknown", goal_ids: ["g"] },
                 { unit_id: "u2", state: "failed", goal_ids: ["g"] },
                 { unit_id: "u3", state: "completed", goal_ids: ["done"] }] };
  assert.deepEqual(retryableRuntimeUnits(report), ["u2"]);
  assert.equal(runtimeLabel("unknown"), "未知");
  assert.equal(runtimeLabel("insufficient_evidence"), "证据不足");
});

function mixedReport(updates = {}) {
  return { coverage_version: "task-goals-v1", execution_status: "completed", termination_reason: "plan_exhausted", scope_complete: true,
    initial_scope_coverage: [{ status: "examined" }], admitted_investigations: [], usage_integrity: "complete", unknown_attempts: 0,
    findings: [{ key: "a", status: "confirmed", candidate: { claim: "已确认" }, issue: { id: "issue-a" } },
      { key: "b", status: "unresolved", candidate: { claim: "待确认" }, issue: null, reason: "candidate_derivation_incomplete" },
      { key: "c", status: "dismissed", candidate: { claim: "已排除" }, issue: null }], ...updates };
}

test("内部发现保留审计文本，前端不再判定发布资格或生成公开正文", () => {
  const issue = { id: "a", title: "核验标题", placement: "summary", failure_scenario: "失败场景", affected_behavior: "业务影响", recommendation: "修改建议", primary_evidence: { file_path: "src/stats.py", resolved_start_line: 12, resolution_status: "exact", resolved_side: "base" } };
  const report = mixedReport();
  report.findings[0].issue = issue;
  const task = { engine: "task_review_v1", task_runtime_report: report, issues: [issue], pr: { title: "PR 标题", number: 1, head: { sha: "snapshot-sha" } } };
  const rows = runtimeFindingRows(report, task.issues);
  assert.equal(rows[0].title, "核验标题");
  assert.equal(rows[0].failureScenario, "失败场景");
  assert.equal(rows[0].recommendation, "修改建议");
  assert.equal(rows[1].group, "unresolved");
  assert.ok(rows.every(row => !("draftEligible" in row)));
  assert.doesNotMatch(source, /runtimeCommentDraft|runtimeCommentsMarkdown|runtimeUserReport/);
});

test("完整问题与无 Issue 候选独立可达，已排除不混入需关注计数", () => {
  const report = mixedReport();
  const issue = { id: "issue-a", source_issue_ids: [] };
  const rows = runtimeFindingRows(report, [issue]);
  assert.equal(rows[0].issue, issue);
  assert.equal(rows[1].issue, null);
  assert.deepEqual(filterRuntimeFindings(rows, "active").map(row => row.key), ["a", "b"]);
  assert.deepEqual(filterRuntimeFindings(rows, "unresolved").map(row => row.key), ["b"]);
  assert.deepEqual(filterRuntimeFindings(rows, "dismissed").map(row => row.key), ["c"]);
  assert.deepEqual(filterRuntimeFindings(rows, "unknown"), []);
  assert.equal(taskRuntimeSummary(report).findings.active, 2);
  assert.match(rows[1].reason, /尚未完成/);
  assert.match(reviewCompletionMessage({ engine: "task_review_v1", issues: [issue], task_runtime_report: report }), /1 项已确认问题，1 项待确认候选/);
});

test("零候选不能掩盖调查、范围、未知用量和异常结束", () => {
  const report = mixedReport({ findings: [], admitted_investigations: [{ status: "insufficient_evidence" }, { status: "not_executed" }], usage_integrity: "unknown", unknown_attempts: 1 });
  assert.match(runtimeWarnings(report).join(" "), /1 项调查证据不足.*1 项调查尚未执行.*1 次调用用量未知/);
  assert.equal(taskRuntimeSummary(report).scope.incomplete, 0);
  for (const execution_status of ["failed", "cancelled"]) assert.ok(runtimeWarnings({ ...report, execution_status }).length);
  assert.match(runtimeWarnings({ ...report, initial_scope_coverage: [] }).join(" "), /尚无可核对的范围记录/);
  assert.match(runtimeWarnings({ ...report, termination_reason: "budget_exhausted" }).join(" "), /预算耗尽/);
});

test("未知发现状态保留，历史通知不自动升级为新引擎", () => {
  const report = mixedReport({ findings: [{ status: "future_state", candidate: { claim: "新状态" } }] });
  assert.equal(taskRuntimeSummary(report).findings.unknown, 1);
  assert.equal(runtimeFindingRows(report)[0].status, "future_state");
  assert.match(runtimeWarnings(report).join(" "), /状态未知/);
  assert.match(reviewCompletionMessage({ engine: "legacy_units", issues: [], task_runtime_report: report }), /未发现达到报告门槛/);
  assert.equal(runtimeLabel("completed"), "审查执行已结束");
});
