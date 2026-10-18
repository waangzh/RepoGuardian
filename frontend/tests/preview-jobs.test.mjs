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
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
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
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
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

test('进度呈现真实阶段、百分比、耗时与缓存提示', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const { default: Progress } = await server.ssrLoadModule('/src/components/review/PreviewProgress.vue');
    const job = { ...snapshot('running'), cache_hit: true, stages: [{ phase: 'clone', message: '正在克隆仓库',
      started_at: new Date(Date.now() - 12000).toISOString(), finished_at: null, percent: 23, operation: 'receiving_objects' }] };
    const html = await renderToString(createSSRApp(Progress, { job, tracking: true, connectionError: '', prUrl: request.pr_url }));
    assert.match(html, /正在克隆仓库/);
    assert.match(html, /23%/);
    assert.match(html, /总耗时/);
    assert.match(html, /刷新后会恢复/);
    assert.match(html, /已复用相同提交/);
    assert.match(html, /role="progressbar"/);
    assert.match(html, /aria-valuenow="23"/);
    assert.match(html, /timeline-row/);
    assert.doesNotMatch(html, /undefined|NaN/);
  } finally { await server.close(); }
});

test('预览页把失败与连接中断留在步骤 1 并提供重试出口', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const { default: Page } = await server.ssrLoadModule('/src/components/pages/PreviewPage.vue');
    const failed = { ...snapshot('failed'), error: '仓库准备失败，请检查网络连接。',
      stages: [{ phase: 'clone', message: '正在克隆仓库', started_at: new Date(Date.now() - 9000).toISOString(), finished_at: null, percent: 40, operation: 'receiving_objects' }] };
    const html = await renderToString(createSSRApp(Page, { job: failed, tracking: false, connectionError: '',
      prUrl: request.pr_url, model: '', defaultModel: 'fixture' }));
    assert.match(html, /STEP 01 \/ IMPORT PULL REQUEST/);
    assert.match(html, /PR 分析未完成/);
    assert.match(html, /仓库准备失败/);
    assert.match(html, /重新分析 PR/);
    assert.match(html, /返回修改 PR 链接/);
    assert.match(html, /预览失败/);
    assert.doesNotMatch(html, /undefined|NaN/);
    const offline = await renderToString(createSSRApp(Page, { job: null, tracking: false, connectionError: 'offline',
      prUrl: request.pr_url, model: '', defaultModel: 'fixture' }));
    assert.match(offline, /无法读取预览状态/);
    assert.match(offline, /继续查询预览/);
    assert.match(offline, /连接中断/);
  } finally { await server.close(); }
});

test('刷新恢复首帧使用本地预览标识，避免闪回新建页', async () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
    getItem: key => key === 'rg-preview-job' ? id : null, setItem() {}, removeItem() {},
  } });
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const { hasSavedPreview } = await server.ssrLoadModule('/src/composables/usePreviewJob.ts');
    assert.equal(hasSavedPreview(), true);
  } finally {
    await server.close();
    if (descriptor) Object.defineProperty(globalThis, 'localStorage', descriptor); else delete globalThis.localStorage;
  }
});
