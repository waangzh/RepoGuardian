import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';

test('高级设置默认折叠并保留冻结限额，修改后不能直接创建', async () => {
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
    const advanced = html.match(/<details\b[^>]*class="[^"]*rg-plan-advanced[^"]*"[^>]*>/)?.[0];
    assert.ok(advanced);
    assert.doesNotMatch(advanced, /\bopen(?:\s|=|>)/);
    assert.match(html, /value="7"[^>]*aria-label="最大模型调用次数"/);
    assert.match(html, /value="30"[^>]*aria-label="最大运行时间"/);
    assert.match(html, /快速初筛 · 只读/);
    assert.match(html, /全部可审查变更/);
    assert.match(html, /aria-label="执行模型"/);
    assert.match(html, /配置已变化，请先更新方案/);
    assert.match(html, /<button[^>]*disabled[^>]*>(?:(?!<\/button>)[\s\S])*开始 AI 审查/);
    assert.doesNotMatch(html, /预览估算仍使用服务端默认模型/);
    assert.doesNotMatch(html, /费用上限|费用留空|缺少价格|USD/);
  } finally { await server.close(); }
});

test('重新校验方案时就地展示服务端真实阶段，不跳转页面', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const { default: component } = await server.ssrLoadModule('/src/components/pages/ReviewPlanPage.vue');
    const limits = { max_model_calls: 12, max_tokens: 60000, max_cost_microusd: null, timeout_seconds: 60 };
    const html = await renderToString(createSSRApp(component, {
      preview: { preview_id:'saved', effective_plan:{ version:'review-plan-v1', depth:'standard', focus:['security'], scope:{mode:'all',primary_files:[]}, limits }, changed_files:[], review_units:[], excluded_files:[], risk_tags:[], warnings:[], included_file_count:0, changed_file_count:0 },
      capabilities: { depth_presets:[{id:'standard',label:'标准审查',limits}], focus_categories:['security'], scope_selection_supported:true, limits:{defaults:limits,maxima:limits} },
      prUrl:'https://github.com/o/r/pull/1', model:'', models:[], defaultModel:'fixture', modelsLoading:false, modelsError:null,
      submitting:false, dirty:true, previewing:true,
      previewJob: { id:'a'.repeat(32), request:{pr_url:'https://github.com/o/r/pull/1',mode:'review',generate_patches:false,validation_backend:'none'},
        status:'running', stages:[{phase:'clone',message:'正在克隆仓库',started_at:'2026-01-01T00:00:00Z',finished_at:null,percent:42,operation:'clone'}],
        cache_hit:false, created_at:'2026-01-01T00:00:00Z', updated_at:'2026-01-01T00:00:00Z', completed_at:null, error:null, result:null, consumed_task_id:null },
    }));
    assert.match(html, /正在克隆仓库 · 42%/);
    assert.match(html, /<button[^>]*disabled[^>]*>更新中…/);
    assert.match(html, /配置已变化，请先更新方案/);
    assert.doesNotMatch(html, /校验并保存方案/);
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
    assert.match(html, /value="7"[^>]*aria-label="最大模型调用次数"/);
    assert.match(html, /本次 PR 概况/);
    assert.match(html, /正在创建审查任务/);
    assert.match(html, /进度/);
    assert.match(html, /aria-busy="true"/);
    assert.match(html, /<button[^>]*disabled[^>]*>(?:(?!<\/button>)[\s\S])*正在创建审查任务…/);
  } finally { await server.close(); }
});

test('标准方案无需编辑即可启动，已有自定义范围和资源不会被简洁展示重置', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const { default: component } = await server.ssrLoadModule('/src/components/pages/ReviewPlanPage.vue');
    const limits = { max_model_calls: 12, max_tokens: 60000, max_cost_microusd: null, timeout_seconds: 60 };
    const props = {
      preview: { preview_id: 'saved', effective_plan: { version: 'review-plan-v1', depth: 'standard', focus: ['security'], scope: { mode: 'all', primary_files: [] }, limits: { ...limits } }, changed_files: [], review_units: [], excluded_files: [], risk_tags: [], warnings: [], included_file_count: 0, changed_file_count: 0 },
      capabilities: { depth_presets: [{ id: 'standard', label: '标准审查', limits }], focus_categories: ['security'], scope_selection_supported: true, limits: { defaults: limits, maxima: limits } },
      prUrl: 'https://github.com/o/r/pull/1', model: '', models: [], defaultModel: 'fixture', modelsLoading: false, modelsError: null, submitting: false, dirty: false, previewing: false,
    };
    const html = await renderToString(createSSRApp(component, props));
    assert.match(html, /标准审查 · 只读/);
    assert.match(html, /全部可审查变更 · 服务端资源预设/);
    const start = html.match(/<button\b[^>]*>(?:(?!<\/button>)[\s\S])*开始 AI 审查<\/button>/)?.[0];
    assert.ok(start);
    assert.doesNotMatch(start, /\bdisabled(?:\s|=|>)/);
    props.preview.effective_plan.scope = { mode: 'selected', primary_files: ['src/a.ts'] };
    props.preview.effective_plan.limits.max_model_calls = 3;
    const before = JSON.stringify(props.preview);
    const custom = await renderToString(createSSRApp(component, props));
    assert.match(custom, /自定义文件范围 · 自定义资源上限/);
    assert.equal(JSON.stringify(props.preview), before);
  } finally { await server.close(); }
});
