import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { readFile } from "node:fs/promises";

test("目标对照逐项呈现行为、证据、限制和未核验目标", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/review/PrChangeSummary.vue");
    const fixtures = JSON.parse(await readFile(new URL("./fixtures/publication-plans.json", import.meta.url), "utf8"));
    const task = fixtures.clean.task;
    const digest = { actual_changes: [], examined_goals: [], intent_assessments: [
      { author_target: "拒绝空值", status: "observed", observations: [{ actual_behavior: "空值先返回", path: "src/a.py", start_line: 7,
        side: "head", evidence_ids: ["e1"], limitations: ["调用方尚未核验"] }] },
      { author_target: "保护缓存", status: "not_checked", observations: [] }] };
    const html = await renderToString(createSSRApp(component, { task, digest }));
    for (const value of ["局部静态证据支持", "空值先返回", "src/a.py:7", "e1", "调用方尚未核验", "保护缓存", "尚未核验"]) assert.ok(html.includes(value));
  } finally { await server.close(); }
});

test("概览使用统一合并建议并区分补丁结果与 Head 验证", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/review/ReviewOutcomePanel.vue");
    const plan = { outcome: { kind: "clean", dynamic_validation: "independent_results", reason_codes: [] }, withheld_findings: [],
      digest: { merge_decision: { advice: "统一事实要求补查缓存路径", blocking_finding_keys: ["a"], nonblocking_finding_keys: [] },
        validations: [{ id: "v", source: "user_runner", stage: "patched", status: "passed", reason: "不代表当前 Head 已通过" }] } };
    const html = await renderToString(createSSRApp(component, { plan }));
    for (const value of ["已有独立补丁结果，Head 未知", "统一事实要求补查缓存路径", "不代表当前 Head 已通过", "阻断问题 1 项"]) assert.ok(html.includes(value));
  } finally { await server.close(); }
});
