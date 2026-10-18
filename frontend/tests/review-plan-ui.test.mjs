import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';

test('冻结方案显示真实限额，修改后不能直接创建', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const { default: component } = await server.ssrLoadModule('/src/components/pages/ReviewPlanPage.vue');
    const limits = { max_model_calls: 7, max_tokens: 20000, max_cost_microusd: null, timeout_seconds: 30 };
    const plan = { version: 'review-plan-v1', depth: 'quick', focus: ['security'], scope: {mode:'all', primary_files:[]}, limits };
    const html = await renderToString(createSSRApp(component, {
      preview: { preview_id:'saved', effective_plan:plan, changed_files:[], review_units:[], excluded_files:[], risk_tags:[], warnings:[], included_file_count:0, changed_file_count:0 },
      capabilities: { depth_presets:[{id:'quick',label:'快速初筛',limits}], focus_categories:['security'], scope_selection_supported:true, limits:{defaults:limits,maxima:limits} },
      prUrl:'https://github.com/o/r/pull/1', model:'fixture', models:[], defaultModel:'fixture', modelsLoading:false, modelsError:null, submitting:false, dirty:true, previewing:false,
    }));
    assert.match(html, /7 次 \/ 30 秒/);
    assert.match(html, /配置已变化，请先校验并保存方案/);
    assert.match(html, /<button[^>]*disabled[^>]*>(?:(?!<\/button>)[\s\S])*确认方案并开始审查/);
    assert.doesNotMatch(html, /预览估算仍使用服务端默认模型/);
    assert.doesNotMatch(html, /费用上限|费用留空|缺少价格|USD/);
  } finally { await server.close(); }
});

test('提交期间方案页保持完整并内联显示创建状态', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const { default: component } = await server.ssrLoadModule('/src/components/pages/ReviewPlanPage.vue');
    const limits = { max_model_calls: 7, max_tokens: 20000, max_cost_microusd: null, timeout_seconds: 30 };
    const plan = { version: 'review-plan-v1', depth: 'quick', focus: ['security'], scope: {mode:'all', primary_files:[]}, limits };
    const html = await renderToString(createSSRApp(component, {
      preview: { preview_id:'saved', effective_plan:plan, changed_files:[], review_units:[], excluded_files:[], risk_tags:[], warnings:[], included_file_count:0, changed_file_count:0 },
      capabilities: { depth_presets:[{id:'quick',label:'快速初筛',limits}], focus_categories:['security'], scope_selection_supported:true, limits:{defaults:limits,maxima:limits} },
      prUrl:'https://github.com/o/r/pull/1', model:'fixture', models:[], defaultModel:'fixture', modelsLoading:false, modelsError:null, submitting:true, dirty:false, previewing:false,
    }));
    // 方案内容不被卸载，提交状态内联在按钮附近。
    assert.match(html, /7 次 \/ 30 秒/);
    assert.match(html, /本次 PR 概况/);
    assert.match(html, /正在创建审查任务/);
    assert.match(html, /进度/);
    assert.match(html, /aria-busy="true"/);
    assert.match(html, /<button[^>]*disabled[^>]*>(?:(?!<\/button>)[\s\S])*正在创建审查任务…/);
  } finally { await server.close(); }
});
