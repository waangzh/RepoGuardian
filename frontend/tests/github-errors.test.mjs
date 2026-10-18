import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';

test('确认方案网络错误显示可理解的原因，并保持原方案供再次确认', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] }, appType: 'custom' });
  const original = globalThis.fetch;
  try {
    const { createReview } = await server.ssrLoadModule('/src/api/client.ts');
    const preview = { preview_id: 'same-plan', plan_hash: 'saved-hash', snapshot: { head_sha: 'head', effective_base_sha: 'base' } };
    const calls = [];
    globalThis.fetch = async (url, init) => {
      calls.push(JSON.parse(init.body));
      return calls.length === 1 ? new Response(JSON.stringify({ detail: { code:'github_connection_failed',
        message:'连接 GitHub 失败，请检查网络或代理后重试。', retryable:true } }), { status:503 })
        : new Response(JSON.stringify({ task_id:'one-task', status:'queued' }), { status:202 });
    };
    await assert.rejects(createReview('https://github.com/o/r/pull/1', 'fixture', 'review', false, 'none', preview), /检查网络或代理后重试/);
    const result = await createReview('https://github.com/o/r/pull/1', 'fixture', 'review', false, 'none', preview);
    assert.equal(result.task_id, 'one-task');
    assert.deepEqual(calls[0], calls[1]);
    assert.equal(calls[1].preview_id, 'same-plan');
  } finally { globalThis.fetch = original; await server.close(); }
});
