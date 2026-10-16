// 合成数据验收，计划由后端 Planner 生成，不读取真实任务或远端 PR。
import { createApp, h, ref } from "vue";
import ReviewViewer from "../src/components/review/ReviewViewer.vue";
import type { ReviewTask } from "../src/types/review";
import type { PublicationPlan } from "../src/types/publication";
import "../src/styles.css";

const fixtures = await (await fetch("/tests/fixtures/publication-plans.json")).json() as Record<string, { task: ReviewTask; plan: PublicationPlan }>;
const choice = ref("findings");
const task = ref(fixtures.findings!.task);
const originalFetch = window.fetch.bind(window);
window.fetch = async (input, init) => {
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
  h(ReviewViewer, { task: task.value, report: task.value.report_markdown, statusText: task.value.status,
    taskDuration: "1 秒", cancelling: false, retryingUnitId: null }),
]) }).mount("#preview");
