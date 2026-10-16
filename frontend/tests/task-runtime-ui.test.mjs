import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";

test("概览保留未决调查与未知用量，已排除记录不挤入待处理列表", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/review/TaskRuntimePanel.vue");
    const report = { execution_status: "completed", termination_reason: "plan_exhausted", scope_complete: true,
      initial_scope_coverage: [{ status: "examined" }], admitted_investigations: [{ status: "insufficient_evidence" }],
      findings: [{ key: "a", status: "unresolved", candidate: { claim: "需要业务确认" } }, { key: "b", status: "dismissed", candidate: { claim: "已排除的误报" } }],
      usage_integrity: "unknown", unknown_attempts: 1, work_items: [], unscheduled_leads: [] };
    const html = await renderToString(createSSRApp(component, { report, terminal: true, retryingUnitId: null, view: "overview" }));
    assert.match(html, /1 项调查证据不足/);
    assert.match(html, /1 次调用用量未知/);
    assert.match(html, /需要业务确认/);
    assert.doesNotMatch(html, /已排除的误报/);
    assert.doesNotMatch(html, /completed/);
  } finally { await server.close(); }
});

test("已回答调查与关联线索默认折叠，原始问题及依据仍可追溯", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/review/TaskRuntimePanel.vue");
    const report = { execution_status: "completed", termination_reason: "plan_exhausted", scope_complete: true,
      initial_scope_coverage: [], admitted_investigations: [{ work_item_id: "answered", status: "answered", answer: "supported", proposition: "原始兼容性命题", reason: "完整检查依据", limitations: ["仅覆盖静态路径"] }],
      findings: [], usage_integrity: "complete", unknown_attempts: 0, sent_calls: 0, settled_tokens: 0, work_items: [], unscheduled_leads: [], relationship_hints: [{ lead_id: "hint", status: "hint" }] };
    const html = await renderToString(createSSRApp(component, { report, terminal: true, retryingUnitId: null, view: "checks" }));
    assert.match(html, /<details class="runtime-investigation">/);
    assert.match(html, /<details class="runtime-disclosure">/);
    assert.match(html, /原始兼容性命题/);
    assert.match(html, /完整检查依据/);
    assert.match(html, /仅覆盖静态路径/);
    assert.doesNotMatch(html, /<details[^>]*\sopen/);
  } finally { await server.close(); }
});
