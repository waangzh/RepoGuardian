// Exercise the real App event chain using synthetic API responses only.
import { createApp } from "vue";
import type { ReviewTask } from "../src/types/review";
import type { PreviewRequest } from "../src/types/previewJob";
import "../src/styles.css";
import "../src/styles/prototype.css";
import "../src/styles/workspace.css";

const nativeFetch = window.fetch.bind(window);
const fixtures = await (await nativeFetch("/tests/fixtures/publication-plans.json")).json();
const task: ReviewTask = structuredClone(fixtures.inconclusive.task);
const params = new URLSearchParams(location.search);
const limits = { max_model_calls: 31, max_tokens: 90000, max_cost_microusd: null, timeout_seconds: 600 };
task.model = "saved-model";
task.confirmed_plan = { version: "review-plan-v1", depth: "deep", focus: ["security", "compatibility"],
  scope: { mode: "selected", primary_files: ["old.py"] }, limits };
task.snapshot = { head_sha: "a".repeat(40), effective_base_sha: "c".repeat(40), declared_base_sha: "c".repeat(40) };
task.plan_hash = "old-confirmation";
task.mode = "review";
task.generate_patches = true;
task.validation_backend = "none";
if (params.has("legacy")) { task.confirmed_plan = null; task.model = null; task.engine = "legacy_units"; }
if (params.has("invalid")) task.pr_url = "https://evil.test/o/r/pull/1";
const requests: { path: string; method: string; body: unknown }[] = [];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json" },
});
const head = "d".repeat(40);
let quickRequests = 0;
window.fetch = async (input, init) => {
  const path = typeof input === "string" ? input : input instanceof URL ? input.pathname : input.url;
  if (!path.startsWith("/api/")) return nativeFetch(input, init);
  const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
  requests.push({ path, method: init?.method || "GET", body });
  document.querySelector("#request-log")!.textContent = JSON.stringify(requests, null, 2);
  if (path === "/api/system/models") return json({ models: [{ id: "saved-model" }], default_model: "default-model" });
  if (path === "/api/system/review-capabilities") return json({ plan_version: "review-plan-v1",
    depth_presets: ["quick", "standard", "deep"].map(id => ({ id, label: id, limits })),
    focus_categories: ["correctness", "security", "performance", "compatibility"], scope_selection_supported: true,
    limits: { defaults: limits, maxima: limits } });
  if (path.startsWith("/api/reviews?")) return json({ items: [task], total: 1, page: 1, page_size: 3 });
  if (path === `/api/reviews/${task.id}`) return json(task);
  if (path.endsWith("/publication-summary")) return json(fixtures.inconclusive.summary);
  if (path.endsWith("/publication-plan")) return json(fixtures.inconclusive.plan);
  if (path === "/api/reviews/preview/quick") {
    quickRequests++;
    if (params.has("slow")) await new Promise(resolve => setTimeout(resolve, 800));
    if (params.has("fail") && quickRequests === 1) return json({ detail: "合成概览读取失败" }, 503);
    return json({ pr_url: task.pr_url, repository: "o/r", number: 1, title: "当前 PR 的最新提交",
      head_sha: head, declared_base_sha: "c".repeat(40), files_complete: true,
      files: [{ path: "new-file.py", status: "added", additions: 2, deletions: 0 }] });
  }
  if (path === "/api/reviews/preview-jobs") {
    const request = body.request as PreviewRequest;
    return json({ id: body.id, request, status: "completed", stages: [], cache_hit: false,
      created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
      result: { preview_id: "new-preview", plan_hash: "new-confirmation",
        snapshot: { head_sha: head, effective_base_sha: "c".repeat(40), declared_base_sha: "c".repeat(40) },
        effective_plan: request.plan || { version: "review-plan-v1", depth: "standard",
          focus: ["correctness"], scope: { mode: "all", primary_files: [] }, limits },
        mode: "review", changed_file_count: 1, included_file_count: 1,
        changed_files: [{ file_path: "new-file.py", additions: 2, deletions: 0 }], review_units: [],
        excluded_files: [], risk_tags: [], warnings: [], estimated_tokens: 0, max_model_calls: 31,
        validation_backend: { name: "none", available: false } } });
  }
  return json({ detail: "合成验收禁止未配置的接口和真实任务创建" }, 403);
};
localStorage.removeItem("rg-preview-job");
const { default: App } = await import("../src/App.vue");
createApp(App).mount("#app");
