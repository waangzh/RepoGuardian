// 合成数据验收，计划由后端 Planner 生成，不读取真实任务或远端 PR。
import { createApp, h, ref } from "vue";
import ReviewWorkspace from "../src/components/review/ReviewWorkspace.vue";
import type { ReviewSection } from "../src/types/workspace";
import type { ReviewTask } from "../src/types/review";
import type { PublicationPlan } from "../src/types/publication";
import "../src/styles.css";
import "../src/styles/prototype.css";
import "../src/styles/workspace.css";

const fixtures = await (await fetch("/tests/fixtures/publication-plans.json")).json() as Record<string, { task: ReviewTask; plan: PublicationPlan }>;
const variants = await (await fetch("/tests/fixtures/review-comment-options.json")).json();
const choice = ref("findings");
const task = ref(fixtures.findings!.task);
const section = ref<ReviewSection>('report');
const snapshotState = ref('current');
const copiedText = ref('');
Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async (text: string) => { copiedText.value = text; } } });
const originalFetch = window.fetch.bind(window);
window.fetch = async (input, init) => {
  if (typeof input === 'string' && (input.endsWith('/review-comment-check') || input.endsWith('/review-comment-preview'))) {
    const plan = fixtures[choice.value]!.plan;
    const options = JSON.parse(typeof init?.body === 'string' ? init.body : '{}');
    const variant = variants[choice.value][`${options.detail || 'compact'}:${!!options.exclude_resolved}`];
    const preview = input.endsWith('/review-comment-preview');
    return new Response(JSON.stringify({ ...variant, review_id: task.value.id, status: preview ? 'unknown' : snapshotState.value,
      reviewed_head_sha: plan.snapshot.head_sha, latest_head_sha: snapshotState.value === 'current' ? plan.snapshot.head_sha : 'd'.repeat(40),
      checked_at: new Date().toISOString(), review_comment_markdown: variant.review_comment_markdown + (preview ? '' : `\n\n快照检查：${snapshotState.value}`) }),
      { headers: { 'Content-Type': 'application/json' } });
  }
  if (typeof input === "string" && input.endsWith("/publication-plan")) {
    if (choice.value === "error") return new Response("加载失败", { status: 503 });
    const fixture = fixtures[choice.value];
    return new Response(JSON.stringify(fixture!.plan), { headers: { "Content-Type": "application/json" } });
  }
  return originalFetch(input, init);
};
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
  h(ReviewWorkspace, { task: task.value, report: task.value.report_markdown, statusText: task.value.status,
    section: section.value, 'onNavigate': (value: ReviewSection) => { section.value = value; }, duration: "1 秒", cancelling: false, retryingUnitId: null }),
]) }).mount("#preview");
