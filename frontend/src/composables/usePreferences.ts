import { ref, watch } from "vue";
import type { ReviewCapabilities, ReviewPlanOptions } from "../types/reviewPlan";
function saved(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}
const theme = ref(saved("rg-theme") === "dark" ? "dark" : "light");
const density = ref(saved("rg-density") === "compact" ? "compact" : "comfortable");
type DiscussionPreference = "server" | "enabled" | "disabled";
const savedDiscussions = saved("rg-default-pr-discussions");
const defaultPrDiscussions = ref<DiscussionPreference>(
  savedDiscussions === "enabled" || savedDiscussions === "disabled" ? savedDiscussions : "server",
);
watch(defaultPrDiscussions, next => {
  try { localStorage.setItem("rg-default-pr-discussions", next); } catch { /* Retain in-memory preferences. */ }
});
type DefaultReviewDepth = ReviewPlanOptions["depth"] | "server";
const savedDepth = saved("rg-default-review-depth");
const defaultReviewDepth = ref<DefaultReviewDepth>(
  savedDepth === "quick" || savedDepth === "standard" || savedDepth === "deep" ? savedDepth : "server",
);
watch(defaultReviewDepth, next => {
  try { localStorage.setItem("rg-default-review-depth", next); } catch { /* Retain in-memory preferences. */ }
});
for (const [key, value] of [["theme", theme], ["density", density]] as const) {
  watch(value, next => {
    document.documentElement.dataset[key] = next;
    try { localStorage.setItem(`rg-${key}`, next); } catch { /* Retain in-memory preferences. */ }
  }, { immediate: true });
}
export function usePreferences() {
  return { theme, density, defaultReviewDepth, defaultPrDiscussions, initialReviewPlan,
    toggleTheme: () => { theme.value = theme.value === "light" ? "dark" : "light"; } };
}

function initialReviewPlan(capabilities: ReviewCapabilities | null): ReviewPlanOptions | undefined {
  if (defaultReviewDepth.value === "server" && defaultPrDiscussions.value === "server") return undefined;
  const depth = defaultReviewDepth.value === "server" ? "standard" : defaultReviewDepth.value;
  if (!capabilities?.depth_presets.some(preset => preset.id === depth)) {
    throw new Error("默认审查深度的能力声明不可用，请刷新页面或在系统设置中重新选择。");
  }
  return {
    version: "review-plan-v1",
    depth,
    ...(defaultPrDiscussions.value === "server" ? {} : {
      include_pr_discussions: defaultPrDiscussions.value === "enabled",
    }),
    focus: [...capabilities.focus_categories],
    scope: { mode: "all", primary_files: [] },
    // 由服务端按所选深度归一化限额，不将偏好当作预算策略。
    limits: { max_model_calls: null, max_tokens: null, max_cost_microusd: null, timeout_seconds: null },
  };
}
