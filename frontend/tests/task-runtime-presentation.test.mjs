import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";
const source = readFileSync(new URL("../src/utils/taskRuntimePresentation.ts", import.meta.url), "utf8");
const javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const { runtimeReport, runtimeLabel, retryableRuntimeUnits } = await import(`data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`);
test("新覆盖不能由旧缓存或旧引擎自动升级", () => {
  const report = { coverage_version: "task-goals-v1" };
  assert.equal(runtimeReport({ task_runtime_report: report }), null);
  assert.equal(runtimeReport({ engine: "legacy_units", task_runtime_report: report }), null);
  assert.equal(runtimeReport({ engine: "task_review_v1", task_runtime_report: report }), report);
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
