import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';

const request = { pr_url: 'https://github.com/o/r/pull/1', mode: 'review', generate_patches: false, validation_backend: 'none' };
const id = 'a'.repeat(32);
const snapshot = (status, identity = id) => ({ id: identity, request, status, stages: [], cache_hit: false,
  created_at: new Date(Date.now() - 70000).toISOString(), updated_at: new Date().toISOString(), completed_at: null,
  error: null, result: status === 'completed' ? { preview_id: 'plan' } : null, consumed_task_id: null });
const defer = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

test('预览刷新恢复、网络失败与过期结果均保留真实状态', async () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  const values = new Map([['rg-preview-job', id]]);
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
    getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key),
  } });
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true, entries: [], include: [] }, appType: 'custom' });
  try {
    const { usePreviewJob } = await server.ssrLoadModule('/src/composables/usePreviewJob.ts');
    let calls = 0;
    const api = { create: async identity => snapshot('queued', identity), get: async identity => {
      calls++;
      if (calls === 1) throw Error('offline');
      return snapshot('completed', identity);
    }, cancel: async identity => snapshot('cancelled', identity) };
    const tracker = usePreviewJob(api, 1);
    await assert.rejects(tracker.resume(), /offline/);
    assert.equal(values.get('rg-preview-job'), id);
    assert.equal(tracker.tracking.value, false);
    assert.equal(tracker.connectionError.value, 'offline');
    const restored = [];
    assert.equal((await tracker.resume(async value => { restored.push(value.request.pr_url); })).status, 'completed');
    assert.deepEqual(restored, [request.pr_url]);
    assert.equal(tracker.connectionError.value, '');
    tracker.detach();
    const afterRefresh = usePreviewJob(api, 1);
    assert.equal((await afterRefresh.resume()).result.preview_id, 'plan');
    api.get = async () => ({ ...snapshot('expired'), error: '方案已过期' });
    assert.equal((await afterRefresh.resume()).status, 'expired');
    assert.equal(afterRefresh.job.value.result, null);
    afterRefresh.detach(true);
    assert.equal(values.has('rg-preview-job'), false);
  } finally {
    await server.close();
    if (descriptor) Object.defineProperty(globalThis, 'localStorage', descriptor); else delete globalThis.localStorage;
  }
});

test('旧请求响应与 finally 不覆盖新请求，存储不可用仍可跟踪和取消', async () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
    getItem() { throw Error('blocked'); }, setItem() { throw Error('blocked'); }, removeItem() { throw Error('blocked'); },
  } });
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true, entries: [], include: [] }, appType: 'custom' });
  try {
    const { usePreviewJob } = await server.ssrLoadModule('/src/composables/usePreviewJob.ts');
    const old = defer(), next = defer();
    const identities = [];
    const api = { create: async identity => { identities.push(identity); return identities.length === 1 ? old.promise : next.promise; },
      get: async identity => snapshot('completed', identity), cancel: async identity => snapshot('cancelled', identity) };
    const tracker = usePreviewJob(api, 1);
    const first = tracker.start(request);
    tracker.detach();
    const second = tracker.start({ ...request, pr_url: 'https://github.com/o/r/pull/2' });
    old.resolve(snapshot('completed', identities[0]));
    assert.equal(await first, null);
    assert.equal(tracker.tracking.value, true);
    assert.equal(tracker.job.value, null);
    next.resolve(snapshot('completed', identities[1]));
    assert.equal((await second).id, identities[1]);
    assert.equal(tracker.tracking.value, false);
    const pending = defer();
    api.create = async identity => snapshot('running', identity);
    api.get = () => pending.promise;
    const running = tracker.start(request);
    await new Promise(resolve => setTimeout(resolve, 5));
    await tracker.cancel();
    pending.resolve(snapshot('completed'));
    assert.equal(await running, null);
    assert.equal(tracker.job.value.status, 'cancelled');
    assert.equal(tracker.tracking.value, false);
  } finally {
    await server.close();
    if (descriptor) Object.defineProperty(globalThis, 'localStorage', descriptor); else delete globalThis.localStorage;
  }
});

test('刷新恢复首帧使用本地预览标识，避免闪回新建页', async () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
    getItem: key => key === 'rg-preview-job' ? id : null, setItem() {}, removeItem() {},
  } });
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true, entries: [], include: [] }, appType: 'custom' });
  try {
    const { hasSavedPreview } = await server.ssrLoadModule('/src/composables/usePreviewJob.ts');
    assert.equal(hasSavedPreview(), true);
  } finally {
    await server.close();
    if (descriptor) Object.defineProperty(globalThis, 'localStorage', descriptor); else delete globalThis.localStorage;
  }
});
