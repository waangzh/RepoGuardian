import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "vite";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";

test("用户用语保留版本、范围与未发布记录的语义", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { reviewScopeLabel, reviewStatusLabel, reportValidationLabel } = await server.ssrLoadModule("/src/utils/reportPresentation.ts");
    assert.equal(reviewScopeLabel(12, 14), "已检查 12/14 项约定范围，2 项待补查");
    assert.equal(reviewScopeLabel(0, 14), "已检查 0/14 项约定范围，14 项待补查");
    assert.equal(reviewScopeLabel(null, 14), "检查范围尚不明确");
    assert.equal(reviewScopeLabel(14, 14), "已检查 14/14 项约定范围");
    assert.equal(reviewStatusLabel("confirmed"), "代码问题已确认，但暂不能生成可发布评论");
    assert.equal(reportValidationLabel("passed"), "当前 PR 版本已记录的对应检查通过");
    assert.equal(reportValidationLabel("unknown"), "未知");
    const { default: Summary } = await server.ssrLoadModule("/src/components/review/diff/PrChangeSummary.vue");
    const { default: Discussion } = await server.ssrLoadModule("/src/components/review/PrDiscussionContext.vue");
    const fixtures = JSON.parse(await readFile(new URL("../fixtures/publication-plans.json", import.meta.url), "utf8"));
    const html = await renderToString(createSSRApp(Summary, {
      task: fixtures.clean.task, compact: true,
      understanding: { author_goal: "目标", actual_changes: "代码观察", alignment: "尚未核实" },
    }));
    assert.match(html, /class="ov-tag">来自静态检查/);
    assert.doesNotMatch(html, /data-tone="success"/);
    const discussion = await renderToString(createSSRApp(Discussion, { reviewId: "fixture", stats: {
      status: "partial", comments: 12, collected: 5, referenced: 2, referenced_comments: 4,
      checked: 2, unchecked: 3, limitations: [], termination_reason: null,
    } }));
    assert.match(discussion, /保存 12 条 PR 评论，整理为 5 项讨论/);
    assert.match(discussion, /已有复核结论/);
    assert.match(discussion, /不代表每项都经过代码验证/);
    assert.match(discussion, /<details class="pd-limits"><summary>讨论统计口径/);
  } finally { await server.close(); }
});
