import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "vite";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";

const HISTORICAL = {
  discussion_id: "inline:1",
  path: "src/auth.py",
  status: "still_present",
  severity: "high",
  priority: "fix_before_merge",
  affected_behavior: "匿名请求仍可读取受保护数据",
  recommendation: "在读取前校验身份",
  reason: "固定源码仍保留原始触发路径",
  evidence_ids: ["history-e1"],
  discussion_url: "https://github.com/o/r/pull/1#discussion_r1",
};

function withHistory(summary, overrides = {}) {
  const value = structuredClone(summary);
  value.outcome.historical_discussion = { findings: [structuredClone(HISTORICAL)] };
  value.digest.merge_decision = {
    status: "fix_blockers", label: "发现 1 项高优先级问题（含历史复核），建议修复后复审",
    blocking_finding_keys: [], nonblocking_finding_keys: [],
    blocking_historical_keys: ["inline:1"], nonblocking_historical_keys: [],
    blocking_independent_count: 1, deduplicated_historical_keys: [],
    unresolved_reasons: [], limits: [],
    ...overrides,
  };
  return value;
}

test("概览数量按来源拆分，且每个数量都能在问题页找到全部条目", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: Results } = await server.ssrLoadModule("/src/components/review/OverviewResults.vue");
    const { default: Findings } = await server.ssrLoadModule("/src/components/pages/ReviewFindingsPage.vue");
    const { overviewFacts } = await server.ssrLoadModule("/src/utils/overviewPresentation.ts");
    const fixtures = JSON.parse(await readFile(new URL("./fixtures/publication-plans.json", import.meta.url), "utf8"));
    const render = (component, props) => renderToString(createSSRApp(component, props));

    // 只有历史复核风险：概览总数为 1，来源拆分为“历史复核 1”，问题页能列出同一条。
    const historyOnly = withHistory(fixtures.clean.summary);
    const facts = overviewFacts(historyOnly, null);
    assert.equal(facts.blockers, 1);
    assert.deepEqual([facts.blockerMetric.current, facts.blockerMetric.historical, facts.blockerMetric.verifiable], [0, 1, true]);
    const overview = await render(Results, { plan: historyOnly, runtime: null, findings: [] });
    assert.match(overview, /高优先级问题<\/span>\s*<strong[^>]*>1<\/strong>/);
    assert.match(overview, /历史复核 <strong>1<\/strong> 项/);
    assert.doesNotMatch(overview, /本轮 Diff <strong>/, "本轮为 0 时不显示空来源");

    const findingsHtml = await render(Findings, { task: fixtures.clean.task, findings: [], focusKey: "",
      publicationFindings: [], summary: historyOnly, initialFilter: "blockers", initialSource: "historical" });
    assert.ok(findingsHtml.includes("匿名请求仍可读取受保护数据"));
    assert.ok(findingsHtml.includes("历史复核已核验"));
    assert.ok(findingsHtml.includes("不计入本轮 Diff 新增"));
    assert.ok(findingsHtml.includes("历史讨论复核证据"));
    assert.ok(!findingsHtml.includes("人工处理"), "历史风险不提供本轮的人工处置入口");

    // 本轮 + 历史各 1 项：总数 2，两份来源各 1，问题页按来源筛选后各只有一条。
    const mixed = withHistory(fixtures.clean.summary, {
      blocking_finding_keys: ["current-1"], blocking_independent_count: 2,
    });
    const mixedFacts = overviewFacts(mixed, null);
    assert.equal(mixedFacts.blockers, 2);
    assert.deepEqual([mixedFacts.blockerMetric.current, mixedFacts.blockerMetric.historical], [1, 1]);
    const row = { key: "current-1", title: "本轮新增缺陷", status: "confirmed", group: "confirmed",
      filePath: "src/b.py", line: 3, issue: null, affectedBehavior: "", failureScenario: "",
      recommendation: "", reason: "", rawReason: "" };
    for (const [source, title] of [["current", "本轮新增缺陷"], ["historical", "匿名请求仍可读取受保护数据"]]) {
      const html = await render(Findings, { task: fixtures.clean.task, findings: [row], focusKey: "",
        publicationFindings: [], summary: mixed, initialFilter: "blockers", initialSource: source });
      assert.ok(html.includes(title), `${source}: 应包含 ${title}`);
      assert.ok(!html.includes(source === "current" ? "匿名请求仍可读取受保护数据" : "本轮新增缺陷"), `${source}: 不应混入其他来源`);
    }

    // 同一缺陷被严格去重：总数仍为 1，且不再单独展示历史条目，数量与列表一致。
    const merged = withHistory(fixtures.clean.summary, {
      blocking_finding_keys: ["current-1"], blocking_independent_count: 1, deduplicated_historical_keys: ["inline:1"],
    });
    const mergedFacts = overviewFacts(merged, null);
    assert.equal(mergedFacts.blockers, 1);
    assert.deepEqual([mergedFacts.blockerMetric.current, mergedFacts.blockerMetric.historical, mergedFacts.blockerMetric.merged], [1, 0, 1]);
    const mergedHtml = await render(Results, { plan: merged, runtime: null, findings: [] });
    assert.match(mergedHtml, /指向同一缺陷的历史讨论已合并/);
    assert.doesNotMatch(mergedHtml, /历史复核 <strong>/);
  } finally { await server.close(); }
});

test("历史明细缺失时仍保留占位条目，数量不会大于可核对条目", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: Findings } = await server.ssrLoadModule("/src/components/pages/ReviewFindingsPage.vue");
    const { historicalFindingRows } = await server.ssrLoadModule("/src/utils/findingWorkbench.ts");
    const fixtures = JSON.parse(await readFile(new URL("./fixtures/publication-plans.json", import.meta.url), "utf8"));
    const plan = withHistory(fixtures.clean.summary);
    // 旧记录只有决策键，没有任何 findings / verified_defects 明细。
    plan.outcome.historical_discussion = {};
    const rows = historicalFindingRows(plan);
    assert.equal(rows.length, 1);
    assert.equal(rows[0].key, "inline:1");
    const html = await renderToString(createSSRApp(Findings, { task: fixtures.clean.task, findings: [], focusKey: "",
      publicationFindings: [], summary: plan, initialFilter: "blockers", initialSource: "historical" }));
    assert.ok(html.includes("历史复核记录缺少可展示的明细"));
    assert.ok(html.includes("查看完整报告或原讨论"));
  } finally { await server.close(); }
});

test("来源数量与总数无法对账时不展示拆分", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: Results } = await server.ssrLoadModule("/src/components/review/OverviewResults.vue");
    const { overviewFacts } = await server.ssrLoadModule("/src/utils/overviewPresentation.ts");
    const fixtures = JSON.parse(await readFile(new URL("./fixtures/publication-plans.json", import.meta.url), "utf8"));
    // 旧计划缺少去重标注：独立计数小于来源之和，拆分会对不上，只能显示总数。
    const legacy = withHistory(fixtures.clean.summary, {
      blocking_finding_keys: ["current-1"], blocking_independent_count: 1,
    });
    const facts = overviewFacts(legacy, null);
    assert.equal(facts.blockers, 1);
    assert.equal(facts.blockerMetric.verifiable, false);
    const html = await renderToString(createSSRApp(Results, { plan: legacy, runtime: null, findings: [] }));
    assert.match(html, /高优先级问题<\/span>\s*<strong[^>]*>1<\/strong>/);
    assert.doesNotMatch(html, /ov-source/);
  } finally { await server.close(); }
});
