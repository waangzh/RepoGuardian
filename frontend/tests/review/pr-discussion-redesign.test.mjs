import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "vite";
import { effectScope, nextTick, ref } from "vue";

const stats = { status: "complete", collected: 101, comments: 140, referenced: 2, referenced_comments: 3, checked: 1, unchecked: 100, limitations: [], termination_reason: null };
const item = (id = "inline:1") => ({ id, source: "inline", path: "src/file.ts", resolved: true,
  outdated: null, complete: true, referenced: true, comment_count: 21, url: "https://github.com/o/r/pull/1",
  comments: [{ id: "c1", body: "# Historical claim", author: "reviewer", url: null, created_at: null }],
  summary: null, recheck: { discussion_id: id, status: "still_present", reason: "需要检查 boundary", evidence_ids: [], limitations: [] }, evidence: [] });

test("讨论状态、原文标题、安全链接与搜索保持独立语义", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const ui = await server.ssrLoadModule("/src/utils/prDiscussionPresentation.ts");
    assert.equal(ui.discussionTitle(item()), "Historical claim");
    assert.equal(ui.recheckLabel("unknown-future"), "未知核验状态");
    assert.equal(ui.recheckTone("insufficient_evidence"), "amber");
    assert.equal(ui.matchesDiscussion(item(), "verified", "BOUNDARY"), true);
    assert.equal(ui.matchesDiscussion(item(), "unverified", ""), false);
    assert.equal(ui.matchesDiscussion(item(), "all", "reviewer"), true);
    for (const url of ["javascript:alert(1)", "https://github.com.evil.test/a", "https://user@github.com/a", "http://github.com/a"]) assert.equal(ui.safeDiscussionLink(url), undefined);
    assert.ok(ui.safeDiscussionLink("https://github.com/o/r/pull/1#discussion_r1"));
    assert.match(ui.discussionNotice(), /不代表没有评论/);
    assert.match(ui.discussionNotice({ ...stats, status: "unavailable" }), /无法判断/);
    assert.match(ui.discussionNotice({ ...stats, status: "partial" }), /不完整/);
  } finally { await server.close(); }
});

test("分页读取所有线程、缓存详情，切换任务取消旧请求并保留失败", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  const previous = globalThis.fetch, scope = effectScope();
  try {
    const { usePrDiscussions } = await server.ssrLoadModule("/src/composables/usePrDiscussions.ts");
    const task = ref("first"), requests = [];
    let staleResolve, fail = false;
    globalThis.fetch = async (url, options) => {
      requests.push([url, options.signal]);
      const parsed = new URL(url, "http://localhost"), id = parsed.searchParams.get("discussion_id");
      if (fail) return new Response("{}", { status: 500 });
      if (id === "late") return new Promise(resolve => { staleResolve = resolve; });
      const page = Number(parsed.searchParams.get("page") || 1);
      const rows = id ? [item(id)] : page === 1 ? Array.from({ length: 100 }, (_, i) => item(`thread:${i}`)) : [item("last-page")];
      return new Response(JSON.stringify({ stats, items: rows, total: id ? 1 : 101, page, page_size: 100 }));
    };
    const store = scope.run(() => usePrDiscussions(() => task.value));
    await store.load();
    assert.equal(store.items.value.length, 101);
    assert.equal(store.items.value.at(-1).id, "last-page");
    assert.ok(requests.some(([url]) => url.includes("page=2")));
    await store.select(item());
    const count = requests.length;
    await store.select(item());
    assert.equal(requests.length, count);
    await store.select(item(), 20);
    assert.equal(store.commentOffset.value, 20);
    assert.ok(requests.at(-1)[0].includes("comment_offset=20"));
    const pending = store.select(item("late"));
    store.clearSelection();
    staleResolve(new Response(JSON.stringify({ items: [item("late")] })));
    await pending;
    assert.equal(store.selected.value, null);
    const pendingTask = store.select(item("late"));
    task.value = "second"; await nextTick();
    staleResolve(new Response(JSON.stringify({ items: [item("late")] })));
    await pendingTask;
    assert.equal(store.items.value.length, 0);
    assert.equal(store.selected.value, null);
    assert.equal(requests.at(-1)[1].aborted, true);
    fail = true; await store.load();
    assert.match(store.error.value, /加载失败/);
    assert.equal(store.ready.value, false);
  } finally { scope.stop(); globalThis.fetch = previous; await server.close(); }
});
