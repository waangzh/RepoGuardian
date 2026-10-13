import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

const source = readFileSync(new URL("../src/utils/reviewObservability.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } });
const ui = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);

test("执行完成但存在未决契约时，前端区分执行与核验状态", () => {
  const warning = ui.unitCoverageWarning({
    review_unit_id: "u1",
    status: "completed",
    unit_completion_status: "complete_with_unresolved_contracts",
    input_coverage: {
      target_coverage: "complete",
      contract_coverage: "partial",
      contract_evidence_coverage: "complete",
      contract_verification_status: "unresolved",
      blocking_dependencies: [],
      omitted_targets: [],
    },
    diff_batches: [],
  });
  assert.match(warning, /契约证据 complete/);
  assert.match(warning, /契约核验 unresolved/);
  assert.equal(ui.unitFindingLabel({
    status: "completed",
    unit_completion_status: "complete_with_unresolved_contracts",
  }, 0), "审查执行完成，存在未决契约");
});
