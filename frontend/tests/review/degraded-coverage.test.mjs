import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

const source = readFileSync(new URL("../../src/utils/reviewObservability.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } });
const ui = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);

test("执行完成但诊断降级时显示目标缺口，候选记录继续展示", () => {
  for (const target_coverage of ["partial", "unknown"]) {
    const result = { status: "completed", input_coverage: { target_coverage } };
    assert.equal(ui.unitFindingLabel(result, 0), "目标覆盖未完整，未报告问题");
    assert.equal(ui.unitFindingLabel(result, 1), "1 条问题记录");
  }
  assert.equal(ui.unitFindingLabel({ status: "completed", plan_skip_reason: "diagnosis_background_budget_degraded" }, 0),
    "目标覆盖未完整，未报告问题");
  assert.equal(ui.unitFindingLabel({ status: "completed", review_summary: { status: "unknown", input_protocol: "legacy" } }, 0),
    "未报告问题");
});

test("覆盖警告同时展示目标、契约和批次执行状态", () => {
  const warning = ui.unitCoverageWarning({
    review_unit_id: "u1",
    status: "completed",
    input_coverage: {
      target_coverage: "complete",
      contract_coverage: "partial",
      reason: "contract_dependency_unresolved",
      omitted_targets: [],
    },
    diff_batches: [{
      id: "b1",
      status: "partial",
      batch_execution_status: "no_new_context",
    }],
  });
  assert.match(warning, /目标覆盖 complete/);
  assert.match(warning, /契约闭合 partial/);
  assert.match(warning, /批次状态 b1:no_new_context/);
});
