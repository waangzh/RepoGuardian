// 仅在独立测试页使用合成接口，不连接真实审查服务。
import { createApp } from "vue";
import type { ReviewTask } from "../src/types/review";
import "../src/styles.css";
import "../src/styles/prototype.css";
import "../src/styles/workspace.css";

const nativeFetch = window.fetch.bind(window);
const fixtures = await (await nativeFetch("/tests/fixtures/publication-plans.json")).json();
const fixture = fixtures.clean;
const params = new URLSearchParams(location.search);
const records: ReviewTask[] = Array.from({ length: 13 }, (_, index) => ({
  ...structuredClone(fixture.task),
  id: index === 0 ? fixture.task.id : `saved-review-${index}`,
  status: index === 1 ? "reviewing" : "completed",
  pr: { owner: "fixture", repo: "history-delete", number: index + 1, title: `合成删除验收 ${index}`,
    html_url: `https://github.com/fixture/history-delete/pull/${index + 1}`, clone_url: "", body: "",
    base: { ref: "main", sha: "c".repeat(40), repo_clone_url: "" },
    head: { ref: "change", sha: "a".repeat(40), repo_clone_url: "" } },
}));
const limits = { max_model_calls: 31, max_tokens: 90000, max_cost_microusd: null, timeout_seconds: 600 };
const requests: { path: string; method: string }[] = [];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json" },
});
let failDelete = params.has("failure");
window.fetch = async (input, init) => {
  const path = typeof input === "string" ? input : input instanceof URL ? input.pathname : input.url;
  if (!path.startsWith("/api/")) return nativeFetch(input, init);
  const method = init?.method || "GET";
  requests.push({ path, method });
  document.querySelector("#request-log")!.textContent = JSON.stringify(requests);
  if (path === "/api/system/models") return json({ models: [{ id: "fixture" }], default_model: "fixture" });
  if (path === "/api/system/review-capabilities") return json({ plan_version: "review-plan-v1",
    depth_presets: ["quick", "standard", "deep"].map(id => ({ id, label: id, limits })),
    focus_categories: ["correctness", "security"], scope_selection_supported: true,
    limits: { defaults: limits, maxima: limits } });
  if (path.startsWith("/api/reviews?")) {
    const query = new URL(path, location.origin).searchParams;
    const page = Number(query.get("page") || 1);
    const pageSize = Number(query.get("page_size") || 12);
    const search = query.get("q") || "";
    const status = query.get("status");
    const filtered = records.filter(task => (!status || task.status === status)
      && (!search || `${task.id} ${task.pr?.title}`.includes(search)));
    return json({ total: filtered.length, page, page_size: pageSize,
      items: filtered.slice((page - 1) * pageSize, page * pageSize).map(task => ({
        ...task, history_result: { outcome: fixture.plan.outcome, merge_decision: fixture.plan.digest.merge_decision },
      })) });
  }
  const identity = path.match(/^\/api\/reviews\/([^/]+)$/)?.[1];
  if (identity) {
    const index = records.findIndex(task => task.id === identity);
    if (method === "DELETE") {
      await new Promise(resolve => setTimeout(resolve, params.has("slow") ? 1200 : 150));
      if (failDelete) { failDelete = false; return json({ detail: {
        code: "review_publication_pending", message: "发布结果尚未确认，暂时不能删除。",
      } }, 409); }
      if (index !== -1 && records[index]!.status === "reviewing") return json({ detail: {
        code: "review_not_terminal", message: "审查尚未结束。",
      } }, 409);
      if (index !== -1) records.splice(index, 1);
      return new Response(null, { status: 204 });
    }
    return index === -1 ? json({ detail: "记录不存在" }, 404) : json(records[index]);
  }
  if (path.endsWith("/report")) return new Response(fixture.plan.user_report_markdown);
  if (path.endsWith("/publication-summary")) return json(fixture.summary);
  if (path.endsWith("/publication-plan")) return json(fixture.plan);
  return json({ detail: "隔离验收禁止未配置的接口或真实写入" }, 403);
};
localStorage.removeItem("rg-preview-job");
const { default: App } = await import("../src/App.vue");
createApp(App).mount("#app");
