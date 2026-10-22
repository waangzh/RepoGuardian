import { readonly, ref, watch } from "vue";
import type { ReviewCapabilities, ReviewLimits, ReviewPlanOptions } from "../types/reviewPlan";
function saved(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}
const theme = ref(saved("rg-theme") === "dark" ? "dark" : "light");
const density = ref(saved("rg-density") === "compact" ? "compact" : "comfortable");
type DiscussionPreference = "enabled" | "disabled";
const savedDiscussions = saved("rg-default-pr-discussions");
const defaultPrDiscussions = ref<DiscussionPreference>(
  savedDiscussions === "disabled" ? "disabled" : "enabled",
);
watch(defaultPrDiscussions, next => {
  try { localStorage.setItem("rg-default-pr-discussions", next); } catch { /* Retain in-memory preferences. */ }
});
type DefaultReviewDepth = ReviewPlanOptions["depth"];
const savedDepth = saved("rg-default-review-depth");
const defaultReviewDepth = ref<DefaultReviewDepth>(
  savedDepth === "quick" || savedDepth === "standard" || savedDepth === "deep" ? savedDepth : "standard",
);
watch(defaultReviewDepth, next => {
  try { localStorage.setItem("rg-default-review-depth", next); } catch { /* Retain in-memory preferences. */ }
});
export const reviewDepthResources = [
  { key: "max_model_calls", label: "模型调用", unit: "次", min: 0 },
  { key: "max_tokens", label: "Token", unit: "tokens", min: 0 },
  { key: "timeout_seconds", label: "运行时间", unit: "秒", min: 1 },
] as const;
type DepthLimits = Partial<Record<typeof reviewDepthResources[number]["key"], number>>;
type DepthOverrides = Partial<Record<DefaultReviewDepth, DepthLimits>>;
const depthLimitsKey = "rg-review-depth-limits-v1";
function readDepthOverrides(): DepthOverrides {
  try {
    const stored = JSON.parse(saved(depthLimitsKey) ?? "{}");
    if (!stored || stored.version !== 1 || !stored.depths || typeof stored.depths !== "object") return {};
    const result: DepthOverrides = {};
    for (const depth of ["quick", "standard", "deep"] as const) {
      const limits: DepthLimits = {};
      for (const resource of reviewDepthResources) {
        const value: unknown = stored.depths[depth]?.[resource.key];
        if (typeof value === "number" && Number.isSafeInteger(value) && value >= resource.min) {
          limits[resource.key] = value;
        }
      }
      if (Object.keys(limits).length) result[depth] = limits;
    }
    return result;
  } catch { return {}; }
}
const depthOverrides = ref<DepthOverrides>(readDepthOverrides());
const depthLimitsPersistent = ref(true);
watch(depthOverrides, next => {
  try {
    localStorage.setItem(depthLimitsKey, JSON.stringify({ version: 1, depths: next }));
    depthLimitsPersistent.value = true;
  } catch { depthLimitsPersistent.value = false; }
}, { flush: "sync" });

function depthLimitOverrides(depth: DefaultReviewDepth): DepthLimits {
  return { ...depthOverrides.value[depth] };
}
function validateDepthLimits(limits: DepthLimits, capabilities: ReviewCapabilities) {
  for (const resource of reviewDepthResources) {
    const value = limits[resource.key];
    if (value === undefined) continue;
    if (typeof value !== "number" || !Number.isSafeInteger(value) || value < resource.min) {
      throw new Error(`${resource.label}须为不小于 ${resource.min} 的整数。`);
    }
    const maximum = capabilities.limits.maxima[resource.key];
    if (maximum !== null && value > maximum) {
      throw new Error(`${resource.label}不能超过当前服务端上限 ${maximum.toLocaleString("zh-CN")}。请在系统设置中修改此档配置或恢复预设。`);
    }
  }
}
function saveDepthLimits(depth: DefaultReviewDepth, limits: DepthLimits, capabilities: ReviewCapabilities) {
  if (!capabilities.depth_presets.some(preset => preset.id === depth)) {
    throw new Error("审查深度的能力声明不可用，请刷新页面重试。");
  }
  validateDepthLimits(limits, capabilities);
  const filtered: DepthLimits = {};
  for (const { key } of reviewDepthResources) {
    if (limits[key] !== undefined) filtered[key] = limits[key];
  }
  depthOverrides.value = { ...depthOverrides.value, [depth]: filtered };
}
function resetDepthLimits(depth: DefaultReviewDepth) {
  const next = { ...depthOverrides.value };
  delete next[depth];
  depthOverrides.value = next;
}
function reviewDepthPresets(capabilities: ReviewCapabilities) {
  return capabilities.depth_presets.map(preset => ({
    ...preset,
    customized: Object.keys(depthLimitOverrides(preset.id)).length > 0,
    limits: { ...preset.limits, ...depthLimitOverrides(preset.id) },
  }));
}
function reviewDepthLimits(depth: DefaultReviewDepth, capabilities: ReviewCapabilities, usePreset = true): ReviewLimits {
  const preset = capabilities.depth_presets.find(item => item.id === depth);
  if (!preset) throw new Error("默认审查深度的能力声明不可用，请刷新页面或在系统设置中重新选择。");
  const overrides = depthLimitOverrides(depth);
  validateDepthLimits(overrides, capabilities);
  return {
    ...(usePreset ? preset.limits : { max_model_calls: null, max_tokens: null, max_cost_microusd: null, timeout_seconds: null }),
    ...overrides,
  };
}
for (const [key, value] of [["theme", theme], ["density", density]] as const) {
  watch(value, next => {
    if (typeof document !== "undefined") document.documentElement.dataset[key] = next;
    try { localStorage.setItem(`rg-${key}`, next); } catch { /* Retain in-memory preferences. */ }
  }, { immediate: true });
}
export function usePreferences() {
  return { theme, density, defaultReviewDepth, defaultPrDiscussions, initialReviewPlan,
    depthLimitOverrides, saveDepthLimits, resetDepthLimits, reviewDepthPresets, reviewDepthLimits,
    depthLimitsPersistent: readonly(depthLimitsPersistent),
    toggleTheme: () => { theme.value = theme.value === "light" ? "dark" : "light"; } };
}

function initialReviewPlan(capabilities: ReviewCapabilities | null): ReviewPlanOptions {
  const depth = defaultReviewDepth.value;
  if (!capabilities?.depth_presets.some(preset => preset.id === depth)) {
    throw new Error("默认审查深度的能力声明不可用，请刷新页面或在系统设置中重新选择。");
  }
  return {
    version: "review-plan-v1",
    depth,
    include_pr_discussions: defaultPrDiscussions.value === "enabled" && capabilities.pr_discussions_available === true,
    focus: [...capabilities.focus_categories],
    scope: { mode: "all", primary_files: [] },
    // 未自定义的字段仍由服务端按所选深度归一化。
    limits: reviewDepthLimits(depth, capabilities, false),
  };
}
