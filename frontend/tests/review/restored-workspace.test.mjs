import test, { after, before } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';

let server;
before(async () => {
  server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    appType: 'custom',
  });
});
after(async () => { await server?.close(); });

async function render(path, props) {
  const { default: component } = await server.ssrLoadModule(`/src/components/${path}.vue`);
  return renderToString(createSSRApp(component, props));
}
const snapshot = { head_sha: 'b'.repeat(40), effective_base_sha: 'c'.repeat(40), declared_base_sha: 'd'.repeat(40) };
const limits = { model_calls: 12, tool_calls: 24, concurrency: 3, timeout_seconds: 600, output_tokens: 2048 };
const options = {
  depth: 'standard', focus: ['correctness'], include_pr_discussions: false,
  scope: { mode: 'all', primary_files: [] }, limits,
};
const file = {
  file_path: 'src/safe.py', old_file_path: null, change_type: 'modified',
  additions: 1, deletions: 1, is_binary: false,
  hunks: [{ hunk_id: 'h1', old_start: 7, old_length: 1, new_start: 9, new_length: 1,
    lines: [
      { kind: 'deleted', content: '<script>old()</script>', old_line_no: 7, new_line_no: null },
      { kind: 'added', content: 'new_value', old_line_no: null, new_line_no: 9 },
    ] }],
};
function task(status = 'failed') {
  return {
    id: 'a'.repeat(32), status, phase: status, snapshot,
    pr: { owner: 'owner', repo: 'repo', number: 1, title: '固定快照', body: null, html_url: '' },
    pr_url: 'https://github.com/owner/repo/pull/1',
    configuration: { provider: 'fixture', model: 'fixture-model', options, limits },
    changed_files: [file], review_report: null, warnings: [], error: null,
    steps: [{ name: 'stale-agent', status: 'running', message: '不应仍在运行', data: {}, updated_at: '2026-10-27T00:00:00Z' }],
  };
}
function finding(id, review_status, overrides = {}) {
  return {
    id, review_status, status: review_status, title: `${id} title`,
    evidence_status: review_status === 'confirmed' ? 'verified' : 'unresolved',
    attribution: 'introduced', severity: 'high', message: 'trigger', impact: 'impact',
    recommendation: 'recommendation', limitations: [], disposition: null,
    location: { path: file.file_path, start_line: 7, end_line: 7, side: 'left' },
    verification: { method: 'static_model_review', evidence_status: 'unresolved' },
    ...overrides,
  };
}

test('修改配置后必须更新冻结方案，并展示当前派发视角和额度', async () => {
  const preview = {
    preview_id: 'e'.repeat(32), snapshot, effective_plan: options, limits, warnings: [],
    expires_at: '2026-10-27T12:00:00Z', goals: [{ id: 'g1' }],
    review_units: [{ id: 'unit-1', changed_paths: [file.file_path], risk_tier: 'low', strategy: 'contextual_review' }],
    assignments: [{ id: 'contract-1', unit_id: 'unit-1', reviewer: 'contract', objective: '核对当前契约', focus_paths: [file.file_path] }],
    changed_files: [{ file_path: file.file_path, included: true, additions: 1, deletions: 1 }],
  };
  const props = { preview, prUrl: task().pr_url, options, model: 'draft-model', capabilities: null, busy: false };
  const dirty = await render('pages/ReviewPlanPage', { ...props, dirty: true });
  assert.match(dirty, /<button[^>]*disabled[^>]*>[\s\S]*?开始 AI 审查<\/button>/);
  assert.match(dirty, /上一次冻结方案/);
  assert.match(dirty, /契约审查/);
  assert.match(dirty, /600 秒 \/ 3/);
  assert.match(dirty, /12 次模型 \/ 24 次工具/);
  assert.ok(dirty.includes(snapshot.head_sha));
  assert.doesNotMatch(dirty, /task_review_runtime|legacy_units|旧版运行时/);
  const ready = await render('pages/ReviewPlanPage', { ...props, dirty: false });
  assert.match(ready, /<button class="btn primary">[\s\S]*?开始 AI 审查<\/button>/);
});

test('失败或取消的概览保留未知，不显示遗留运行中步骤', async () => {
  for (const status of ['failed', 'cancelled']) {
    const html = await render('pages/ReviewOverviewPage', { task: task(status), plan: null, loading: false });
    assert.match(html, status === 'failed' ? /审查中断/ : /审查已停止/);
    assert.match(html, /未知 次模型调用 · 未知 Tokens/);
    assert.match(html, /覆盖范围尚未结算/);
    assert.doesNotMatch(html, /不应仍在运行|ov-count-danger|NaN|undefined/);
  }
});

test('概览在摘要未就绪时仍显示规划限制，并与摘要中的相同限制去重', async () => {
  const value = task('completed');
  const limitation = '预算不足：专项审查未启动 <script>unsafe()</script>';
  value.review_report = {
    complete: true, goals: [], findings: [], discovery_results: [], verification_results: [], limitation_results: [],
    coverage_limitations: [limitation],
  };
  for (const plan of [null, { outcome: { kind: 'inconclusive' }, digest: {
    merge_decision: { label: '审查方案仍有限制', advice: '核对限制', limits: [limitation] },
  } }]) {
    const html = await render('pages/ReviewOverviewPage', { task: value, plan, loading: false });
    assert.equal(html.split('预算不足：专项审查未启动').length - 1, 1);
    assert.match(html, /&lt;script&gt;unsafe\(\)&lt;\/script&gt;/);
    assert.doesNotMatch(html, /<script>/);
  }
});

test('零缺陷记录仍展示独立的待复核声明和必要证据缺口', async () => {
  const limitations = [
    {id:'pending',status:'pending',text:'未证明全局正确 <script>unsafe()</script>',assessment:null},
    {id:'gap',status:'unresolved',text:'必要调用方源码缺失',assessment:{
      dependent_conclusion:'新返回值是否破坏调用方契约',change_relevance:'本次修改改变返回值',next_check:'读取具体校验函数'}},
    {id:'boundary',status:'boundary',text:'已归类的边界，不再是待办',assessment:null},
  ];
  const html = await render('pages/ReviewFindingsPage',{task:task('completed'),findings:[],limitations});
  assert.match(html,/审查待核实事项/);
  assert.match(html,/限制声明尚待复核/);
  assert.match(html,/必要证据缺口/);
  assert.match(html,/读取具体校验函数/);
  assert.match(html,/尚无匹配的代码缺陷记录/);
  assert.match(html,/&lt;script&gt;unsafe/);
  assert.doesNotMatch(html,/<script>|已归类的边界，不再是待办/);
});

test('问题页区分机器确认、待核实和已排除，并转义不可信文本', async () => {
  const findings = [finding('confirmed-item', 'confirmed'), finding('unresolved-item', 'unresolved'), finding('dismissed-item', 'dismissed')];
  for (const state of ['confirmed', 'unresolved', 'dismissed']) {
    const html = await render('pages/ReviewFindingsPage', { task: task(), findings, limitations: [], initialFilter: state });
    assert.ok(html.includes(`${state}-item title`));
    for (const other of ['confirmed', 'unresolved', 'dismissed'].filter(value => value !== state)) {
      assert.ok(!html.includes(`${other}-item title`));
    }
    assert.match(html, /人工注记不改变发布资格/);
  }
  const html = await render('pages/ReviewFindingsPage', {
    task: task(), limitations: [], findings: [finding('unsafe', 'confirmed', { title: '<script>title()</script>' })],
  });
  assert.match(html, /&lt;script&gt;title\(\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /src\/safe.py:7[\s\S]*?BASE/);
});

test('删除行按 BASE 行号高亮，原 Diff 不执行模型或源码文本', async () => {
  const html = await render('review/diff/ReviewDiff', {
    file, fileId: 0, taskId: task().id, snapshot, highlightLine: 7, highlightSide: 'left',
  });
  const deleted = html.match(/<div\b[^>]*data-kind="deleted"[^>]*>/)?.[0];
  const added = html.match(/<div\b[^>]*data-kind="added"[^>]*>/)?.[0];
  assert.ok(deleted && /class="[^"]*\bhighlighted\b/.test(deleted));
  assert.ok(added && !/class="[^"]*\bhighlighted\b/.test(added));
  assert.match(html, /&lt;script&gt;old\(\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});

test('完整源码拒绝错文件、错快照、非文本行，空文件仍为有效响应', async () => {
  const { isFileContextFor } = await server.ssrLoadModule('/src/utils/nativeReview.ts');
  const value = { file_id: 0, file_path: file.file_path, snapshot, base_lines: [], head_lines: ['safe'] };
  assert.equal(isFileContextFor(value, 0, file.file_path, snapshot), true);
  for (const invalid of [null, {}, { ...value, file_id: 1 }, { ...value, file_path: '../other.py' },
    ...Object.keys(snapshot).map(key => ({ ...value, snapshot: { ...snapshot, [key]: 'f'.repeat(40) } })),
    { ...value, base_lines: [42] }, { ...value, head_lines: null }]) {
    assert.equal(isFileContextFor(invalid, 0, file.file_path, snapshot), false);
  }
});

test('覆盖页不把缺失结算显示为完整，保留受阻范围和原因', async () => {
  const missing = await render('pages/ReviewScopePage', { task: task(), plan: null });
  assert.match(missing, /覆盖范围尚未结算/);
  const blocked = task();
  blocked.review_report = { goals: [{ id: 'g1', path: file.file_path, side: 'base', start_line: 7, end_line: 7, status: 'blocked', reason: '源码读取被拒绝' }] };
  const html = await render('pages/ReviewScopePage', { task: blocked, plan: null });
  assert.match(html, /受阻/);
  assert.match(html, /源码读取被拒绝/);
  assert.match(html, /BASE/);
});

test('恢复的侧栏与设置可在无 document 和 localStorage 的环境渲染', async () => {
  const sidebar = await render('layout/AppSidebar', { modelValue: 'dashboard', section: 'overview', hasReview: true, taskActive: false, mobileOpen: false, busy: false });
  for (const label of ['主导航', '审查历史', '系统设置', '补充证据']) assert.ok(sidebar.includes(label));
  const settings = await render('pages/SettingsPage', {});
  assert.match(settings, /主题/);
  assert.match(settings, /信息密度/);
  assert.match(settings, /诊断尚不可用/);
});

test('预览仍在准备时可以从历史或设置返回当前方案', async () => {
  const html = await render('layout/AppSidebar', {
    modelValue: 'history', section: 'overview', hasReview: false, hasPreview: true,
    taskActive: false, mobileOpen: false, busy: true,
  });
  assert.match(html, /返回当前审查方案/);
  const returnButton = [...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].find(match => match[0].includes('返回当前审查方案'))?.[0];
  assert.ok(returnButton && !/\sdisabled(?:[\s=>])/.test(returnButton));
});
