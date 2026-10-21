import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";

test("删除接口接受空 204、编码任务 ID，并保留服务端冲突说明", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  const originalFetch = globalThis.fetch;
  try {
    const { deleteReview } = await server.ssrLoadModule("/src/api/client.ts");
    const pending = new AbortController();
    globalThis.fetch = async (path, init) => {
      assert.equal(path, "/api/reviews/id%2Fwith%20space");
      assert.equal(init.method, "DELETE");
      assert.equal(init.signal, pending.signal);
      return new Response(null, { status: 204 });
    };
    assert.equal(await deleteReview("id/with space", pending.signal), undefined);
    globalThis.fetch = async () => new Response(JSON.stringify({
      detail: { code: "review_publication_pending", message: "发布结果尚未确认，不能删除。" },
    }), { status: 409 });
    await assert.rejects(deleteReview("id"), /发布结果尚未确认，不能删除/);
    globalThis.fetch = async () => { throw new TypeError("network unavailable"); };
    await assert.rejects(deleteReview("id"), /network unavailable/);
  } finally { globalThis.fetch = originalFetch; await server.close(); }
});
