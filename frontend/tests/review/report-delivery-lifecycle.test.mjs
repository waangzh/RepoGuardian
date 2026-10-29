import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import * as Vue from 'vue';
import { parse, compileScript } from '@vue/compiler-sfc';
import { transformSync } from 'esbuild';

const flush = async () => { for (let i = 0; i < 12; i++) await Vue.nextTick(); };
const text = node => [node.text ?? '', ...node.children.map(text)].join('');
const find = (node, predicate) => predicate(node) ? node : node.children.map(child => find(child, predicate)).find(Boolean);
const stub = Vue.defineComponent({ render: () => Vue.h('span') });

test('总览仅在实际变更被省略时提示部分内容，不改变审查结论与覆盖', async () => {
  const page = await compile('components/pages/ReviewOverviewPage.vue', {
    '../../utils/nativeReview': { terminal: () => true, observedUsage: () => ({ calls: 2, tokens: 100 }),
      phaseLabel: value => value },
  });
  const plan = Vue.reactive({
    outcome: { kind: 'clean', unresolved_count: 0 },
    digest: { merge_decision: { label: '未发现已确认问题', advice: '结合 CI 判断', limits: [] } },
    presentation: { summary_source: 'model', synthesis_status: 'ready',
      change_summary: '调整区间边界。\n\n补充边界断言。', review_summary: '', action_summary: '',
      synthesis: { omitted: { sources: 3, observations: 2 }, omitted_changes: [] } },
  });
  const task = { status: 'completed', phase: 'completed', steps: [], warnings: [],
    pr: { title: '区间修复', body: '' }, changed_files: [], configuration: { model: 'fixture' },
    review_report: { complete: true, findings: [], goals: [], coverage_limitations: [] } };
  const root = { children: [] };
  const app = hostRenderer().createApp(page, { task, plan, loading: false });
  app.mount(root);
  await flush();
  assert.match(text(root), /调整区间边界/);
  assert.doesNotMatch(text(root), /总览涵盖部分变更/);
  plan.presentation.synthesis.omitted_changes.push({ path: 'other.py', reason: 'material_limit', hunk_index: 0 });
  await flush();
  assert.match(text(root), /总览涵盖部分变更/);
  assert.match(text(root), /未发现已确认问题/);
  assert.equal(task.review_report.complete, true);
  app.unmount();
});

test('历史和最近记录读取独立摘要，坏快照明确展示且不阻止正常记录打开', async () => {
  const items = [
    { id: 'good', status: 'completed', phase: 'completed', pr_url: 'https://example.test/pr/1',
      repository: 'owner/repo', pr_number: 1, pr_title: '正常记录', head_sha: 'a'.repeat(40),
      created_at: '2026-10-29T04:00:00Z', updated_at: '2026-10-29T04:00:00Z', snapshot_error: null },
    { id: 'broken', status: 'completed', phase: 'completed', pr_url: 'https://example.test/pr/2',
      repository: 'owner/repo', pr_number: 2, pr_title: '快照不可读取', head_sha: null,
      created_at: '2026-10-28T04:00:00Z', updated_at: '2026-10-28T04:00:00Z',
      snapshot_error: 'history_snapshot_unavailable' },
  ];
  const dependencies = {
    '../../api/client': { listReviews: async () => ({ items, total: 2, page: 1, page_size: 20 }),
      deleteReview: async () => {} },
    '../../utils/nativeReview': { terminal: value => value === 'completed' },
  };
  for (const path of ['components/pages/ReviewHistoryPage.vue', 'components/pages/NewReviewPage.vue']) {
    const component = await compile(path, dependencies);
    const root = { children: [] }, opened = [];
    const history = path.includes('ReviewHistory');
    const app = hostRenderer().createApp(component, {
      busy: false, onOpen: id => opened.push(id), ...(history ? {} : { prUrl: '', model: '', models: [],
      options: { focus: ['correctness'], depth: 'standard', scope: { mode: 'all', primary_files: [] } },
      capabilities: null }),
    });
    app.mount(root);
    await flush();
    assert.match(text(root), /正常记录/);
    assert.match(text(root), /快照不可读取/);
    const good = find(root, node => node.tag === 'button'
      && (history ? text(node).includes('正常记录') : node.props.class === 'recent-row')
      && !node.props.disabled);
    assert.ok(good);
    good.props.onClick();
    assert.deepEqual(opened, ['good']);
    const broken = find(root, node => node.tag === 'button'
      && text(node).includes('快照不可读取') && node.props.disabled);
    assert.ok(broken);
    app.unmount();
  }
});

function hostRenderer() {
  const make = tag => ({ tag, text: '', props: {}, children: [], parent: null, style: {}, value: '',
    tagName: tag.toUpperCase(), addEventListener() {}, removeEventListener() {},
    getRootNode() { return globalThis.document; },
    setAttribute(key, value) { this.props[key] = value; }, removeAttribute(key) { delete this.props[key]; },
    showModal() { this.open = true; }, close() { this.open = false; } });
  return Vue.createRenderer({
    createElement: make, createText: value => ({ ...make('#text'), text: value }),
    createComment: () => make('#comment'), setText: (node, value) => { node.text = value; },
    setElementText: (node, value) => { node.text = value; node.children = []; },
    patchProp: (node, key, _old, value) => { node.props[key] = value; },
    insert(node, parent, anchor) {
      if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
      node.parent = parent;
      const index = anchor ? parent.children.indexOf(anchor) : -1;
      parent.children.splice(index < 0 ? parent.children.length : index, 0, node);
    },
    remove(node) { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); node.parent = null; },
    parentNode: node => node.parent,
    nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
    setScopeId() {},
  });
}

async function compile(relative, dependencies) {
  const filename = fileURLToPath(new URL(`../../src/${relative}`, import.meta.url));
  const { descriptor } = parse(await readFile(filename, 'utf8'), { filename });
  const script = compileScript(descriptor, { id: relative, inlineTemplate: true });
  const { code } = transformSync(script.content, { loader: 'ts', format: 'cjs', target: 'es2022' });
  const module = { exports: {} };
  const require = name => {
    if (name === 'vue') return Vue;
    if (name in dependencies) return { __esModule: true, ...dependencies[name] };
    if (name.endsWith('.vue')) return { __esModule: true, default: stub };
    throw new Error(`Unmocked dependency: ${name}`);
  };
  new Function('require', 'module', 'exports', code)(require, module, module.exports);
  return module.exports.default;
}

test('折叠发布、切换阅读与工作区仍保持口令及轮询，卸载后停止', async t => {
  const previousDocument = globalThis.document;
  const PreviousDocument = globalThis.Document;
  globalThis.Document = class Document {};
  globalThis.document = Object.assign(new Document(), { activeElement: null });
  t.after(() => {
    if (previousDocument === undefined) delete globalThis.document; else globalThis.document = previousDocument;
    if (PreviousDocument === undefined) delete globalThis.Document; else globalThis.Document = PreviousDocument;
  });
  const plan = { review_id: 'review', snapshot: { head_sha: 'b'.repeat(40), effective_base_sha: 'c'.repeat(40) },
    review_comment_markdown: '正文', user_report_markdown: '报告', inline_drafts: [], summary_findings: [] };
  const task = { id: 'review', status: 'completed', phase: 'completed', snapshot: plan.snapshot,
    steps: [], error: null, pr: { owner: 'owner', repo: 'repo', number: 1, title: 'PR' } };
  let polls = 0;
  let keepQueued = false;
  const api = {
    getPublicationCapability: async () => ({ enabled: true }),
    getPublicationPlan: async () => plan, getFindings: async () => ({ items: [], limitations: [] }), getReport: async () => '报告',
    preflightPublication: async () => ({ ready: true, plan_hash: 'hash', head_sha: plan.snapshot.head_sha,
      summary_action: 'create', summary_markdown: 'summary', inline: { ready: 0, already_published: 0, summary_fallback: 0 },
      inline_previews: [], warnings: [] }),
    createPublication: async () => ({ publication_id: 'attempt' }),
    getPublication: async () => ({ id: 'attempt', state: ++polls === 1 || keepQueued ? 'queued' : 'succeeded', items: [] }),
  };
  const labels = { publicationStateLabel: { queued: '排队中', succeeded: '成功' },
    publicationErrorText: String, publicationItemKindLabel: String, publicationItemStateLabel: {},
    publicationWarningText: String, inlineActionLabel: {}, summarySourceLabel: String };
  const native = { terminal: status => status === 'completed', executionErrorLabel: String,
    resumeLabel: () => null, phaseLabel: String, safeGitHubLink: () => undefined };
  const copy = { useReviewCommentCopy: source => ({ previewMarkdown: Vue.computed(() => source.markdown()),
    copied: Vue.ref(false), feedback: Vue.ref(''), checking: Vue.ref(false), pendingCopy: Vue.ref(null),
    headCheck: Vue.ref(null), copyCheckedComment() {}, copyAcknowledged() {} }) };
  const Panel = await compile('components/review/publication/GitHubPublicationPanel.vue', {
    '../../../api/client': api, '../../../utils/reportPresentation': { publicationFindingTitle: item => item.title },
    '../../../utils/publicationPresentation': labels, dompurify: { default: { sanitize: String } },
    marked: { marked: { parse: String } },
  });
  const Page = await compile('components/pages/ReviewReportPage.vue', {
    '../../api/client': api, '../../utils/nativeReview': native, '../../composables/useReviewCommentCopy': copy,
    '../review/publication/GitHubPublicationPanel.vue': { default: Panel },
  });
  const Workspace = await compile('components/review/ReviewWorkspace.vue', {
    '../../api/client': api, '../../utils/nativeReview': native,
    '../../types/workspace': { reviewPrimarySections: [], reviewSecondarySections: [] },
    '../pages/ReviewReportPage.vue': { default: Page },
  });
  const section = Vue.ref('report');
  const root = { children: [] };
  const app = hostRenderer().createApp({ render: () => Vue.h(Workspace, { task, section: section.value }) });
  app.mount(root);
  let unmounted = false;
  t.after(() => { if (!unmounted) app.unmount(); });
  await flush();
  const button = label => {
    const node = find(root, item => item.tag === 'button' && text(item).trim() === label);
    assert.ok(node, `missing button: ${label}`);
    return node;
  };
  button('发布到 GitHub').props.onClick();
  await flush();
  const panel = find(root, item => item.props?.['aria-label'] === '发布到 GitHub');
  const token = find(panel, item => item.tag === 'input' && item.props.type === 'password');
  token.props['onUpdate:modelValue']('local-secret');
  await flush();
  button('检查发布条件并预览').props.onClick();
  await flush();
  button('确认发布').props.onClick();
  button('确认发布到 GitHub').props.onClick();
  await flush();
  assert.equal(polls, 1);
  button('收起发布面板').props.onClick();
  await flush();
  button('详细报告').props.onClick();
  await flush();
  section.value = 'overview';
  await flush();
  assert.equal(find(root, item => item === panel), panel);
  assert.equal(token.value, 'local-secret');
  await new Promise(resolve => setTimeout(resolve, 1600));
  await flush();
  assert.equal(polls, 2, 'hidden publication must keep polling until success');
  section.value = 'report';
  await flush();
  button('审查意见').props.onClick();
  await flush();
  assert.match(text(root), /已发布总结评论/);
  assert.equal(find(root, item => item === panel), panel);
  keepQueued = true;
  button('发布到 GitHub').props.onClick();
  await flush();
  button('确认发布').props.onClick();
  button('确认发布到 GitHub').props.onClick();
  await flush();
  assert.equal(polls, 3);
  app.unmount();
  unmounted = true;
  await new Promise(resolve => setTimeout(resolve, 1600));
  assert.equal(polls, 3, 'unmounted workspace must stop polling');
});
