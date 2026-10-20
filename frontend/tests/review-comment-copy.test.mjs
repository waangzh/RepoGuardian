import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";
import { effectScope, ref } from "vue";

test("完整评论与单条评论共用快照、确认和请求生命周期边界", async t => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  const previousNavigator = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  const writes = [];
  let denyClipboard = false;
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { clipboard: {
    writeText: async text => { if (denyClipboard) throw new Error("denied"); writes.push(text); },
  } } });
  try {
    const { useReviewCommentCopy } = await server.ssrLoadModule("/src/composables/useReviewCommentCopy.ts");
    function setup(single = false) {
      writes.length = 0;
      const id = ref("run"), head = ref("a".repeat(40)), markdown = ref("旧预览正文");
      const key = ref(single ? "key-a" : undefined), detail = ref("compact"), exclude = ref(false);
      const requests = [];
      const load = (id, signal, options) => new Promise((resolve, reject) => requests.push({ id, signal, options, resolve, reject }));
      const scope = effectScope();
      const state = scope.run(() => useReviewCommentCopy({ reviewId: () => id.value, reviewedHeadSha: () => head.value,
        markdown: () => markdown.value, options: () => ({ detail: detail.value, exclude_resolved: exclude.value, finding_key: key.value }) }, load));
      function result(status = "current", update = {}) {
        return { review_id: "run", reviewed_head_sha: "a".repeat(40), latest_head_sha: status === "current" ? "a".repeat(40) : "d".repeat(40),
          status, detail: "compact", checked_at: "2026-10-19T00:00:00Z", review_comment_markdown: `服务端最新正文 · 快照检查：${status}`,
          included_finding_keys: [key.value || "key-a"], omitted_finding_keys: [], retained_blocking_keys: [], ...update };
      }
      return { state, requests, scope, result, id, head, markdown, key, detail, exclude };
    }

    for (const single of [false, true]) {
      await t.test(`${single ? "单条" : "完整"}评论只复制服务端检查后的正文`, async () => {
        const c = setup(single);
        try {
          const copy = c.state.copyCheckedComment();
          assert.equal(writes.length, 0);
          assert.equal(c.requests[0].options.finding_key, single ? "key-a" : undefined);
          c.requests[0].resolve(c.result()); await copy;
          assert.deepEqual(writes, [c.result().review_comment_markdown]);
          assert.equal(c.state.copied.value, true);
        } finally { c.scope.stop(); }
      });
      for (const status of ["stale", "unknown"]) {
        await t.test(`${single ? "单条" : "完整"}评论 ${status} 必须显式确认`, async () => {
          const c = setup(single);
          try {
            const copy = c.state.copyCheckedComment(); c.requests[0].resolve(c.result(status)); await copy;
            assert.equal(writes.length, 0);
            assert.equal(c.state.pendingCopy.value.status, status);
            await c.state.copyAcknowledged(); await c.state.copyAcknowledged();
            assert.deepEqual(writes, [c.result(status).review_comment_markdown]);
            assert.equal(c.state.copied.value, true);
            assert.equal(c.state.headCheck.value.status, status, "复制成功不改变快照状态");
            assert.equal(c.state.pendingCopy.value, null);
          } finally { c.scope.stop(); }
        });
      }
    }

    for (const update of [{ review_id: "other" }, { reviewed_head_sha: "b".repeat(40) }, { latest_head_sha: "b".repeat(40) },
      { detail: "full" }, { included_finding_keys: ["key-b"] }, { review_comment_markdown: "" }, { status: "future" }]) {
      await t.test(`拒绝错误身份或响应 ${Object.keys(update)[0]}`, async () => {
        const c = setup(true);
        try {
          const copy = c.state.copyCheckedComment(); c.requests[0].resolve(c.result("current", update)); await copy;
          assert.equal(writes.length, 0); assert.equal(c.state.copied.value, false);
          assert.match(c.state.feedback.value, /尚未复制/);
        } finally { c.scope.stop(); }
      });
    }

    for (const field of ["id", "head", "markdown", "key", "detail", "exclude", "unmount"]) {
      await t.test(`${field} 变化使旧检查与确认失效`, async () => {
        const c = setup(true);
        try {
          const copy = c.state.copyCheckedComment();
          if (field === "unmount") c.scope.stop();
          else c[field].value = field === "exclude" ? true : "changed";
          assert.equal(c.requests[0].signal.aborted, true);
          c.requests[0].resolve(c.result()); await copy;
          assert.equal(writes.length, 0); assert.equal(c.state.pendingCopy.value, null);
          assert.equal(c.state.checking.value, false);
        } finally { c.scope.stop(); }
      });
    }
    await t.test("改变选择清除待确认评论，旧 finally 不清除新请求", async () => {
      const c = setup(true);
      try {
        const first = c.state.copyCheckedComment();
        c.key.value = "key-b";
        const second = c.state.copyCheckedComment();
        c.requests[0].resolve(c.result()); await first;
        assert.equal(c.state.checking.value, true);
        c.requests[1].resolve(c.result("stale")); await second;
        c.key.value = "key-c";
        await c.state.copyAcknowledged();
        assert.equal(writes.length, 0); assert.equal(c.state.pendingCopy.value, null);
      } finally { c.scope.stop(); }
    });
    await t.test("单独校验只更新 Head 状态，交付时仍重新检查", async () => {
      const c = setup();
      try {
        const check = c.state.checkHead(); c.requests[0].resolve(c.result('stale')); await check;
        assert.equal(c.state.headCheck.value.status, 'stale');
        assert.equal(c.state.pendingCopy.value, null); assert.equal(writes.length, 0);
        const copy = c.state.copyCheckedComment();
        assert.equal(c.requests.length, 2);
        c.requests[1].resolve(c.result()); await copy;
        assert.equal(writes.length, 1);
      } finally { c.scope.stop(); }
    });
    await t.test("过期导出确认保留原操作，取消和选项变化使确认失效", async () => {
      const c = setup(); const downloads = [];
      try {
        const first = c.state.checkedComment(text => downloads.push(text));
        c.requests[0].resolve(c.result('stale')); await first;
        assert.deepEqual(downloads, []);
        await c.state.copyAcknowledged();
        assert.deepEqual(downloads, [c.result('stale').review_comment_markdown]);
        assert.deepEqual(writes, []);
        const second = c.state.checkedComment(text => downloads.push(text));
        c.requests[1].resolve(c.result('unknown')); await second;
        c.state.cancelPending(); await c.state.copyAcknowledged();
        assert.equal(downloads.length, 1);
        const third = c.state.checkedComment(text => downloads.push(text));
        c.requests[2].resolve(c.result('stale')); await third;
        c.exclude.value = true; await c.state.copyAcknowledged();
        assert.equal(downloads.length, 1); assert.equal(c.state.headCheck.value, null);
      } finally { c.scope.stop(); }
    });
    await t.test("网络失败和剪贴板拒绝不能显示复制成功", async () => {
      const c = setup(true);
      try {
        const failed = c.state.copyCheckedComment(); c.requests[0].reject(new Error("409 or offline")); await failed;
        assert.equal(c.state.copied.value, false); assert.equal(writes.length, 0);
        denyClipboard = true;
        const denied = c.state.copyCheckedComment(); c.requests[1].resolve(c.result()); await denied;
        assert.equal(c.state.copied.value, false); assert.match(c.state.feedback.value, /复制失败/);
        assert.equal(writes.length, 0);
      } finally { denyClipboard = false; c.scope.stop(); }
    });
    await t.test("未知快照重试保留原导出动作，取消后不能重放", async () => {
      const c = setup(); const downloads = [];
      try {
        const first = c.state.checkedComment(text => downloads.push(text));
        c.requests[0].resolve(c.result("unknown")); await first;
        const retry = c.state.retryPending();
        c.requests[1].resolve(c.result("current")); await retry;
        assert.deepEqual(downloads, [c.result("current").review_comment_markdown]);
        assert.deepEqual(writes, []);
        const next = c.state.copyCheckedComment();
        c.requests[2].resolve(c.result("unknown")); await next;
        c.state.cancelPending(); await c.state.retryPending();
        assert.equal(c.requests.length, 3);
        assert.deepEqual(writes, []);
      } finally { c.scope.stop(); }
    });
  } finally {
    if (previousNavigator) Object.defineProperty(globalThis, "navigator", previousNavigator);
    else delete globalThis.navigator;
    await server.close();
  }
});
