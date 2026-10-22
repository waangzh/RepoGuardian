import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';

test('增量方案展示复用范围，并声明前序问题需重新核验', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const { default: component } = await server.ssrLoadModule('/src/components/pages/ReviewPlanPage.vue');
    const limits = { max_model_calls: 12, max_tokens: 60000, max_cost_microusd: null, timeout_seconds: 60 };
    const html = await renderToString(createSSRApp(component, {
      preview: {
        preview_id: 'saved',
        effective_plan: { version: 'review-plan-v1', depth: 'standard', focus: ['security'], scope: { mode: 'all', primary_files: [] }, limits: { ...limits } },
        changed_files: [], review_units: [], excluded_files: [], risk_tags: [], warnings: [], included_file_count: 0, changed_file_count: 0,
        incremental: { status: 'applied', delta_file_count: 2, full_changed_file_count: 5, reused_unit_count: 3, reused_file_count: 4, superseded_finding_count: 1, warnings: [] },
      },
      capabilities: { depth_presets: [{ id: 'standard', label: '标准审查', limits }], focus_categories: ['security'], scope_selection_supported: true, limits: { defaults: limits, maxima: limits } },
      prUrl: 'https://github.com/o/r/pull/1', model: '', models: [], defaultModel: 'fixture', modelsLoading: false, modelsError: null, submitting: false, dirty: false, previewing: false,
    }));
    assert.match(html, /增量审查：复用 3 个未变更 Unit（4 个文件），重新检查 2 个变化文件/);
    assert.match(html, /前序问题需在当前快照重新核验/);
  } finally { await server.close(); }
});

test('完整审查方案不显示增量提示', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const { incrementalReviewLabel } = await server.ssrLoadModule('/src/utils/reportPresentation.ts');
    assert.equal(incrementalReviewLabel(null), '');
    assert.equal(incrementalReviewLabel({ status: 'not_applicable', delta_file_count: 0,
      full_changed_file_count: 1, reused_unit_count: 0, reused_file_count: 0,
      superseded_finding_count: 0, warnings: [] }), '');
    assert.match(incrementalReviewLabel({ status: 'applied', delta_file_count: 1,
      full_changed_file_count: 3, reused_unit_count: 2, reused_file_count: 2,
      superseded_finding_count: 0, warnings: [] }), /复用 2 个未变更 Unit/);
    assert.match(incrementalReviewLabel({ version: 'incremental-review-v1',
      previous_task_id: 'a'.repeat(32), reused_units: [{ unit_id: 'u1' }], delta_file_count: 1 }),
    /复用 1 个未变更 Unit，来源 aaaaaaaa/);
    assert.equal(incrementalReviewLabel({ status: 'not_applicable',
      message: '前序问题需要重新审查。' }), '前序问题需要重新审查。');
  } finally { await server.close(); }
});
