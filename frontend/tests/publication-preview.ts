// 合成数据验收，计划由后端 Planner 生成，不读取真实任务或远端 PR。
import { createApp, h, ref } from "vue";
import ReviewWorkspace from "../src/components/review/ReviewWorkspace.vue";
import AppSidebar from '../src/components/layout/AppSidebar.vue';
import type { ReviewSection } from "../src/types/workspace";
import type { ReviewTask } from "../src/types/review";
import type { PublicationPlan } from "../src/types/publication";
import "../src/styles.css";
import "../src/styles/prototype.css";
import "../src/styles/workspace.css";

const fixtures = await (await fetch("/tests/fixtures/publication-plans.json")).json() as Record<string, { task: ReviewTask; plan: PublicationPlan; summary: import("../src/types/publication").ReviewSummary }>;
const variants = await (await fetch("/tests/fixtures/review-comment-options.json")).json();
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
const choice = ref("findings");
const task = ref(fixtures.findings!.task);
const section = ref<ReviewSection>('report');
const snapshotState = ref('current');
const copiedText = ref('');
const shell = new URLSearchParams(location.search).has('shell');
const mobileOpen = ref(false);
Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async (text: string) => { copiedText.value = text; } } });
const originalFetch = window.fetch.bind(window);
window.fetch = async (input, init) => {
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
      checked_at: new Date().toISOString(), review_comment_markdown: variant.review_comment_markdown + (preview ? '' : `\n\n快照检查：${snapshotState.value}`) }),
      { headers: { 'Content-Type': 'application/json' } });
  }
  if (typeof input === 'string' && input.endsWith('/findings')) {
    return new Response(JSON.stringify({ items: fixtures[choice.value]!.plan.digest.findings || [] }), { headers: { 'Content-Type': 'application/json' } });
  }
  if (typeof input === "string" && input.endsWith("/publication-summary")) {
    if (choice.value === "error") return new Response("加载失败", { status: 503 });
    const fixture = fixtures[choice.value];
    return new Response(JSON.stringify(fixture!.summary), { headers: { "Content-Type": "application/json" } });
  }
  if (typeof input === "string" && input.endsWith("/publication-plan")) {
    if (choice.value === "error") return new Response("加载失败", { status: 503 });
    const fixture = fixtures[choice.value];
    return new Response(JSON.stringify(fixture!.plan), { headers: { "Content-Type": "application/json" } });
  }
  return originalFetch(input, init);
};
function workspace() {
  return h(ReviewWorkspace, { task: task.value, report: task.value.report_markdown, statusText: task.value.status,
    section: section.value, 'onNavigate': (value: ReviewSection) => { section.value = value; }, duration: "1 秒", cancelling: false, retryingUnitId: null });
}
createApp({ setup: () => () => h("div", [
  h("div", { style: "padding:16px;background:#fff3dd;display:flex;gap:16px" }, [
    h("strong", "合成快照 · 后端发布计划验收"),
    h("select", { "aria-label": "验收场景", value: choice.value, onChange: (event: Event) => {
      choice.value = (event.target as HTMLSelectElement).value;
      task.value = structuredClone(fixtures[choice.value === "error" ? "inconclusive" : choice.value]!.task);
      if (choice.value === "error") task.value.id = "preview-error";
    } }, ["findings", "clean", "inconclusive", "failed", "cancelled", "withheld", "error"].map(value => h("option", { value }, value))),
  ]),
  h('select', { 'aria-label': '快照场景', value: snapshotState.value, onChange: (event: Event) => { snapshotState.value = (event.target as HTMLSelectElement).value; copiedText.value = ''; } }, ['current', 'stale', 'unknown'].map(value => h('option', {value}, value))),
  h('output', { 'aria-label': '已复制内容', style: 'display:block;max-height:120px;overflow:auto' }, copiedText.value),
  shell ? h('div', { class: 'rg-app app' }, [
    h(AppSidebar, { modelValue: 'dashboard', section: section.value, hasReview: true, taskActive: false, mobileOpen: mobileOpen.value, busy: false,
      onSection: (value: ReviewSection) => { section.value = value; mobileOpen.value = false; }, onClose: () => { mobileOpen.value = false; } }),
    h('main', { class: 'main' }, [h('button', { class: 'btn secondary', type: 'button', 'aria-label': '打开导航', onClick: () => { mobileOpen.value = true; } }, '导航'), h('div', { class: 'workspace-main' }, [workspace()])]),
  ]) : workspace(),
]) }).mount("#preview");
