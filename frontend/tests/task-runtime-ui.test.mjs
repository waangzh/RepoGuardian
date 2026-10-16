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
