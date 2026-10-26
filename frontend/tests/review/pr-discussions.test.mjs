import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "vite";
import { createSSRApp, h } from "vue";
import { renderToString } from "vue/server-renderer";

test("历史讨论区分平台状态与代码核验，旧记录不显示零条", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: Component } = await server.ssrLoadModule("/src/components/review/PrDiscussionContext.vue");
    const old = await renderToString(createSSRApp({ render: () => h(Component, { reviewId: "old" }) }));
    assert.match(old, /未记录历史讨论/);
    assert.doesNotMatch(old, /已参考 0/);
    const html = await renderToString(createSSRApp({ render: () => h(Component, { reviewId: "current",
      stats: { status: "partial", collected: 5, comments: 12, referenced: 2, referenced_comments: 4,
        checked: 1, unchecked: 4, limitations: ["inline_thread_state_unavailable"], termination_reason: "budget_exhausted" } }) }));
    for (const text of ["已参考 2", "包含 4", "保存 12 条 PR 评论", "整理为 5 项讨论", "已有复核结论", "尚未核实充分", "线程已解决不代表代码问题已修复", "不完整"]) assert.ok(html.includes(text), text);
    assert.match(html, /<details class="pd-limits"><summary>讨论统计口径<\/summary>/);
    assert.ok(!html.includes("<pre>"));
  } finally { await server.close(); }
});

test("完整且为空的讨论使用中性空状态，未采集与不可用仍保留警示", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: Component } = await server.ssrLoadModule("/src/components/review/PrDiscussionContext.vue");
    const stats = { status: "complete", collected: 0, comments: 0, referenced: 0, referenced_comments: 0,
      checked: 0, unchecked: 0, limitations: [], termination_reason: null };
    const render = value => renderToString(createSSRApp({ render: () => h(Component, { reviewId: "empty", stats: value }) }));
    const empty = await render(stats);
    assert.match(empty, /采集已完成，未发现可用的 PR 讨论线程/);
    assert.doesNotMatch(empty, /pd-notice amber|pd-summary-grid|代码安全的证明/);
    assert.match(empty, /查看讨论与核验详情/);
    for (const [status, text] of [["not_recorded", "不代表没有评论"], ["unavailable", "无法判断"], ["partial", "采集不完整"]]) {
      const html = await render({ ...stats, status, limitations: ["保留的采集限制"] });
      assert.match(html, /pd-notice amber/);
      assert.ok(html.includes(text));
      assert.match(html, /保留的采集限制/);
      assert.doesNotMatch(html, /采集已完成，未发现/);
    }
  } finally { await server.close(); }
});

test("讨论请求编码任务及事项ID，只访问任务内只读接口", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  const previous = globalThis.fetch;
  try {
    const seen = [];
    globalThis.fetch = async (url, options) => { seen.push([url, options]); return new Response(JSON.stringify({ items: [] })); };
    const { getPrDiscussions } = await server.ssrLoadModule("/src/api/client.ts");
    const controller = new AbortController();
    await getPrDiscussions("task/id", { discussion_id: "inline:1", comment_offset: 20 }, controller.signal);
    assert.equal(seen[0][0], "/api/reviews/task%2Fid/discussions?discussion_id=inline%3A1&comment_offset=20");
    assert.equal(seen[0][1].signal, controller.signal);
    assert.equal(seen[0][1].method, undefined);
  } finally { globalThis.fetch = previous; await server.close(); }
});

test("历史重复项保留发布资格，默认不选但允许手动纳入", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: Component } = await server.ssrLoadModule("/src/components/review/publication/GitHubPublicationPanel.vue");
    const findings = [
      { finding_key: "new", title: "New defect", raw_title: "New defect", placement: "inline" },
      { finding_key: "old", title: "Existing defect", raw_title: "Existing defect", placement: "inline",
        default_selected: false, discussion_urls: ["https://github.com/o/r/pull/1#discussion_r1"] },
    ];
    const html = await renderToString(createSSRApp({ render: () => h(Component, { reviewId: "run",
      capability: { enabled: true, mode: "publish" },
      plan: { inline_drafts: findings, summary_findings: [] } }) }));
    assert.match(html, /<input(?=[^>]*value="new")(?=[^>]*checked)[^>]*>/);
    assert.doesNotMatch(html, /<input(?=[^>]*value="old")(?=[^>]*checked)[^>]*>/);
    assert.match(html, /已有讨论且未发现新增信息/);
    assert.match(html, /可手动纳入/);
  } finally { await server.close(); }
});
