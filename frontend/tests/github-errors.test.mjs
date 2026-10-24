import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';

test('默认重审不复用同快照结果，只有显式来源才传递复用标识', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] }, appType: 'custom' });
  const original = globalThis.fetch;
  try {
    const { previewReview, createPreviewJob } = await server.ssrLoadModule('/src/api/client.ts');
    const calls = [];
    globalThis.fetch = async (_url, init) => {
      calls.push(JSON.parse(init.body));
      return new Response('{}', { status: 200 });
    };
    await previewReview('https://github.com/o/r/pull/1');
    assert.equal(Object.hasOwn(calls[0], 'reuse_review_id'), false);
    const source = 'a'.repeat(32);
    await previewReview('https://github.com/o/r/pull/1', 'review', 'none', undefined, undefined, undefined, source);
    assert.equal(calls[1].reuse_review_id, source);
    await createPreviewJob('b'.repeat(32), {
      pr_url: 'https://github.com/o/r/pull/1', mode: 'review', validation_backend: 'none', reuse_review_id: source,
    });
    assert.equal(calls[2].request.reuse_review_id, source);
  } finally { globalThis.fetch = original; await server.close(); }
});

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
    await assert.rejects(createReview('https://github.com/o/r/pull/1', 'fixture', 'review', 'none', preview), /检查网络或代理后重试/);
    const result = await createReview('https://github.com/o/r/pull/1', 'fixture', 'review', 'none', preview);
    assert.equal(result.task_id, 'one-task');
    assert.deepEqual(calls[0], calls[1]);
    assert.equal(calls[1].preview_id, 'same-plan');
  } finally { globalThis.fetch = original; await server.close(); }
});

test('方案输入或服务端配置变化时明确要求重新预览并保留失效标识', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] }, appType: 'custom' });
  const original = globalThis.fetch;
  try {
    const { createReview } = await server.ssrLoadModule('/src/api/client.ts');
    for (const [code, reason] of [['review_plan_repreview_required', '旧预览'],
      ['review_plan_configuration_changed', '服务端配置'], ['review_plan_stale', '规划输入']]) {
      globalThis.fetch = async () => new Response(JSON.stringify({detail:{code}}), {status:409});
      await assert.rejects(createReview('https://github.com/o/r/pull/1'), error =>
        error.message.includes('方案已失效') && error.message.includes(reason) && error.message.includes('重新预览'));
    }
  } finally { globalThis.fetch = original; await server.close(); }
});
