import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';

const sha = char => char.repeat(40);
const task = status => ({ id: 'review', status, phase: status, pr_url: 'https://github.com/o/r/pull/1', pr: { owner: 'o', repo: 'r', number: 1, title: '<script>alert(1)</script>', body: null, html_url: 'https://github.com/o/r/pull/1' }, snapshot: { review_id: 'review', repository: 'o/r', repository_id: 1, pr_number: 1, declared_base_sha: sha('a'), base_sha: sha('b'), head_sha: sha('c') }, configuration: { provider: 'fixture', endpoint: '', model: 'fixture', options: { depth: 'standard', paths: null, focus: ['correctness'], include_pr_discussions: false, limits: {} }, prompt_version: 'review-vnext-1' }, changed_files: [], plan: { version: 'hunk-plan-v1', units: [], excluded: {}, incremental_from: null, history: [] }, report: null, review_cutoff: '', hard_deadline: '', created_at: '', updated_at: '', started_at: null, completed_at: null, error_code: status === 'failed' ? 'TimeoutError' : null });

test('预算耗尽说明具体限制，避免显示为未知执行异常', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    appType: 'custom',
  });
  try {
    const { executionErrorLabel } = await server.ssrLoadModule('/src/utils/nativeReview.ts');
    assert.equal(executionErrorLabel('agent_model_limit'), '达到单个 Agent 的模型调用上限，调查尚未完成');
    assert.equal(executionErrorLabel('agent_structured_output_missing'), '模型未返回有效的结构化结果，任务尚未完成');
    for (const code of ['model_call_limit', 'input_token_limit', 'output_token_limit',
      'context_capacity', 'tool_call_limit', 'review_cutoff', 'hard_deadline']) {
      assert.doesNotMatch(executionErrorLabel(code), /执行异常/);
    }
  } finally { await server.close(); }
});

test('新运行投影保留终态、快照和安全链接语义', async () => { const server = await createServer({ server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true, entries: [], include: [] }, appType: 'custom' }); try { const { terminal, phaseLabel, safeGitHubLink, isFileContextFor } = await server.ssrLoadModule('/src/utils/nativeReview.ts'); assert.equal(terminal('partial'), true); assert.equal(terminal('running'), false); assert.equal(phaseLabel('finished'), '审查结束'); assert.equal(safeGitHubLink('javascript:alert(1)'), undefined); assert.equal(safeGitHubLink('https://github.com/o/r/pull/1'), 'https://github.com/o/r/pull/1'); assert.equal(isFileContextFor({ file_id: 0, file_path: 'a.py', snapshot: task('completed').snapshot, base_lines: [], head_lines: [] }, 0, 'a.py', task('completed').snapshot), true); } finally { await server.close(); } });
test('五切片工作台展示运行中和失败状态，不渲染不可信 HTML', async () => { const server = await createServer({ server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true, entries: [], include: [] }, appType: 'custom' }); try { const { default: Component } = await server.ssrLoadModule('/src/components/review/ReviewWorkspace.vue'); for (const status of ['running', 'failed', 'cancelled']) { const html = await renderToString(createSSRApp(Component, { task: task(status) })); assert.match(html, /概览|变更|问题|活动|发布/); assert.match(html, /未执行目标代码/); assert.match(html, /&lt;script&gt;/); assert.doesNotMatch(html, /<script>alert/); if (status === 'running') assert.match(html, /停止审查/); } } finally { await server.close(); } });

test('总览经 JSON 读取后保留自然段和行内代码，只把其他标记作为文本', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    appType: 'custom',
  });
  try {
    const { default: Component } = await server.ssrLoadModule('/src/components/pages/ReviewOverviewPage.vue');
    const report = JSON.parse(JSON.stringify({
      version: 'd'.repeat(64), status: 'completed', findings: [], candidates: [], units: [],
      excluded: {}, reasons: [], usage: {},
      overview: {
        status: 'ready', input_hash: 'e'.repeat(64), error_code: null, sources: [],
        omitted: { changes: 1, sources: 2, findings: 0, decision_boundaries: 0 },
        omitted_materials: [{ kind: 'change', unit_id: 'unit', member_id: 'hunk',
          path: '<script>unsafe.py</script>', side: 'head', start_line: 437, end_line: 459,
          reason: 'material_capacity' }],
        output: { finding_order: [], change_overview: [
          { text: '调整 `weighted_posterior` 的区间边界。\r\n\r\n不收窄原区间。', source_refs: ['S1'] },
          { text: '补充边界参数化测试和中间值对照断言。', source_refs: ['S2'] },
          { text: '<img src=x onerror=alert(1)> [链接](javascript:alert(1)) `a<b` *强调*', source_refs: ['S1'] },
        ] },
      },
    }));
    const html = await renderToString(createSSRApp(Component, {
      task: task('completed'), report, findings: [], loading: false,
    }));
    assert.match(html, /<code>weighted_posterior<\/code>/);
    assert.match(html, /区间边界。\r\n\r\n不收窄原区间。/);
    assert.match(html, /<p class="rg-preserve-lines">.*补充边界参数化测试和中间值对照断言。/);
    assert.match(html, /&lt;img/);
    assert.match(html, /<code>a&lt;b<\/code>/);
    assert.match(html, /\*强调\*/);
    assert.doesNotMatch(html, /<img|<script>|<a[^>]+javascript:|<em>/);
    assert.match(html, /查看总览省略范围/);
    assert.match(html, /HEAD 437–459/);
    assert.match(html, /材料容量不足/);
    assert.match(html, /&lt;script&gt;unsafe.py&lt;\/script&gt;/);
  } finally { await server.close(); }
});

test('行内代码组件不把围栏、跨行反引号、HTML 和链接当作可执行内容', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    appType: 'custom',
  });
  try {
    const { default: Component } = await server.ssrLoadModule('/src/components/common/InlineCodeText.vue');
    for (const text of ['```shell\nalert(1)\n```', '`跨\n行`', '``双反引号``']) {
      const html = await renderToString(createSSRApp(Component, { text }));
      assert.doesNotMatch(html, /<code>|<pre>/);
    }
    const html = await renderToString(createSSRApp(Component, {
      text: '`<script>alert(1)</script>` [外链](javascript:alert(1))',
    }));
    assert.match(html, /<code>&lt;script&gt;alert\(1\)&lt;\/script&gt;<\/code>/);
    assert.doesNotMatch(html, /<script>|<a /);
  } finally { await server.close(); }
});
