import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "vite";

const units = [
  {
    id: "ru-1",
    primary_files: ["backend/app/services/posterior.py"],
    related_files: ["backend/app/services/stats.py"],
    changed_symbols: ["test_the_posterior_point_estimate_lies_inside_its_own_interval"],
    grouping_reason: "single_file",
  },
  {
    id: "ru-2",
    primary_files: ["backend/app/services/posterior.py"],
    related_files: [],
    changed_symbols: ["test_the_posterior_point_estimate_lies_inside_its_own_interval"],
    grouping_reason: "large_file_hunk_split",
  },
];

test("变更组标题以文件路径为主，符号降为辅助摘要", async (t) => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const ui = await server.ssrLoadModule("/src/utils/unitPresentation.ts");

    await t.test("短路径保留末两级目录，并跳过空路径", () => {
      assert.equal(ui.shortPath("backend/app/services/posterior.py"), "services/posterior.py");
      assert.equal(ui.shortPath("posterior.py"), "posterior.py");
      assert.equal(ui.shortPath("src/a/posterior.py"), "a/posterior.py");
      assert.equal(ui.shortPath(""), "");
    });

    await t.test("同文件拆分的多个单元通过序号可区分", () => {
      assert.equal(ui.unitTitle(units[0], 0, 2), "services/posterior.py · 1/2");
      assert.equal(ui.unitTitle(units[1], 1, 2), "services/posterior.py · 2/2");
      assert.equal(ui.unitTitle(units[0]), "services/posterior.py");
      assert.equal(ui.unitTitle(units[0], 0, 1), "services/posterior.py");
    });

    await t.test("单个符号可读化并截断，多个符号只报数量", () => {
      assert.equal(
        ui.unitSymbolSummary(units[0]),
        "test the posterior point estimate lies…",
      );
      assert.equal(ui.unitSymbolSummary({ changed_symbols: ["a", "b"] }), "2 个变更符号");
      assert.equal(ui.unitSymbolSummary({ changed_symbols: [] }), "");
    });

    await t.test("分组原因映射中文，未知原因原样保留", () => {
      assert.equal(ui.groupingReasonText("implementation_with_tests"), "实现与测试");
      assert.equal(ui.groupingReasonText("large_file_hunk_split"), "大文件按 Hunk 拆分");
      assert.equal(ui.groupingReasonText("unknown_reason"), "unknown_reason");
      assert.equal(ui.groupingReasonText(""), "");
    });

    await t.test("方案页侧栏不把英文符号当标题", async () => {
      const { default: component } = await server.ssrLoadModule("/src/components/pages/ReviewPlanPage.vue");
      const { createSSRApp } = await import("vue");
      const { renderToString } = await import("@vue/server-renderer");
      const limits = { max_model_calls: 7, max_tokens: 20000, max_cost_microusd: null, timeout_seconds: 30 };
      const plan = { version: "review-plan-v1", depth: "quick", focus: [], scope: { mode: "all", primary_files: [] }, limits };
      const html = await renderToString(createSSRApp(component, {
        preview: { preview_id: "saved", effective_plan: plan, changed_files: [], review_units: units, excluded_files: [], risk_tags: [], warnings: [], included_file_count: 1, changed_file_count: 1 },
        capabilities: { depth_presets: [{ id: "quick", label: "快速初筛", limits }], focus_categories: [], scope_selection_supported: true, limits: { defaults: limits, maxima: limits } },
        prUrl: "https://github.com/o/r/pull/1", model: "fixture", models: [], defaultModel: "fixture", modelsLoading: false, modelsError: null, submitting: false, dirty: false, previewing: false,
      }));
      assert.match(html, /services\/posterior\.py · 1\/2/);
      assert.match(html, /services\/posterior\.py · 2\/2/);
      assert.match(html, /单文件/);
      assert.match(html, /大文件按 Hunk 拆分/);
      assert.doesNotMatch(html, /<summary[^>]*>test_the_posterior_point_estimate/);
    });
  } finally {
    await server.close();
  }
});
