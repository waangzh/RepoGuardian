import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";

test("重新审查只带入可编辑选择，范围与快照授权必须重新建立", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { rereviewInput } = await server.ssrLoadModule("/src/utils/reviewRestart.ts");
    const original = {
      pr_url: " https://github.com/o/r/pull/3 ", model: " saved-model ",
      confirmed_plan: { version: "review-plan-v1", depth: "deep", focus: ["security", "compatibility"],
        scope: { mode: "selected", primary_files: ["old.py"] },
        limits: { max_model_calls: 31, max_tokens: 90000, max_cost_microusd: 2000, timeout_seconds: 600 } },
      snapshot: { head_sha: "old-head" }, plan_hash: "old-hash", preview_id: "old-preview",
      mode: "review_suggest_and_validate", generate_patches: true, validation_backend: "project_ci",
    };
    const before = structuredClone(original);
    const input = rereviewInput(original);
    assert.equal(input.prUrl, original.pr_url.trim());
    assert.equal(input.model, "saved-model");
    assert.equal(input.plan.depth, "deep");
    assert.deepEqual(input.plan.focus, original.confirmed_plan.focus);
    assert.deepEqual(input.plan.limits, original.confirmed_plan.limits);
    assert.deepEqual(input.plan.scope, { mode: "all", primary_files: [] });
    assert.deepEqual(Object.keys(input).sort(), ["model", "plan", "prUrl"]);
    input.plan.focus.push("correctness");
    input.plan.limits.max_model_calls = 1;
    assert.deepEqual(original, before);
    for (const confirmed_plan of [null, undefined, { version: "future-plan" }]) {
      const legacy = rereviewInput({ pr_url: original.pr_url, confirmed_plan });
      assert.equal(legacy.plan, undefined);
      assert.equal(legacy.model, "");
    }
    for (const pr_url of ["", "https://evil.test/o/r/pull/1", "https://user:pass@github.com/o/r/pull/1"]) {
      assert.equal(rereviewInput({ ...original, pr_url }), null);
    }
  } finally { await server.close(); }
});
