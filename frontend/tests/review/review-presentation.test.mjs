import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "vite";
import { createSSRApp, h } from "vue";
import { renderToString } from "@vue/server-renderer";
import { marked } from "marked";

const fixtures = JSON.parse(await readFile(new URL("../fixtures/publication-plans.json", import.meta.url), "utf8"));

test("摘要按纯文本编码后不生成模型提供的标签、图片或自动链接", () => {
  const encoded = String.raw`&lt;img src=x onerror=alert\(1\)&gt; \!\[p\]\(https&#58;//evil\.invalid/x\) \[open\]\(javascript&#58;alert\(1\)\) &#64;everyone https&#58;//evil\.invalid low &lt; point &lt; high`;
  const html = marked.parse(encoded);
  assert.doesNotMatch(html, /<(?:a|img|script)\b/i);
  assert.ok(html.includes("low &lt; point &lt; high"));
  assert.ok(html.includes("https&#58;//evil.invalid"));
  assert.ok(html.includes("&#64;everyone"));
});

test("快照元信息在新旧评论预览中都使用中性颜色", async () => {
  const css = await readFile(new URL("../../src/styles/report-delivery.css", import.meta.url), "utf8");
  assert.match(css, /--comment-snapshot-bg: #f3f5f8/);
  assert.match(css, /--comment-snapshot-bg: var\(--rd-soft\)/);
  assert.match(css, /\.rd-markdown hr:last-of-type \+ p \{[\s\S]*?border-left: 3px solid var\(--rd-line\)/);
  assert.match(css, /\.rd-comment-snapshot \{[^}]*border-left: 3px solid var\(--rd-line\)/);
  assert.doesNotMatch(css, /#13a28b/);
});

test("PR 评论预览移除额外提示，保留服务端正文与旧格式渲染", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: Preview } = await server.ssrLoadModule("/src/components/report/PrCommentPreview.vue");
    // 此处只核验预览层提示与正文透传；Markdown 清洗由生产组件和浏览器承担。
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
    for (const [schema_version, synthesis_status] of [
      ["review-presentation-v1", "ready"], ["review-presentation-v1", "fallback"],
      ["review-presentation-v2", "ready"], ["review-presentation-v2", "fallback"],
    ]) {
      const html = await renderToString(createSSRApp({ render: () => h(Preview, {
        content, presentation: { ...presentation, schema_version, synthesis_status }, findingCount: 1,
      }) }));
      for (const text of ["服务端保留的原始结论", "未执行目标测试"]) assert.ok(html.includes(text), text);
      for (const text of ["摘要来源：", "本轮已确认 3 项", "当前正文包含 1 项", "0 项问题"]) {
        assert.ok(!html.includes(text), text);
      }
    }
    const { default: Panel } = await server.ssrLoadModule("/src/components/ReportPanel.vue");
    const panel = await renderToString(createSSRApp({ render: () => h(Panel, {
      markdown: "", reviewMarkdown: content, reviewId: plan.review_id,
      task: fixtures.findings.task, plan: { ...plan, presentation },
    }) }));
    assert.ok(!panel.includes("评论草稿不代表合并批准"));
    assert.ok(!panel.includes("复制完整评论只生成文本，不会创建 GitHub 代码行讨论"));
    assert.ok(panel.includes("服务端保留的原始结论"));
    const legacy = await renderToString(createSSRApp({ render: () => h(Preview, { content: plan.review_comment_markdown }) }));
    assert.ok(legacy.includes("PR Code Review"));
    const utils = await server.ssrLoadModule("/src/utils/publicationPresentation.ts");
    assert.equal(utils.inlineActionLabel.summary_only, "仅在总结评论中反馈，不创建代码行讨论");
  } finally { await server.close(); }
});
