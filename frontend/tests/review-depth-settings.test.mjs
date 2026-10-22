import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { createSSRApp, nextTick } from 'vue';
import { renderToString } from '@vue/server-renderer';

const storageKey = 'rg-review-depth-limits-v1';
const maxima = { max_model_calls: 200, max_tokens: 1000000, max_cost_microusd: 1000, timeout_seconds: 1800 };
const capabilities = {
  plan_version: 'review-plan-v1',
  depth_presets: [
    { id: 'quick', label: '快速初筛', limits: { max_model_calls: 50, max_tokens: 250000, max_cost_microusd: 250, timeout_seconds: 450 } },
    { id: 'standard', label: '标准审查', limits: { max_model_calls: 120, max_tokens: 600000, max_cost_microusd: 600, timeout_seconds: 1080 } },
    { id: 'deep', label: '深度核验', limits: { ...maxima } },
  ],
  focus_categories: ['correctness', 'security'],
  scope_selection_supported: true,
  limits: { defaults: maxima, maxima },
  pr_discussions_available: true,
};

test('三档资源偏好独立保存、恢复与校验，并接入真实方案和设置卡片', async () => {
  const previousStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  const values = new Map();
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  } });
  const servers = [];
  async function load() {
    const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
    servers.push(server);
    return { server, preferences: (await server.ssrLoadModule('/src/composables/usePreferences.ts')).usePreferences() };
  }
  try {
    const { server, preferences: prefs } = await load();
    const originalCapabilities = JSON.stringify(capabilities);
    const oldPlan = prefs.initialReviewPlan(capabilities);
    const originalPlan = JSON.stringify(oldPlan);
    prefs.saveDepthLimits('quick', { max_model_calls: 0, max_tokens: 0, timeout_seconds: 1 }, capabilities);
    prefs.saveDepthLimits('standard', { max_model_calls: 81 }, capabilities);
    prefs.saveDepthLimits('deep', { max_tokens: 900000, timeout_seconds: 1700 }, capabilities);
    assert.deepEqual(prefs.depthLimitOverrides('quick'), { max_model_calls: 0, max_tokens: 0, timeout_seconds: 1 });
    assert.deepEqual(prefs.depthLimitOverrides('standard'), { max_model_calls: 81 });
    const stored = JSON.parse(values.get(storageKey));
    assert.equal(stored.version, 1);
    assert.equal(stored.depths.quick.max_model_calls, 0);
    for (const depth of ['quick', 'standard', 'deep']) {
      prefs.defaultReviewDepth.value = depth;
      const plan = prefs.initialReviewPlan(capabilities);
      assert.equal(plan.depth, depth);
      for (const [key, value] of Object.entries(prefs.depthLimitOverrides(depth))) {
        assert.equal(plan.limits[key], value);
      }
      assert.equal(plan.limits.max_cost_microusd, null);
    }
    assert.equal(prefs.initialReviewPlan(capabilities).limits.max_model_calls, null);
    assert.equal(prefs.reviewDepthLimits('quick', capabilities).max_cost_microusd, 250);
    assert.equal(prefs.reviewDepthLimits('standard', capabilities).max_tokens, 600000);
    assert.equal(JSON.stringify(capabilities), originalCapabilities);
    assert.equal(JSON.stringify(oldPlan), originalPlan);
    const copy = prefs.depthLimitOverrides('quick');
    copy.max_model_calls = 99;
    assert.equal(prefs.depthLimitOverrides('quick').max_model_calls, 0);
    await nextTick();
    const { preferences: restored } = await load();
    assert.equal(restored.defaultReviewDepth.value, 'deep');
    assert.deepEqual(restored.depthLimitOverrides('quick'), prefs.depthLimitOverrides('quick'));
    assert.equal(restored.initialReviewPlan(capabilities).limits.max_tokens, 900000);

    for (const limits of [
      { max_model_calls: -1 }, { max_model_calls: 1.5 }, { max_model_calls: 201 },
      { max_tokens: 1000001 }, { timeout_seconds: 0 }, { timeout_seconds: 1801 },
      { max_tokens: NaN }, { max_tokens: Infinity }, { max_tokens: Number.MAX_SAFE_INTEGER + 1 },
      { max_model_calls: true }, { timeout_seconds: '60' }, { timeout_seconds: null },
    ]) {
      const before = values.get(storageKey);
      assert.throws(() => prefs.saveDepthLimits('quick', limits, capabilities), /整数|上限/);
      assert.equal(values.get(storageKey), before);
    }
    assert.throws(() => prefs.saveDepthLimits('quick', {}, { ...capabilities, depth_presets: [] }), /能力声明不可用/);
    const reduced = { ...capabilities, limits: { ...capabilities.limits, maxima: { ...maxima, max_tokens: 800000 } } };
    assert.throws(() => prefs.reviewDepthLimits('deep', reduced), /当前服务端上限 800,000/);
    assert.throws(() => prefs.initialReviewPlan(reduced), /当前服务端上限/);
    const unlimited = { ...capabilities, limits: { ...capabilities.limits, maxima: { ...maxima, max_tokens: null } } };
    prefs.saveDepthLimits('standard', { max_tokens: 2000000 }, unlimited);
    assert.equal(prefs.reviewDepthLimits('standard', unlimited).max_tokens, 2000000);
    prefs.resetDepthLimits('standard');

    const { default: Settings } = await server.ssrLoadModule('/src/components/pages/SettingsPage.vue');
    const html = await renderToString(createSSRApp(Settings, { capabilities }));
    assert.equal((html.match(/type="number"/g) || []).length, 9);
    assert.equal((html.match(/保存此档/g) || []).length, 4);
    assert.equal((html.match(/恢复预设/g) || []).length, 3);
    const quickCallsInput = html.match(/<input\b[^>]*aria-label="快速初筛模型调用上限"[^>]*>/)?.[0];
    assert.ok(quickCallsInput);
    assert.match(quickCallsInput, /\bvalue="0"/);
    assert.match(html, /aria-label="快速初筛运行时间上限"/);
    assert.match(html, /预设 250,000 · 最高 1,000,000/);
    assert.match(html, /留空使用对应服务端预设/);
    for (const button of html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)) {
      assert.doesNotMatch(button[0], /<input\b|<form\b|<button\b[^>]*>[\s\S]*<button\b/);
    }
    const { default: Editor } = await server.ssrLoadModule('/src/components/review/ReviewDepthLimitEditor.vue');
    const policyHtml = await renderToString(createSSRApp(Editor, { depth: 'deep', capabilities: reduced }));
    assert.match(policyHtml, /role="alert"[^>]*>Token不能超过当前服务端上限 800,000/);
    const { default: Controls } = await server.ssrLoadModule('/src/components/review/ReviewPlanControls.vue');
    const controls = await renderToString(createSSRApp(Controls, {
      preview: { effective_plan: oldPlan, changed_files: [] }, capabilities, busy: false,
    }));
    assert.match(controls, /自定义资源上限/);
    assert.match(controls, /0 次/);
    assert.match(controls, /placeholder="服务端默认"/);
    assert.equal(JSON.stringify(oldPlan), originalPlan);

    prefs.resetDepthLimits('quick');
    assert.deepEqual(prefs.depthLimitOverrides('quick'), {});
    assert.equal(prefs.reviewDepthLimits('quick', capabilities).max_model_calls, 50);
    assert.equal(prefs.depthLimitOverrides('deep').timeout_seconds, 1700);
    prefs.saveDepthLimits('deep', {}, capabilities);
    assert.equal(prefs.reviewDepthPresets(capabilities).find(item => item.id === 'deep').customized, false);
    assert.deepEqual(prefs.initialReviewPlan(capabilities).limits, {
      max_model_calls: null, max_tokens: null, max_cost_microusd: null, timeout_seconds: null,
    });
    for (const corrupt of ['{broken', 'null', '{"version":2,"depths":{"quick":{"max_model_calls":1}}}']) {
      values.set(storageKey, corrupt);
      const { preferences } = await load();
      assert.deepEqual(preferences.depthLimitOverrides('quick'), {});
    }
    values.set(storageKey, JSON.stringify({ version: 1, depths: {
      quick: { max_model_calls: false, max_tokens: -1, timeout_seconds: '60', max_cost_microusd: 0 },
      deep: { max_model_calls: 0, max_tokens: 1.5, timeout_seconds: 60 },
    } }));
    const { preferences: sanitized } = await load();
    assert.deepEqual(sanitized.depthLimitOverrides('quick'), {});
    assert.deepEqual(sanitized.depthLimitOverrides('deep'), { max_model_calls: 0, timeout_seconds: 60 });
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
      getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); },
    } });
    const { preferences: blocked } = await load();
    blocked.saveDepthLimits('standard', { max_model_calls: 2 }, capabilities);
    assert.equal(blocked.depthLimitsPersistent.value, false);
    assert.equal(blocked.initialReviewPlan(capabilities).limits.max_model_calls, 2);
    blocked.resetDepthLimits('standard');
    assert.equal(blocked.initialReviewPlan(capabilities).limits.max_model_calls, null);
  } finally {
    await Promise.all(servers.map(server => server.close()));
    if (previousStorage) Object.defineProperty(globalThis, 'localStorage', previousStorage);
    else delete globalThis.localStorage;
  }
});
