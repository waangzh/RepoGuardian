import { computed } from "vue";
import type { ReviewTask } from "../types/review";
import { runtimeReport, runtimeFindingRows, taskRuntimeSummary, runtimeWarnings, type RuntimeFindingRow } from "../utils/taskRuntimePresentation";

export function useReviewWorkspace(task: () => ReviewTask) {
  const runtime = computed(() => runtimeReport(task()));
  const summary = computed(() => runtime.value ? taskRuntimeSummary(runtime.value) : null);
  const findings = computed<RuntimeFindingRow[]>(() => {
    if (runtime.value) return runtimeFindingRows(runtime.value, task().issues);
    // A missing current-runtime report is unknown; never promote legacy Issues into its results.
    if (task().engine === "task_review_v1") return [];
    return task().issues.map(issue => ({
      key: issue.id, status: issue.status,
      group: ["confirmed", "published"].includes(issue.status) ? "confirmed"
        : issue.status === "dismissed" ? "dismissed" : ["candidate", "unresolved", "needs_human"].includes(issue.status) ? "unresolved" : "unknown",
      title: issue.title, failureScenario: issue.failure_scenario, affectedBehavior: issue.affected_behavior,
      recommendation: issue.recommendation, reason: issue.unresolved_reason || "", rawReason: issue.unresolved_reason || "",
      filePath: issue.primary_evidence.file_path, line: issue.primary_evidence.resolved_start_line || null, issue,
    }));
  });
  const warnings = computed(() => [...new Set([...(task().warnings || []), ...(runtime.value ? runtimeWarnings(runtime.value) : ["当前记录没有任务级范围报告；完整性未知。"])])]);
  const terminal = computed(() => ["completed", "completed_with_warnings", "failed", "cancelled", "waiting_for_human"].includes(task().status));
  return { runtime, summary, findings, warnings, terminal };
}
