import { ref, watch } from "vue";
import { getPublicationPlan } from "../api/client";
import type { ReviewTask } from "../types/review";
import type { PublicationPlan } from "../types/publication";

export function usePublicationPlan(task: () => ReviewTask, load = getPublicationPlan) {
  const plan = ref<PublicationPlan | null>(null);
  const error = ref("");
  const loading = ref(false);
  const revision = ref(0);
  watch([() => task().id, () => task().status, () => task().task_runtime_report, () => task().run_manifest, revision],
    async (_, __, onCleanup) => {
      const controller = new AbortController();
      onCleanup(() => controller.abort());
      plan.value = null;
      error.value = "";
      loading.value = false;
      if (!["completed", "completed_with_warnings", "failed", "cancelled"].includes(task().status)) return;
      const id = task().id;
      loading.value = true;
      try {
        const result = await load(id, controller.signal);
        if (!controller.signal.aborted && task().id === id) {
          if (result.review_id !== id || result.schema_version !== "review-publication-v1") throw new Error("发布计划与任务不匹配");
          plan.value = result;
        }
      } catch {
        if (!controller.signal.aborted) error.value = "发布计划加载失败，请重试；原始审计仍可查看。";
      } finally {
        if (!controller.signal.aborted) loading.value = false;
      }
    }, { immediate: true });
  return { plan, error, loading, reload: () => { revision.value += 1; } };
}
