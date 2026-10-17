import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { createSSRApp, nextTick } from 'vue';
import { renderToString } from '@vue/server-renderer';

const limits = { max_model_calls: 12, max_tokens: 60000, max_cost_microusd: null, timeout_seconds: 60 };
const capabilities = {
  plan_version: 'review-plan-v1',
  depth_presets: [['quick', '快速初筛'], ['standard', '标准审查'], ['deep', '深度核验']].map(([id, label]) => ({ id, label, limits })),
  focus_categories: ['correctness', 'security', 'performance', 'compatibility'],
  scope_selection_supported: true, limits: { defaults: limits, maxima: limits },
};

test('默认深度偏好持久化、恢复并生成真实 Preview 方案', async () => {
  const previousStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  const previousDocument = Object.getOwnPropertyDescriptor(globalThis, 'document');
  const values = new Map([['rg-default-review-depth', 'invalid-depth']]);
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
    getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value),
  } });
  Object.defineProperty(globalThis, 'document', { configurable: true, value: { documentElement: { dataset: {} } } });
  const servers = [];
  async function loadPreferences() {
    const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
    servers.push(server);
    return { server, preferences: (await server.ssrLoadModule('/src/composables/usePreferences.ts')).usePreferences() };
  }
  try {
    const { server, preferences } = await loadPreferences();
    assert.equal(preferences.defaultReviewDepth.value, 'server');
    assert.equal(preferences.initialReviewPlan(null), undefined);
    for (const depth of ['quick', 'standard', 'deep']) {
      preferences.defaultReviewDepth.value = depth;
      await nextTick();
      assert.equal(values.get('rg-default-review-depth'), depth);
      const plan = preferences.initialReviewPlan(capabilities);
      assert.equal(plan.depth, depth);
      assert.deepEqual(plan.scope, { mode: 'all', primary_files: [] });
      assert.deepEqual(plan.limits, { max_model_calls: null, max_tokens: null, max_cost_microusd: null, timeout_seconds: null });
      assert.notEqual(plan.focus, capabilities.focus_categories);
    }
    assert.throws(() => preferences.initialReviewPlan(null), /能力声明不可用/);
    assert.throws(() => preferences.initialReviewPlan({ ...capabilities, depth_presets: [] }), /能力声明不可用/);
    const { preferences: restored } = await loadPreferences();
    assert.equal(restored.defaultReviewDepth.value, 'deep');
    const { default: Settings } = await server.ssrLoadModule('/src/components/pages/SettingsPage.vue');
    const html = await renderToString(createSSRApp(Settings, { capabilities }));
    assert.match(html, /深度核验/);
    assert.match(html, /仅用于新建方案/);
    const detailsTag = html.match(/<details\b[^>]*class="[^"]*rg-depth-preference[^"]*"[^>]*>/)?.[0];
    assert.ok(detailsTag);
    assert.doesNotMatch(detailsTag, /\bopen(?:\s|=|>)/);
    assert.match(html, /<summary[^>]*aria-label="默认审查深度：深度核验"/);
    assert.doesNotMatch(html, /后端尚未提供深度预设|尚未接入，不能调整/);
    const { default: Selector } = await server.ssrLoadModule('/src/components/review/ReviewDepthSelector.vue');
    const selectorHtml = await renderToString(createSSRApp(Selector, {
      modelValue: 'deep', allowServerDefault: true,
      presets: capabilities.depth_presets.map(preset => ({ ...preset,
        limits: { max_model_calls: 0, max_tokens: 60000, max_cost_microusd: 125000, timeout_seconds: 60 } })),
    }));
    assert.match(selectorHtml, /聚焦直接变更/);
    assert.match(selectorHtml, /调用关系与关联契约/);
    assert.match(selectorHtml, /主动寻找反证/);
    assert.match(selectorHtml, /0 次/);
    assert.match(selectorHtml, /60,000/);
    assert.doesNotMatch(selectorHtml, /费用|USD/);
    assert.match(selectorHtml, /60 秒/);
    assert.match(selectorHtml, /上限不代表预计消耗/);
    assert.match(selectorHtml, /aria-label="深度核验" aria-pressed="true"/);
    const reviewHtml = await renderToString(createSSRApp(Selector, {
      modelValue: 'quick', presets: capabilities.depth_presets, disabled: true,
    }));
    assert.doesNotMatch(reviewHtml, /跟随服务端默认/);
    assert.doesNotMatch(reviewHtml, /费用|USD/);
    assert.equal((reviewHtml.match(/ disabled/g) || []).length, 3);
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
      getItem() { throw new Error('storage disabled'); }, setItem() { throw new Error('storage disabled'); },
    } });
    const { preferences: blocked } = await loadPreferences();
    assert.equal(blocked.defaultReviewDepth.value, 'server');
    blocked.defaultReviewDepth.value = 'quick';
    await nextTick();
    assert.equal(blocked.initialReviewPlan(capabilities).depth, 'quick');
    preferences.defaultReviewDepth.value = 'server';
    assert.equal(preferences.initialReviewPlan(capabilities), undefined);
  } finally {
    await Promise.all(servers.map(server => server.close()));
    if (previousStorage) Object.defineProperty(globalThis, 'localStorage', previousStorage);
    else delete globalThis.localStorage;
    if (previousDocument) Object.defineProperty(globalThis, 'document', previousDocument);
    else delete globalThis.document;
  }
});
