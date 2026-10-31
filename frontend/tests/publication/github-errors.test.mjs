import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';

const input = {
  pr_url: 'https://github.com/o/r/pull/1',
  depth: 'standard',
  paths: ['src/a.py'],
  focus: ['correctness'],
  limits: { model_calls: 4 },
  model: 'fixture',
  include_pr_discussions: false,
};

async function withClient(run) {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    appType: 'custom',
  });
  try {
    return await run(
      await server.ssrLoadModule('/src/api/client.ts'),
      await server.ssrLoadModule('/src/utils/nativeReview.ts'),
    );
  }
  finally { await server.close(); }
}

for (const method of ['createReview', 'previewReview']) {
  test(method + ' 省略未填写的预算并保留显式设置', async () => {
    const original = globalThis.fetch;
    const bodies = [];
    globalThis.fetch = async (_url, init) => {
      bodies.push(JSON.parse(init.body));
      return new Response(JSON.stringify({ id: 'review' }), { status: 200 });
    };
    try {
      await withClient(async (client, { defaultPlan }) => {
        const options = defaultPlan();
        await client[method]({ ...options, pr_url: input.pr_url });
        options.limits.model_calls = 4;
        options.limits.total_input_tokens = 12000;
        await client[method]({ ...options, pr_url: input.pr_url });
        options.limits.model_calls = null;
        await client[method]({ ...options, pr_url: input.pr_url });
        assert.equal(options.limits.tool_calls, null);
      });
      assert.deepEqual(bodies.map(body => body.limits), [
        {},
        { model_calls: 4, total_input_tokens: 12000 },
        { total_input_tokens: 12000 },
      ]);
      assert.equal(bodies[0].include_pr_discussions, false);
    } finally { globalThis.fetch = original; }
  });
}

test('参数校验失败显示字段及原因，不显示输入内容', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({
    detail: [{
      type: 'int_type',
      loc: ['body', 'limits', 'model_calls'],
      msg: 'Input should be a valid integer',
      input: 'private-input',
    }],
  }), { status: 422 });
  try {
    await withClient(async ({ createReview }) => {
      await assert.rejects(createReview(input, undefined, 'invalid-limit'), {
        message: 'limits.model_calls: Input should be a valid integer',
      });
    });
  } finally { globalThis.fetch = original; }
});

test('创建审查仅在网络失败时重试并复用幂等键', async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (_url, init) => {
    calls.push(init);
    if (calls.length === 1) throw Error('offline');
    return new Response(JSON.stringify({ id: 'review' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };
  try {
    await withClient(async ({ createReview }) => {
      const result = await createReview(input, undefined, 'stable-key');
      assert.equal(result.id, 'review');
    });
    assert.equal(calls.length, 2);
    assert.equal(calls[0].headers['Idempotency-Key'], 'stable-key');
    assert.equal(calls[1].headers['Idempotency-Key'], 'stable-key');
    assert.deepEqual(JSON.parse(calls[0].body), input);
  } finally { globalThis.fetch = original; }
});

for (const status of [409, 422]) {
  test('创建审查不重试 HTTP ' + status, async () => {
    const original = globalThis.fetch;
    let calls = 0;
    globalThis.fetch = async () => {
      calls += 1;
      return new Response(JSON.stringify({ detail: 'invalid request' }), {
        status,
        headers: { 'Content-Type': 'application/json' },
      });
    };
    try {
      await withClient(async ({ createReview, ApiError }) => {
        await assert.rejects(createReview(input, undefined, 'stable-key'), ApiError);
      });
      assert.equal(calls, 1);
    } finally { globalThis.fetch = original; }
  });
}

test('创建审查重试可重试 HTTP 并复用幂等键', async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (_url, init) => {
    calls.push(init);
    if (calls.length === 1) {
      return new Response(JSON.stringify({ detail: 'busy' }), {
        status: 503,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({ id: 'review' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };
  try {
    await withClient(async ({ createReview }) => {
      await createReview(input, undefined, 'stable-key');
    });
    assert.equal(calls.length, 2);
    assert.equal(calls[0].headers['Idempotency-Key'], calls[1].headers['Idempotency-Key']);
  } finally { globalThis.fetch = original; }
});
