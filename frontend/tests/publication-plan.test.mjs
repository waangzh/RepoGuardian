import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";
import { createSSRApp, effectScope, nextTick, ref } from "vue";
import { renderToString } from "@vue/server-renderer";

test("Outcome 显示后端数量，不根据问题列表推断 Clean", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/review/ReviewOutcomePanel.vue");
    for (const [kind, status, label] of [["clean", "conditional_merge", "未发现阻断性问题"],
      ["findings", "fix_blockers", "建议修改后再合并"], ["inconclusive", "resolve_unknowns", "需要补充检查"],
      ["failed", "unavailable", "需要补充检查"], ["cancelled", "unavailable", "需要补充检查"]]) {
      const plan = { schema_version: "review-summary-v1",
        outcome: { kind, coverage: "partial", confirmed_count: 3, publishable_count: 2, unresolved_count: 1,
        examined_goals: 8, total_goals: 10, incomplete_investigations: 1, reason_codes: ["scope_incomplete"] },
        digest: { merge_decision: { status, label, advice: "先处理阻断问题" }, validations: [] },
        withheld_findings: [{ candidate_id: "c1", reason_code: "snapshot_mismatch" }] };
      const html = await renderToString(createSSRApp(component, { plan }));
      for (const expected of [label, "3", "2", "1", "8 / 10", "scope_incomplete", "snapshot_mismatch"]) assert.ok(html.includes(expected));
      assert.match(html, /逐条问题正文与发布状态/);
      // 三态合并建议始终展示，当前结论与服务端 label 一致。
      for (const option of ["未发现阻断性问题", "建议修改后再合并", "需要补充检查"]) assert.ok(html.includes(option));
      assert.match(html, /class="(?:danger|warning|success) active merge-option"/);
    }
  } finally { await server.close(); }
});

test("概览摘要与完整计划分离；切换任务、失败与卸载丢弃旧请求", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  const scope = effectScope();
  try {
    const { usePublicationPlan } = await server.ssrLoadModule("/src/composables/usePublicationPlan.ts");
    const task = ref({ id: "one", status: "completed" });
    const pending = [];
    const load = (id, signal) => new Promise((resolve, reject) => pending.push({ id, signal, resolve, reject }));
    const state = scope.run(() => usePublicationPlan(() => task.value, load, load));
    // 概览只读取轻量摘要；完整计划在没有页面请求前不会加载。
    assert.equal(pending.length, 1);
    state.ensurePlan(); await nextTick();
    assert.equal(pending.length, 2);
    task.value = { id: "two", status: "completed" };
    await nextTick();
    assert.equal(pending[0].signal.aborted, true);
    assert.equal(pending[1].signal.aborted, true);
    pending[0].resolve({ review_id: "one", schema_version: "review-summary-v1" });
    pending[1].resolve({ review_id: "one", schema_version: "review-publication-v1" });
    pending[2].reject(new Error("offline"));
    pending[3].reject(new Error("offline"));
    await nextTick(); await nextTick();
    assert.equal(state.summary.value, null);
    assert.match(state.error.value, /加载失败/);
    state.reload(); await nextTick();
    pending[4].resolve({ review_id: "two", schema_version: "review-summary-v1", outcome: { kind: "inconclusive" } });
    pending[5].resolve({ review_id: "two", schema_version: "review-publication-v1", outcome: { kind: "inconclusive" } });
    await nextTick(); await nextTick();
    assert.equal(state.summary.value.outcome.kind, "inconclusive");
    assert.equal(state.plan.value.outcome.kind, "inconclusive");
    task.value = { id: "two", status: "reviewing" }; await nextTick();
    assert.equal(state.summary.value, null);
    assert.equal(state.plan.value, null);
    task.value = { id: "three", status: "cancelled" }; await nextTick();
    scope.stop();
    assert.equal(pending[6].signal.aborted, true);
    pending[6].resolve({ review_id: "three", schema_version: "review-summary-v1" });
    await nextTick(); await nextTick();
    assert.equal(state.summary.value, null);
  } finally { scope.stop(); await server.close(); }
});

test("完整范围和计量限制分开展示；历史维度缺失保持未知", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/review/ReviewOutcomePanel.vue");
    const plan = { outcome: { kind: 'inconclusive', coverage: 'partial', review_completeness: 'complete',
      runtime_reliability: 'unreliable', dynamic_validation: 'not_executed', finding_conclusion: 'no_confirmed_findings', reason_codes: [] },
      digest: { merge_decision: { status: 'resolve_unknowns', label: '需要补充检查', advice: '先补齐限制' } }, withheld_findings: [] };
    const html = await renderToString(createSSRApp(component, { plan }));
    for (const text of ['约定检查范围：完整', '存在计量或执行限制', '未发现已确认缺陷', '未执行']) assert.ok(html.includes(text));
    assert.ok(!html.includes('覆盖部分完成'));
  } finally { await server.close(); }
});
