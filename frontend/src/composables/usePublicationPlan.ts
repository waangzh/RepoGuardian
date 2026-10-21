import { ref, watch } from "vue";
import { getPublicationPlan, getPublicationSummary } from "../api/client";
import type { ReviewTask } from "../types/review";
import type { PublicationPlan, ReviewSummary } from "../types/publication";

/**
 * 结果概览只读取轻量摘要；完整计划（逐条正文、目标明细、执行追踪）
 * 由报告、发布或证据页按需加载，避免普通页面解析审计数据。
 */
export function usePublicationPlan(task: () => ReviewTask,
  loadSummary = getPublicationSummary, loadPlan = getPublicationPlan, options: { autoPlan?: boolean } = {}) {
  const summary = ref<ReviewSummary | null>(null);
  const plan = ref<PublicationPlan | null>(null);
  const error = ref("");
  const loading = ref(false);
  const detailLoading = ref(false);
  const revision = ref(0);
  const detailRevision = ref(0);
  const planRequested = ref(false);
  const terminal = () => ["completed", "completed_with_warnings", "failed", "cancelled"].includes(task().status);

  watch([() => task().id, () => task().status, () => task().task_runtime_report, () => task().run_manifest, revision],
    async (_, __, onCleanup) => {
      const controller = new AbortController();
      onCleanup(() => controller.abort());
      summary.value = null;
      error.value = "";
      loading.value = false;
      if (!terminal()) return;
      const id = task().id;
      loading.value = true;
      try {
        const result = await loadSummary(id, controller.signal);
        if (!controller.signal.aborted && task().id === id) {
          if (result.review_id !== id || result.schema_version !== "review-summary-v1") throw new Error("审查摘要与任务不匹配");
          summary.value = result;
        }
      } catch {
        if (!controller.signal.aborted) error.value = "审查摘要加载失败，当前无法读取完整审查结论。请点击“重试加载摘要”；原始运行记录仍可查看。";
      } finally {
        if (!controller.signal.aborted) loading.value = false;
      }
    }, { immediate: true });

  watch([() => task().id, () => task().status, () => task().task_runtime_report, () => task().run_manifest, detailRevision],
    async (_, __, onCleanup) => {
      const controller = new AbortController();
      onCleanup(() => controller.abort());
      const snapshot = task().run_manifest;
      const sameSnapshot = plan.value?.review_id === task().id
        && plan.value?.snapshot?.head_sha === snapshot?.head_sha
        && plan.value?.snapshot?.effective_base_sha === snapshot?.effective_base_sha;
      if (!terminal() || !sameSnapshot) plan.value = null;
      detailLoading.value = false;
      if (!terminal() || (!options.autoPlan && !planRequested.value)) return;
      const id = task().id;
      detailLoading.value = true;
      try {
        const result = await loadPlan(id, controller.signal);
        if (!controller.signal.aborted && task().id === id) {
          if (result.review_id !== id || result.schema_version !== "review-publication-v1") throw new Error("完整报告与任务不匹配");
          plan.value = result;
        }
      } catch {
        if (!controller.signal.aborted) error.value = error.value || "完整报告加载失败，请重试。";
      } finally {
        if (!controller.signal.aborted) detailLoading.value = false;
      }
    }, { immediate: true });

  return { summary, plan, error, loading, detailLoading,
    reload: () => { revision.value += 1; if (planRequested.value) detailRevision.value += 1; },
    ensurePlan: () => {
      planRequested.value = true;
      if (!plan.value && !detailLoading.value) detailRevision.value += 1;
    },
    reloadPlan: () => { planRequested.value = true; detailRevision.value += 1; } };
}
