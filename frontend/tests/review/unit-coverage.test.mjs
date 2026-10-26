import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("../../src/utils/unitCoverage.ts", import.meta.url), "utf8");
const javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText;
const { unitCoverageComplete } = await import(`data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`);

function result() {
  return { status: "completed", diff_manifest: { review_unit_id: "u", input_hash: "hash", hunks: [{ id: "h" }] },
    coverage_ledger: { review_unit_id: "u", manifest_hash: "hash", diff_hunks: { h: { status: "reviewed" } },
      targets: { t: { required: true, status: "checked" } }, hypotheses: { risk: { required: true, status: "refuted" } },
      questions: {}, dependencies: {} } };
}

test("执行结束不能掩盖 pending Hunk、高风险假设或未决问题", () => {
  assert.equal(unitCoverageComplete(result()), true);
  for (const change of [
    (item) => item.coverage_ledger.diff_hunks.h.status = "pending",
    (item) => item.coverage_ledger.diff_hunks.h.status = "unsupported",
    (item) => item.coverage_ledger.hypotheses.risk.status = "unresolved",
    (item) => item.coverage_ledger.questions.q = { status: "pending" },
    (item) => item.coverage_ledger.manifest_hash = "stale",
    (item) => item.coverage_ledger.diff_hunks = {},
    (item) => item.input_coverage = { target_coverage: "partial" },
  ]) {
    const item = result();
    change(item);
    assert.equal(unitCoverageComplete(item), false);
  }
});

test("无文本证据的文件清单不能靠零长度区间伪装成已覆盖", () => {
  const item = result();
  item.diff_manifest.hunks[0].line_count = 0;
  item.diff_manifest.hunks[0].evidence_id = null;
  item.coverage_ledger.diff_hunks.h.reviewed_ranges = [[0, 0]];
  assert.equal(unitCoverageComplete(item), false);
});

test("旧响应可读，新账本缺失不能被当作覆盖完成", () => {
  assert.equal(unitCoverageComplete({ status: "completed" }), true);
  const item = result();
  delete item.coverage_ledger;
  assert.equal(unitCoverageComplete(item), false);
});

test("文件元数据核验与影响检查分别控制完成", () => {
  const item = result();
  item.diff_manifest.files = [{ file_path: "new.py", metadata_required: true, file_change_evidence: { id: "meta" } }];
  item.coverage_ledger.file_changes = { meta: { metadata_verified: true, impact_status: "pending", evidence_ids: [] } };
  assert.equal(unitCoverageComplete(item), false);
  item.coverage_ledger.file_changes.meta.impact_status = "checked";
  assert.equal(unitCoverageComplete(item), false);
  item.coverage_ledger.file_changes.meta.evidence_ids = ["caller-body"];
  assert.equal(unitCoverageComplete(item), true);
  item.coverage_ledger.file_changes.meta.metadata_verified = false;
  assert.equal(unitCoverageComplete(item), false);
});
