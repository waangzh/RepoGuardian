import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";

test("零问题仍展示未核验依赖，并区分候选和已准入证据", async () => {
  const server = await createServer({ server: { middlewareMode: true }, appType: "custom" });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/review/runtime/UnitReviewRecord.vue");
    const html = await renderToString(createSSRApp(component, { result: {
      review_summary: { status: "unknown", evidence: [], reason: "contract_dependency_unresolved" },
      diff_manifest: { hunks: [] },
      coverage_ledger: { diff_hunks: {}, questions: {}, dependencies: { one: { status: "unresolved" } }, file_changes: {} },
      input_coverage: { blocking_dependencies: ["impl.py:contract"] },
      diff_batches: [{ id: "b", status: "completed", ranges: [], contract_handoff: {
        obligations: [], carryable_paths: ["impl.py"], dropped_obligations: [],
        candidate_evidence: [{ evidence_id: "e", file_path: "impl.py" }], selected_evidence_ids: [],
        carried_evidence_ids: [], omitted_evidence: [{ evidence_id: "e", file_path: "impl.py", reason: "same_file_support_char_limit" }],
      } }],
    } }));
    assert.match(html, /待解决问题 0 个/);
    assert.match(html, /未核验依赖 1 个/);
    assert.match(html, /候选 1 项/);
    assert.match(html, /正文已准入 0 项/);
    assert.match(html, /同文件正文超过交接上限/);
  } finally {
    await server.close();
  }
});
