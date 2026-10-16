import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";
const source = readFileSync(new URL("../src/utils/taskRuntimePresentation.ts", import.meta.url), "utf8");
const javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const { runtimeReport, runtimeLabel, retryableRuntimeUnits, relationshipGroups, relationshipEvidence, investigationRelationships } = await import(`data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`);
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
