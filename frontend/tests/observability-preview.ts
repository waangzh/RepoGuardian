// 本地 Vite 验收入口；不包含在生产构建中，不读取或写入真实任务。
import { createApp, h, ref } from "vue";
import ReviewViewer from "../src/components/review/ReviewViewer.vue";
import type { ReviewTask } from "../src/types/review";
import "../src/styles.css";

const base = await (await fetch(new URL("./fixtures/review-observability.json", import.meta.url))).json() as ReviewTask;
const task = ref(structuredClone(base));
const choice = ref("completed");
function change(value: string) {
  choice.value = value;
  const next = structuredClone(base);
  next.id = `preview-${value}`;
  if (value === "legacy") {
    next.cross_unit_risk = null;
    next.coordination_plan = null;
    next.followup_results = [];
    next.review_unit_results.forEach((result) => { delete (result as Partial<typeof result>).review_summary; });
  } else if (value === "published") {
    next.issues[0].id = "published-merged";
    next.issues[0].status = "published";
  } else if (value === "candidate") {
    next.status = "verifying_issues";
    next.coordination_plan!.status = "validated";
    next.followup_results[0].validation_status = "pending";
    next.followup_results[0].unit_result!.issues[0].status = "candidate";
    next.issues = [];
  } else if (value === "stale-base") {
    next.pr!.base.sha = "new-base";
  } else if (value === "evidence-legacy") {
    for (const result of next.review_unit_results) for (const reference of result.review_summary.evidence) {
      delete (reference as Partial<typeof reference>).content_hash;
      delete (reference as Partial<typeof reference>).head_sha;
      delete (reference as Partial<typeof reference>).base_sha;
    }
  } else if (value === "skip") {
    next.cross_unit_risk!.decision = "skip";
    next.cross_unit_risk!.execution_status = "not_requested";
    next.cross_unit_risk!.reasons = [{ code: "independent_changes", unit_ids: [], files: [], evidence_ids: [], detail: "本次未触发跨组检查规则；局部覆盖仍需分别判断。" }];
    next.coordination_plan = null;
    next.followup_results = [];
  } else if (value === "uncertain") {
    next.cross_unit_risk!.decision = "uncertain";
    next.cross_unit_risk!.index_status = "unknown";
    next.cross_unit_risk!.execution_status = "unresolved";
    next.coordination_plan!.decision = "uncertain";
    next.coordination_plan!.status = "unresolved";
    next.coordination_plan!.followups = [];
    next.coordination_plan!.unresolved_questions = ["调用方是否保证输入非空？缺少该路径的代码证据。"];
    next.followup_results = [];
  } else if (value === "failed") {
    next.coordination_plan!.status = "failed";
    next.coordination_plan!.reason = "共享预算不足，未发起后续补查调用。";
    next.coordination_plan!.execution_budget.max_model_calls = 0;
    next.coordination_plan!.execution_budget.model_calls = 0;
    next.cross_unit_risk!.execution_status = "failed";
    next.followup_results = [];
  } else if (value === "running" || value === "cancelled") {
    next.status = value === "running" ? "verifying_issues" : "cancelled";
    next.coordination_plan!.status = value === "running" ? "validated" : "cancelled";
    next.cross_unit_risk!.execution_status = value === "running" ? "not_implemented" : "cancelled";
    next.coordination_plan!.runtime_metrics.unknown_calls = 1;
    next.report_markdown = null;
    next.followup_results[0].validation_status = "pending";
    next.followup_results[0].unit_result!.issues.forEach((issue) => { issue.status = "candidate"; });
    next.issues = [];
    next.steps.find((step) => step.name === "cross_unit_coordination")!.status = value === "running" ? "running" : "failed";
  }
  task.value = next;
}

createApp({ setup() { return () => h("div", [
  h("div", { style: "padding:12px 24px;background:#fff3dd;display:flex;gap:18px;align-items:center;flex-wrap:wrap;font-size:13px" }, [
    h("strong", "前端验收 · 合成数据，非真实 PR"),
    h("label", ["场景 ", h("select", { value: choice.value, "aria-label": "验收场景", style: "width:180px;min-height:32px;padding:4px", onChange: (event: Event) => change((event.target as HTMLSelectElement).value) },
      [ ["completed", "补查完成"], ["running", "补查进行中"], ["uncertain", "关系未决"], ["skip", "跳过补查"], ["failed", "预算不足"], ["cancelled", "已取消"], ["legacy", "旧记录缺失"], ["published", "去重后已发布"], ["candidate", "候选待核验"], ["stale-base", "Base快照变更"], ["evidence-legacy", "旧证据缺字段"] ].map(([value, label]) => h("option", { value }, label)))]),
  ]),
  h(ReviewViewer, { task: task.value, report: task.value.report_markdown, statusText: ({ completed_with_warnings: "存在警告", verifying_issues: "验证问题", cancelled: "已取消" } as Record<string, string>)[task.value.status] || task.value.status, taskDuration: "42 秒", cancelling: false, retryingUnitId: null,
    onCancel: () => change("cancelled"), onNewReview: () => change("completed"), onRetry: () => {} }),
]); } }).mount("#preview");
