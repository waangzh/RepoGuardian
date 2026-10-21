// 合成数据验收，计划由后端 Planner 生成，不读取真实任务或远端 PR。
import { createApp, h, ref } from "vue";
import ReviewWorkspace from "../src/components/review/ReviewWorkspace.vue";
import AppSidebar from '../src/components/layout/AppSidebar.vue';
import NewReviewPage from "../src/components/pages/NewReviewPage.vue";
import ReviewHistoryPage from "../src/components/pages/ReviewHistoryPage.vue";
import type { ReviewSection } from "../src/types/workspace";
import type { ReviewTask } from "../src/types/review";
import type { PublicationPlan } from "../src/types/publication";
import "../src/styles.css";
import "../src/styles/prototype.css";
import "../src/styles/workspace.css";

const fixtures = await (await fetch("/tests/fixtures/publication-plans.json")).json() as Record<string, { task: ReviewTask; plan: PublicationPlan; summary: import("../src/types/publication").ReviewSummary }>;
const variants = await (await fetch("/tests/fixtures/review-comment-options.json")).json();
if (new URLSearchParams(location.search).has('intro')) {
  for (const { plan } of Object.values(fixtures)) {
    plan.review_comment_markdown = plan.review_comment_markdown.replace('\n\n## ',
      '\n\n当前结论针对本轮变更范围；已参考 2 项历史讨论，另有 2 项历史事项未核验。\n\n## ');
  }
}
if (new URLSearchParams(location.search).has('unsafe')) {
  for (const { plan } of Object.values(fixtures)) plan.review_comment_markdown += '\n\n<img src="invalid" onerror="document.body.dataset.unsafe=1"><script>document.body.dataset.unsafe=1</script><style>body{display:none}</style>';
}
// 仅补充浏览器夹具的身份和 Diff，生产页面仍只使用 API 返回值。
for (const { task } of Object.values(fixtures)) {
  task.pr ||= { owner: 'fixture', repo: 'report-ui', number: 95, title: '[合成验收] 修复空返回值与调用方契约',
    html_url: 'https://github.com/fixture/report-ui/pull/95', clone_url: '', body: '仅用于界面验收',
    base: { ref: 'main', sha: 'c'.repeat(40), repo_clone_url: '' }, head: { ref: 'fix/empty-result', sha: 'a'.repeat(40), repo_clone_url: '' } };
  task.changed_files = [{ file_path: 'src/a.py', change_type: 'modified', additions: 1, deletions: 1, is_binary: false,
    hunks: [{ old_start: 6, old_length: 2, new_start: 6, new_length: 2, hunk_id: 'h1', added_lines: [], removed_lines: [], lines: [
      { kind: 'context', content: 'def result():', old_line_no: 6, new_line_no: 6 },
      { kind: 'deleted', content: '    return {}', old_line_no: 7, new_line_no: null },
      { kind: 'added', content: '    return None', old_line_no: null, new_line_no: 7 },
    ] }] }];
}
const params = new URLSearchParams(location.search);
const initialScene = params.get("scene") || "findings";
const choice = ref(fixtures[initialScene] ? initialScene : "findings");
const task = ref(fixtures[choice.value]!.task);
const section = ref<ReviewSection>(params.get("section") === "overview" ? "overview" : "report");
const snapshotState = ref('current');
const copiedText = ref('');
const createRequested = ref(false);
let summaryFailures = 0;
const shell = new URLSearchParams(location.search).has('shell');
const mobileOpen = ref(false);
Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async (text: string) => { copiedText.value = text; } } });
const originalFetch = window.fetch.bind(window);
window.fetch = async (input, init) => {
  if (typeof input === "string" && input.startsWith("/api/reviews?")) {
    const items = Object.values(fixtures).map(({ task, plan }) => ({ ...task,
      history_result: { outcome: plan.outcome, merge_decision: plan.digest.merge_decision } }));
    return new Response(JSON.stringify({ items, total: items.length, page: 1, page_size: 12 }));
  }
  if (typeof input === 'string' && input.endsWith('/publications')) {
    return new Response(JSON.stringify(init?.method && init.method !== 'GET' ? { detail: '合成验收禁止真实发布' } : []),
      { status: init?.method && init.method !== 'GET' ? 403 : 200, headers: { 'Content-Type': 'application/json' } });
  }
  if (typeof input === 'string' && (input.endsWith('/review-comment-check') || input.endsWith('/review-comment-preview'))) {
    const plan = fixtures[choice.value]!.plan;
    const options = JSON.parse(typeof init?.body === 'string' ? init.body : '{}');
    const variant = variants[choice.value][`${options.detail || 'compact'}:${!!options.exclude_resolved}${options.finding_key ? ':' + options.finding_key : ''}`];
    const preview = input.endsWith('/review-comment-preview');
    return new Response(JSON.stringify({ ...variant, review_id: task.value.id, status: preview ? 'unknown' : snapshotState.value,
      reviewed_head_sha: plan.snapshot.head_sha, latest_head_sha: snapshotState.value === 'current' ? plan.snapshot.head_sha : 'd'.repeat(40),
      checked_at: new Date().toISOString(), preview_markdown: variant.review_comment_markdown,
      review_comment_markdown: variant.review_comment_markdown + (preview ? '' : `\n\n快照检查：${snapshotState.value}`) }),
      { headers: { 'Content-Type': 'application/json' } });
  }
  if (typeof input === 'string' && input.endsWith('/findings')) {
    return new Response(JSON.stringify({ items: fixtures[choice.value]!.plan.digest.findings || [] }), { headers: { 'Content-Type': 'application/json' } });
  }
  if (typeof input === "string" && input.endsWith("/publication-summary")) {
    if (choice.value === "error" && summaryFailures > 0) {
      summaryFailures -= 1;
      return new Response("加载失败", { status: 503 });
    }
    const fixture = fixtures[choice.value === "error" ? "inconclusive" : choice.value];
    return new Response(JSON.stringify({ ...fixture!.summary, review_id: task.value.id }), { headers: { "Content-Type": "application/json" } });
  }
  if (typeof input === "string" && input.endsWith("/publication-plan")) {
    await new Promise(resolve => setTimeout(resolve, 120));
    if (choice.value === "error") return new Response("加载失败", { status: 503 });
    const fixture = fixtures[choice.value];
    return new Response(JSON.stringify(fixture!.plan), { headers: { "Content-Type": "application/json" } });
  }
  return originalFetch(input, init);
};
function workspace() {
  return h(ReviewWorkspace, { task: task.value, report: task.value.report_markdown, statusText: task.value.status,
    section: section.value, 'onNavigate': (value: ReviewSection) => { section.value = value; },
    onCreate: () => { createRequested.value = true; }, onRereview: () => { createRequested.value = true; },
    duration: "1 秒", cancelling: false, retryingUnitId: null });
}
createApp({ setup: () => () => h("div", [
  params.get("page") === "history" ? h("div", { class: "rg-app" }, [h(ReviewHistoryPage)])
    : params.get("page") === "new" ? h("div", { class: "rg-app" }, [h(NewReviewPage, { prUrl: "", loading: false, busy: false })]) : null,
  h("button", { "aria-label": "刷新同任务报告", onClick: () => {
    const fixture = fixtures[choice.value]!;
    fixture.plan.user_report_markdown += "\n\n报告更新验收";
    fixture.plan.review_comment_markdown += "\n\n报告更新验收";
    task.value = { ...task.value, task_runtime_report: { ...task.value.task_runtime_report! } };
  } }, "刷新同任务报告"),
  h("div", { style: "padding:16px;background:#fff3dd;display:flex;gap:16px" }, [
    h("strong", "合成快照 · 后端发布计划验收"),
    h("select", { "aria-label": "验收场景", value: choice.value, onChange: (event: Event) => {
      choice.value = (event.target as HTMLSelectElement).value;
      createRequested.value = false;
      summaryFailures = choice.value === "error" ? 1 : 0;
      task.value = structuredClone(fixtures[choice.value === "error" ? "inconclusive" : choice.value]!.task);
      if (choice.value === "error") task.value.id = "preview-error";
    } }, ["findings", "clean", "inconclusive", "failed", "cancelled", "withheld", "error"].map(value => h("option", { value }, value))),
  ]),
  h('select', { 'aria-label': '快照场景', value: snapshotState.value, onChange: (event: Event) => { snapshotState.value = (event.target as HTMLSelectElement).value; copiedText.value = ''; } }, ['current', 'stale', 'unknown'].map(value => h('option', {value}, value))),
  h('output', { 'aria-label': '已复制内容', style: 'display:block;max-height:120px;overflow:auto' }, copiedText.value),
  createRequested.value ? h('output', { 'aria-label': '重新审查入口' }, '已请求重新分析 PR（合成验收，不创建真实任务）') : null,
  params.has("page") ? null : shell ? h('div', { class: 'rg-app app' }, [
    h(AppSidebar, { modelValue: 'dashboard', section: section.value, hasReview: true, taskActive: false, mobileOpen: mobileOpen.value, busy: false,
      onSection: (value: ReviewSection) => { section.value = value; mobileOpen.value = false; }, onClose: () => { mobileOpen.value = false; } }),
    h('main', { class: 'main' }, [h('button', { class: 'btn secondary', type: 'button', 'aria-label': '打开导航', onClick: () => { mobileOpen.value = true; } }, '导航'), h('div', { class: 'workspace-main' }, [workspace()])]),
  ]) : workspace(),
]) }).mount("#preview");
