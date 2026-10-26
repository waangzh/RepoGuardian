import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";

// 颜色语义：执行状态、证据状态、风险等级互不复用；成功色只表示正向完成。
test("状态徽章按执行/证据/风险三条语义轴着色", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: StatusBadge } = await server.ssrLoadModule("/src/components/common/StatusBadge.vue");
    const toneOf = async (status, label) => {
      const html = await renderToString(createSSRApp(StatusBadge, { status, label }));
      return /data-tone="([^"]+)"/.exec(html)?.[1];
    };
    // 执行状态：只有真正完成的执行才使用成功色。
    assert.equal(await toneOf("completed"), "success");
    assert.equal(await toneOf("passed"), "success");
    assert.equal(await toneOf("running"), "info");
    assert.equal(await toneOf("failed"), "danger");
    assert.equal(await toneOf("cancelled"), "neutral");
    // 证据状态：已核验是事实，不是好消息；待确认、已排除各自区分。
    assert.equal(await toneOf("confirmed"), "info");
    assert.equal(await toneOf("published"), "info");
    assert.equal(await toneOf("unresolved"), "purple");
    assert.equal(await toneOf("needs_human"), "purple");
    assert.equal(await toneOf("dismissed"), "neutral");
    // 风险等级：用风险色，低等级也不冒充成功。
    assert.equal(await toneOf("critical"), "danger");
    assert.equal(await toneOf("high"), "danger");
    assert.equal(await toneOf("medium"), "warning");
    assert.equal(await toneOf("low"), "neutral");
    // 已确认的高危问题不能同时叠加绿色成功标签。
    for (const status of ["confirmed", "high", "critical"]) assert.notEqual(await toneOf(status), "success", status);
    // 旧调用方直接把 tone 当 status 传入时保持原样。
    for (const tone of ["neutral", "info", "success", "warning", "danger", "purple"]) assert.equal(await toneOf(tone), tone);
  } finally { await server.close(); }
});
