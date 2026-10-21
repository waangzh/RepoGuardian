import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "vite";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";

test("审查进度以当前工作为主，缺少可靠数值时保留不定进度", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: Progress } = await server.ssrLoadModule("/src/components/pages/ReviewProgressPage.vue");
    const fixtures = JSON.parse(await readFile(new URL("./fixtures/publication-plans.json", import.meta.url), "utf8"));
    for (const percent of [65, 0, null, undefined, NaN, Infinity]) {
      const task = { ...fixtures.clean.task, status: "running", steps: [
        { name: "review", status: "running", message: "正在核对调用链", progress: { percent, detail: "检查返回值的消费路径" } },
      ] };
      const html = await renderToString(createSSRApp(Progress, { task, runtime: null, cancelling: false }));
      assert.match(html, /<h2>正在核对调用链<\/h2>/);
      assert.match(html, /检查返回值的消费路径/);
      assert.doesNotMatch(html, /plan-number|整轮审查或覆盖完成率|预计完成/);
      if (typeof percent === "number" && Number.isFinite(percent)) {
        assert.ok(html.includes(`当前步骤 ${percent}%`));
        assert.ok(html.includes(`aria-valuenow="${percent}"`));
      } else {
        assert.match(html, /当前步骤进行中/);
        assert.match(html, /rg-indeterminate/);
        assert.doesNotMatch(html, /aria-valuenow=/);
      }
    }
  } finally { await server.close(); }
});

test("概览只突出一个情景操作，证据在前、补充信息默认收起", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: Overview } = await server.ssrLoadModule("/src/components/pages/ReviewOverviewPage.vue");
    const fixtures = JSON.parse(await readFile(new URL("./fixtures/publication-plans.json", import.meta.url), "utf8"));
    for (const name of ["clean", "findings", "inconclusive", "failed", "cancelled"]) {
      const fixture = fixtures[name];
      const html = await renderToString(createSSRApp(Overview, {
        task: fixture.task, summaryPlan: fixture.summary, summary: null, findings: [], warnings: [], terminal: true,
      }));
      const page = html.replace(/<dialog\b[\s\S]*?<\/dialog>/g, "");
      assert.equal((page.match(/class="btn primary ov-main-action"/g) || []).length, 1, name);
      assert.doesNotMatch(page, /button--primary/);
      assert.ok(page.indexOf('id="overview-conclusion"') < page.indexOf("PR 目标与代码变化"));
      assert.ok(page.indexOf("问题与建议") < page.indexOf('aria-label="补充信息"'));
      assert.doesNotMatch(page, /<details[^>]*\sopen(?:[\s=>])/);
      if (name === "clean") assert.match(page, /查看完整报告/);
    }
    const fixture = structuredClone(fixtures.findings);
    fixture.summary.digest.merge_decision.blocking_finding_keys = ["important"];
    const row = (key, group) => ({ key, group, status: group, title: key, filePath: "src/a.py", line: 7 });
    const html = await renderToString(createSSRApp(Overview, {
      task: fixture.task, summaryPlan: fixture.summary, summary: null,
      findings: [row("suggestion", "confirmed"), row("dismissed-record", "dismissed"), row("important", "confirmed"), row("candidate", "unresolved")],
      warnings: [], terminal: true,
    }));
    assert.ok(html.indexOf("<strong>important</strong>") < html.indexOf("<strong>suggestion</strong>"));
    assert.doesNotMatch(html, /dismissed-record/);
    assert.match(html.replace(/<!--[\s\S]*?-->/g, ""), /未确认 · src\/a.py:7/);
  } finally { await server.close(); }
});
