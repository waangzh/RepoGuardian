import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "vite";
import { createSSRApp, h } from "vue";
import { renderToString } from "@vue/server-renderer";

const fixtures = JSON.parse(await readFile(new URL("./fixtures/publication-plans.json", import.meta.url), "utf8"));

test("新结构化预览显示冻结摘要来源，正文直接使用服务端 Markdown，旧格式仍能渲染", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: Preview } = await server.ssrLoadModule("/src/components/report/PrCommentPreview.vue");
    // 此处只核验结构化来源/计数与正文透传；Markdown 清洗由生产组件和浏览器承担。
    const { default: Markdown } = await server.ssrLoadModule("/src/components/report/ReportMarkdown.vue");
    Markdown.ssrRender = undefined;
    Markdown.render = function () { return h("pre", this.content); };
    const plan = fixtures.findings.plan;
    const presentation = { schema_version: "review-presentation-v1", review_id: plan.review_id,
      snapshot: plan.snapshot, language: "zh-CN", synthesis_status: "ready", purpose_text: null,
      purpose_source: "unknown", change_summary: "变更观察", test_changes: [], review_summary: "",
      verdict: plan.digest.merge_decision, findings: plan.inline_drafts,
      delivery: { confirmed: 3, eligible: 2, selected: 2, inline_ready: 1, summary_only: 1,
        not_selected: 0, already_published: 0, actual_published: null },
      validation_state: "not_executed", limitations: [] };
    const content = "## RepoGuardian · PR Review\n\n### 审查结果\n\n服务端保留的原始结论。\n\n### 验证与合并建议\n\n未执行目标测试。";
    const html = await renderToString(createSSRApp({ render: () => h(Preview, {
      content, presentation, findingCount: 1,
    }) }));
    for (const text of ["已冻结的模型编辑结果", "本轮已确认 3 项", "当前正文包含 1 项", "服务端保留的原始结论", "未执行目标测试"]) {
      assert.ok(html.includes(text), text);
    }
    assert.ok(!html.includes("0 项问题"));
    const legacy = await renderToString(createSSRApp({ render: () => h(Preview, { content: plan.review_comment_markdown }) }));
    assert.ok(legacy.includes("PR Code Review"));
    const utils = await server.ssrLoadModule("/src/utils/publicationPresentation.ts");
    assert.equal(utils.inlineActionLabel.summary_only, "仅在总结评论中反馈，不创建代码行讨论");
  } finally { await server.close(); }
});
