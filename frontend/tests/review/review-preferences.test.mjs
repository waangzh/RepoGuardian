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
  pr_discussions_available: true,
};

test('默认深度偏好持久化、恢复并生成真实 Preview 方案', async () => {
  const previousStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  const previousDocument = Object.getOwnPropertyDescriptor(globalThis, 'document');
  const values = new Map([['rg-default-review-depth', 'server'], ['rg-default-pr-discussions', 'server']]);
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
    assert.equal(preferences.defaultReviewDepth.value, 'standard');
    assert.equal(preferences.defaultPrDiscussions.value, 'enabled');
    assert.throws(() => preferences.initialReviewPlan(null), /能力声明不可用/);
    assert.equal(preferences.initialReviewPlan(capabilities).include_pr_discussions, true);
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
    assert.match(html, /审查时参考 PR 历史讨论/);
    assert.match(html, /已有任务继续使用保存的选择/);
    assert.doesNotMatch(html, /跟随服务端默认|<select\b/);
    assert.match(html, /class="app-select__trigger app-select__trigger--compact"/);
    assert.match(html, /aria-label="历史讨论审查默认选择"/);
    assert.match(html, /aria-label="默认审查深度"/);
    const unavailableHtml = await renderToString(createSSRApp(Settings, {
      capabilities: { ...capabilities, pr_discussions_available: false },
    }));
    assert.match(unavailableHtml, /当前为关闭/);
    assert.match(unavailableHtml, /服务端未开放历史讨论审查，当前为关闭/);
    assert.equal(preferences.initialReviewPlan({ ...capabilities, pr_discussions_available: false }).include_pr_discussions, false);
    const detailsTag = html.match(/<details\b[^>]*class="[^"]*rg-depth-preference[^"]*"[^>]*>/)?.[0];
    assert.ok(detailsTag);
    assert.doesNotMatch(detailsTag, /\bopen(?:\s|=|>)/);
    assert.match(html, /<summary[^>]*aria-label="默认审查深度设置"/);
    assert.doesNotMatch(html, /后端尚未提供深度预设|尚未接入，不能调整/);
    const { default: Selector } = await server.ssrLoadModule('/src/components/review/ReviewDepthSelector.vue');
    const selectorHtml = await renderToString(createSSRApp(Selector, {
      modelValue: 'deep',
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
    preferences.defaultReviewDepth.value = 'standard';
    assert.equal(preferences.initialReviewPlan(capabilities).depth, 'standard');
    preferences.defaultPrDiscussions.value = 'disabled';
    await nextTick();
    assert.equal(values.get('rg-default-pr-discussions'), 'disabled');
    const historyPlan = preferences.initialReviewPlan(capabilities);
    assert.equal(historyPlan.include_pr_discussions, false);
    assert.equal(historyPlan.depth, 'standard');
    const { default: Controls } = await server.ssrLoadModule('/src/components/review/ReviewPlanControls.vue');
    const controls = await renderToString(createSSRApp(Controls, {
      preview: { effective_plan: historyPlan, changed_files: [] },
      capabilities: { ...capabilities, pr_discussions_available: true }, busy: false,
    }));
    assert.match(controls, /审查时参考 PR 历史讨论/);
    assert.match(controls, /关闭后不采集、不提供给模型/);
    const disabledControls = await renderToString(createSSRApp(Controls, {
      preview: { effective_plan: historyPlan, changed_files: [] },
      capabilities: { ...capabilities, pr_discussions_available: false }, busy: false,
    }));
    assert.match(disabledControls, /服务端未开放历史讨论审查/);
    const { preferences: historyRestored } = await loadPreferences();
    assert.equal(historyRestored.defaultPrDiscussions.value, 'disabled');
    preferences.defaultPrDiscussions.value = 'enabled';
    assert.equal(preferences.initialReviewPlan(capabilities).include_pr_discussions, true);
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
      getItem() { throw new Error('storage disabled'); }, setItem() { throw new Error('storage disabled'); },
    } });
    const { preferences: blocked } = await loadPreferences();
    assert.equal(blocked.defaultReviewDepth.value, 'standard');
    blocked.defaultReviewDepth.value = 'quick';
    blocked.defaultPrDiscussions.value = 'disabled';
    await nextTick();
    assert.equal(blocked.initialReviewPlan(capabilities).depth, 'quick');
    assert.equal(blocked.initialReviewPlan(capabilities).include_pr_discussions, false);
  } finally {
    await Promise.all(servers.map(server => server.close()));
    if (previousStorage) Object.defineProperty(globalThis, 'localStorage', previousStorage);
    else delete globalThis.localStorage;
    if (previousDocument) Object.defineProperty(globalThis, 'document', previousDocument);
    else delete globalThis.document;
  }
});
