import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "vite";
import { createSSRApp, effectScope, nextTick, ref } from "vue";
import { renderToString } from "@vue/server-renderer";

test("历史结论复用后端投影，缺失记录不转为成功", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { historyConclusion } = await server.ssrLoadModule("/src/utils/reviewHistoryPresentation.ts");
    const fixtures = JSON.parse(await readFile(new URL("../fixtures/publication-plans.json", import.meta.url), "utf8"));
    for (const [name, expected] of [["clean", "未发现需在合并前处理的问题"], ["findings", "有改进建议，暂无高优先级问题"],
      ["failed", "审查中断"], ["cancelled", "审查已停止"]]) {
      const { task, plan } = fixtures[name];
      const value = { ...task, history_result: { outcome: plan.outcome, merge_decision: plan.digest.merge_decision } };
      assert.equal(historyConclusion(value).title, expected);
    }
    const high = structuredClone(fixtures.findings);
    high.plan.digest.merge_decision.status = "fix_blockers";
    high.plan.digest.merge_decision.blocking_finding_keys = ["high"];
    assert.equal(historyConclusion({ ...high.task, history_result: { outcome: high.plan.outcome,
      merge_decision: high.plan.digest.merge_decision } }).title, "存在高优先级问题");
    assert.equal(historyConclusion({ status: "completed" }).title, "结论未知");
    assert.equal(historyConclusion({ status: "reviewing" }).title, "尚未形成结论");
    const { default: Confirmation } = await server.ssrLoadModule("/src/components/review/publication/ReviewCommentConfirmation.vue");
    for (const status of ["stale", "unknown"]) for (const action of ["copy", "download"]) {
      const html = await renderToString(createSSRApp(Confirmation, {
        pending: { status, reviewed_head_sha: "a".repeat(40) }, action, feedback: "",
      }));
      assert.ok(html.includes(action === "copy" ? "仍复制旧版审查记录" : "仍导出旧版审查记录"));
      assert.ok(html.includes(status === "stale" ? "重新审查此 PR" : "重新检查 PR 版本"));
      assert.ok(html.includes("不能作为最新提交的审查结论") && html.includes("a".repeat(40)));
    }
  } finally { await server.close(); }
});

test("同任务同快照刷新保留完整计划，跨快照和任务清空，过期响应丢弃", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  const scope = effectScope();
  try {
    const { usePublicationPlan } = await server.ssrLoadModule("/src/composables/usePublicationPlan.ts");
    const task = ref({ id: "one", status: "completed", run_manifest: { head_sha: "a", effective_base_sha: "b" } });
    const pending = [];
    const load = (id, signal) => new Promise((resolve, reject) => pending.push({ id, signal, resolve, reject }));
    const state = scope.run(() => usePublicationPlan(() => task.value, load, load, { autoPlan: true }));
    pending[0].resolve({ review_id: "one", schema_version: "review-summary-v1" });
    pending[1].resolve({ review_id: "one", schema_version: "review-publication-v1", snapshot: { head_sha: "a", effective_base_sha: "b" } });
    await nextTick(); await nextTick();
    const previous = state.plan.value;
    task.value = { ...task.value, task_runtime_report: {} };
    await nextTick();
    assert.equal(state.plan.value, previous);
    assert.equal(state.detailLoading.value, true);
    task.value = { ...task.value, run_manifest: { head_sha: "c", effective_base_sha: "b" } };
    await nextTick();
    assert.equal(state.plan.value, null);
    assert.equal(pending[3].signal.aborted, true);
    pending[3].resolve({ review_id: "one", schema_version: "review-publication-v1" });
    await nextTick(); await nextTick();
    assert.equal(state.plan.value, null);
    task.value = { ...task.value, id: "two" };
    await nextTick();
    assert.equal(pending[5].signal.aborted, true);
  } finally { scope.stop(); await server.close(); }
});
