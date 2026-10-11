import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("../src/utils/unitCoverage.ts", import.meta.url), "utf8");
const javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText;
const { unitCoverageComplete } = await import(`data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`);

test("检查目标完成但契约未闭合时，整体仍未完成", () => {
  for (const contract of ["partial", "unknown"]) {
    assert.equal(unitCoverageComplete({ status: "completed", input_coverage: {
      target_coverage: "complete", contract_coverage: contract, unresolved_questions: ["q"] } }), false);
  }
});

test("显式输入覆盖不能掩盖当前无效或缺失的记录", () => {
  for (const latest of ["invalid", "missing", "failed", "unknown"]) {
    assert.equal(unitCoverageComplete({ status: "completed", input_coverage: { target_coverage: "complete" },
      review_summary: { status: "reported", record: {}, latest_attempt_status: latest } }), false);
  }
});
