import test, { after, before } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { createServer } from 'vite';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';

const dompurifyStub = fileURLToPath(new URL('../fixtures/dompurify-stub.mjs', import.meta.url));
let server;
before(async () => {
  server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    resolve: { alias: { dompurify: dompurifyStub } },
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
function publicationPlan(overrides = {}) {
  return {
    review_id: 'a'.repeat(32),
    snapshot: { head_sha: snapshot.head_sha, effective_base_sha: snapshot.effective_base_sha },
    outcome: { kind: 'findings', coverage: 'complete', confirmed_count: 1, publishable_count: 1, unresolved_count: 0,
      examined_goals: 1, total_goals: 1, incomplete_investigations: 0, pending_limitation_count: 0,
      review_limitations: [], reason_codes: [], finding_conclusion: 'confirmed', review_completeness: 'complete', runtime_reliability: 'reliable' },
    digest: { examined_goals: [], incomplete_goals: [], reviewed_files: [], excluded_files: [], checked_areas: [] },
    summary_markdown: '总结', user_report_markdown: '## 详细报告正文',
    review_comment_markdown: '## 审查结果\n\n已确认 1 个问题。',
    inline_drafts: [], summary_findings: [], withheld_findings: [], content_eligible: true, blocking_reasons: [],
    ...overrides,
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

test('概览展示变更总览与问题概述，省略和元数据范围明确标记', async () => {
  const current = task('completed');
  const presentation = {
    summary_source: 'model', synthesis_status: 'ready',
    change_summary: '评论改为批次投递并保存恢复标识。',
    review_summary: '问题集中在响应丢失后的恢复路径。',
    action_summary: '',
    synthesis: { omitted: { sources: 1 }, omitted_changes: [
      { path: 'other.py', reason: 'editorial_omission', hunk_index: null },
    ] },
  };
  const html = await render('pages/ReviewOverviewPage', {
    task: current, plan: publicationPlan({ presentation }), loading: false,
  });
  assert.match(html, /评论改为批次投递并保存恢复标识/);
  assert.match(html, /响应丢失后的恢复路径/);
  assert.doesNotMatch(html, /处理重点/);
  assert.match(html, /总览涵盖部分变更/);
  const fallback = await render('pages/ReviewOverviewPage', {
    task: current, plan: publicationPlan({ presentation: { ...presentation, summary_source: 'metadata', synthesis_status: 'fallback' } }), loading: false,
  });
  assert.match(fallback, /变更范围/);
  assert.match(fallback, /变更总览尚未生成/);
  assert.doesNotMatch(fallback, /评论改为批次投递并保存恢复标识/);
});

test('默认阅读核验正文，技术理由留在可展开详情且文本经过转义', async () => {
  const item = finding('verified', 'confirmed', {
    author_body: '仅在响应丢失时会重复发布。<script>unsafe()</script>',
    verification_reason: 'BASE 和 HEAD 已分别核对。',
    impact: '不应默认显示的旧影响',
  });
  const html = await render('pages/ReviewFindingsPage', {
    task: task('completed'), findings: [item], limitations: [], loading: false,
  });
  assert.match(html, /仅在响应丢失时会重复发布/);
  assert.match(html, /&lt;script&gt;unsafe\(\)&lt;\/script&gt;/);
  assert.match(html, /<details[^>]*>[\s\S]*独立核验理由[\s\S]*BASE 和 HEAD 已分别核对/);
  assert.doesNotMatch(html, /不应默认显示的旧影响/);
});

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

test('零缺陷记录仍展示局部缺口及历史声明，不承诺追加独立复核', async () => {
  const limitations = [
    {id:'pending',status:'pending',text:'未证明全局正确 <script>unsafe()</script>',assessment:null},
    {id:'gap',status:'unresolved',text:'必要调用方源码缺失',assessment:{
      dependent_conclusion:'新返回值是否破坏调用方契约',change_relevance:'本次修改改变返回值',next_check:'读取具体校验函数'}},
    {id:'boundary',status:'boundary',text:'已归类的边界，不再是待办',assessment:null},
    {id:'local',status:'reported',text:'局部输入前提未核实',goal_ids:['head'],gap:{
      goal_ids:['head','outside'],dependent_conclusion:'输入是否满足约束',change_relevance:'修改输入条件',next_check:'查看入参校验源码'}},
    {id:'candidate-gap',source:'verification',status:'reported',text:'候选缺少调用方证据',assessment:null},
  ];
  const html = await render('pages/ReviewFindingsPage',{task:task('completed'),findings:[],limitations});
  assert.match(html,/审查待核实事项/);
   assert.match(html,/历史未评估声明/);
   assert.match(html,/主审局部证据缺口/);
   assert.match(html,/候选核验证据缺口/);
   assert.match(html,/查看入参校验源码/);
   assert.match(html,/部分关联目标未通过范围校验/);
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

test('审查意见默认展示完整评论正文，重要状态提示不折叠', async () => {
  const html = await render('pages/ReviewReportPage', { task: task('completed'), plan: publicationPlan(), report: '', loading: false });
  for (const label of ['审查意见', '详细报告', '技术记录', '复制完整 PR 评论', '发布到 GitHub']) assert.ok(html.includes(label), label);
  assert.match(html, /class="active"[^>]*aria-current="page"[^>]*>审查意见</);
  assert.match(html, /<h2>审查结果<\/h2>/);
  assert.match(html, /已确认 1 个问题/);
  assert.ok(html.includes(snapshot.head_sha) && html.includes(snapshot.effective_base_sha));
  const failed = await render('pages/ReviewReportPage', { task: task('failed'), plan: publicationPlan(), report: '', loading: false });
  assert.match(failed.replace(/<details[\s\S]*?<\/details>/g, ''), /审查未正常结束/);
  const outcome = { ...publicationPlan().outcome, review_completeness: 'partial', coverage: 'partial', incomplete_investigations: 2, pending_limitation_count: 1 };
  const partial = await render('pages/ReviewReportPage', { task: task('completed'), plan: publicationPlan({ outcome }), report: '', loading: false });
  assert.match(partial.replace(/<details[\s\S]*?<\/details>/g, ''), /覆盖不完整：已检查 1 \/ 1 项约定目标，具体缺口见覆盖页/);
});

test('恢复的侧栏与设置可在无 document 和 localStorage 的环境渲染', async () => {
  const sidebar = await render('layout/AppSidebar', { modelValue: 'dashboard', section: 'overview', hasReview: true, taskActive: false, mobileOpen: false, busy: false });
  for (const label of ['主导航', '审查历史', '系统设置', '补充证据', '审查意见']) assert.ok(sidebar.includes(label));
  assert.ok(!sidebar.includes('发布或复制'));
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
