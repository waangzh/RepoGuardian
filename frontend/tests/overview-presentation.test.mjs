import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "vite";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";

test("概览保留未知计数、真实限制及独立验证边界", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { overviewFacts } = await server.ssrLoadModule("/src/utils/overviewPresentation.ts");
    const fixtures = JSON.parse(await readFile(new URL("./fixtures/publication-plans.json", import.meta.url), "utf8"));
    const unknown = overviewFacts(null, null);
    assert.equal(unknown.blockers, null);
    assert.equal(unknown.unresolved, null);
    assert.equal(unknown.examined, null);
    assert.equal(unknown.boundaries[0].tone, "neutral");
    assert.equal(unknown.boundaries[2].value, "未知");
    const incomplete = structuredClone(fixtures.inconclusive.summary);
    incomplete.outcome.examined_goals = 2;
    incomplete.outcome.total_goals = 2;
    incomplete.outcome.dynamic_validation = "not_executed";
    incomplete.outcome.reason_codes = ["investigation_incomplete"];
    incomplete.outcome.incomplete_investigations = 1;
    incomplete.digest.merge_decision.limits = ["真实限制：关联返回值消费路径未核验"];
    const facts = overviewFacts(incomplete, null);
    assert.equal(facts.blockers, 0);
    assert.equal(facts.unresolved, incomplete.outcome.unresolved_count);
    assert.equal(facts.boundaries[1].tone, "warning");
    assert.ok(facts.limitations.includes("真实限制：关联返回值消费路径未核验"));
    assert.equal(facts.boundaries[2].value, "未执行");
    assert.equal(facts.boundaries[2].tone, "neutral");
    for (const validation of ["passed", "failed", "independent_results", "unknown"]) {
      incomplete.outcome.dynamic_validation = validation;
      const row = overviewFacts(incomplete, null).boundaries[2];
      assert.equal(row.tone, validation === "passed" ? "success" : validation === "failed" ? "danger" : "neutral");
    }
    incomplete.outcome.review_completeness = "unknown";
    incomplete.outcome.reason_codes = [];
    assert.equal(overviewFacts(incomplete, null).boundaries[0].tone, "neutral");
    delete incomplete.digest.merge_decision.blocking_finding_keys;
    incomplete.digest.merge_decision.status = "fix_blockers";
    assert.equal(overviewFacts(incomplete, null).blockers, null, "保守阻断情景不可伪造数量");
    incomplete.outcome.examined_goals = 1;
    assert.equal(overviewFacts(incomplete, null).boundaries[0].tone, "warning");
    const runtime = { scope: { examined: 1, total: 3 } };
    assert.equal(overviewFacts(null, runtime).examined, 1);
    assert.equal(overviewFacts(null, runtime).total, 3);
    assert.equal(overviewFacts(null, runtime).unresolved, null);
    const clean = structuredClone(fixtures.clean.summary);
    clean.digest.merge_decision.limits = ["只做静态审查，未执行 CI"];
    assert.equal(overviewFacts(clean, null).boundaries[1].value, "存在已知限制", "静态验证边界不推断为证据缺口");
    clean.outcome.reason_codes = ["scope_incomplete"];
    assert.equal(overviewFacts(clean, null).boundaries[0].tone, "warning", "保留后端显式范围缺口");
    const runtimeFacts = overviewFacts(fixtures.runtime_limits.summary, null);
    assert.equal(runtimeFacts.boundaries[0].value, "1/1 项已检查");
    assert.equal(runtimeFacts.boundaries[0].tone, "success");
    assert.equal(runtimeFacts.boundaries[1].value, "运行或计量记录受限");
    assert.equal(runtimeFacts.boundaries[1].tone, "warning");
    const investigation = structuredClone(fixtures.runtime_limits.summary);
    investigation.outcome.reason_codes = ["investigation_incomplete"];
    investigation.outcome.review_completeness = "partial";
    investigation.outcome.incomplete_investigations = 1;
    const separate = overviewFacts(investigation, null);
    assert.equal(separate.boundaries[0].description.includes("仍有约定目标未完整检查"), false);
    assert.equal(separate.boundaries[1].value, "1 项调查未完成");
  } finally { await server.close(); }
});

test("概览正常状态短显，具体限制仅在可信边界常驻展示", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { overviewFacts, overviewConclusionDescription, overviewNextSteps } = await server.ssrLoadModule("/src/utils/overviewPresentation.ts");
    const { reviewSituation } = await server.ssrLoadModule("/src/utils/reviewSituation.ts");
    const { default: Overview } = await server.ssrLoadModule("/src/components/pages/ReviewOverviewPage.vue");
    const fixtures = JSON.parse(await readFile(new URL("./fixtures/publication-plans.json", import.meta.url), "utf8"));
    const fixture = structuredClone(fixtures.clean);
    fixture.summary.digest.merge_decision.limits = [];
    fixture.summary.digest.merge_decision.unresolved_reasons = [];
    fixture.summary.outcome.dynamic_validation = "not_executed";
    const facts = overviewFacts(fixture.summary, null);
    assert.equal(facts.boundaries[0].description, "");
    assert.equal(facts.boundaries[1].description, "");
    assert.equal(facts.boundaries[2].description, "");
    assert.equal(facts.boundaries[2].tone, "neutral");
    assert.equal(facts.boundaries[2].value, "未执行");
    assert.equal(overviewConclusionDescription(null), "");
    for (const name of ["clean", "findings", "inconclusive", "failed", "cancelled", "runtime_limits"]) {
      const situation = reviewSituation(fixtures[name].summary);
      assert.ok(overviewConclusionDescription(situation));
      assert.doesNotMatch(overviewConclusionDescription(situation), /请点击/);
      assert.ok(overviewNextSteps(situation).steps.length > 0);
    }
    const props = { task: fixture.task, report: null, summaryPlan: fixture.summary, summary: null, findings: [], warnings: [], terminal: true };
    const html = await renderToString(createSSRApp(Overview, props));
    assert.match(html, /<details class="ov-step-details"><summary>查看处理步骤<\/summary>/);
    assert.doesNotMatch(html, /<details[^>]*class="ov-step-details"[^>]*\sopen(?:[\s=>])/);
    assert.equal((html.match(/查看完整报告/g) || []).length, 1);
    assert.doesNotMatch(html, /阅读详细审查报告/);
    assert.doesNotMatch(html, /ov-note|ov-verdict__limitation/);
    assert.doesNotMatch(html, /<dt>动态验证<\/dt>/, "快照不再重复验证状态");
    fixture.summary.digest.merge_decision.limits = ["具体限制：下游消费路径尚未核验"];
    props.warnings = ["具体限制：下游消费路径尚未核验", "独立提示：模型用量尚不完整", "独立提示：模型用量尚不完整"];
    const limited = await renderToString(createSSRApp(Overview, props));
    assert.equal((limited.match(/具体限制：下游消费路径尚未核验/g) || []).length, 1);
    assert.equal((limited.match(/独立提示：模型用量尚不完整/g) || []).length, 1);
    const stale = await renderToString(createSSRApp(Overview, { ...props, stale: true }));
    assert.match(stale, /以上数量来自旧快照，不适用于最新 Head/);
    assert.match(stale, /复制历史审查记录/);
    fixture.summary.outcome.dynamic_validation = "unknown";
    assert.match(overviewFacts(fixture.summary, null).boundaries[2].description, /未知/);
    fixture.summary.outcome.dynamic_validation = "failed";
    assert.match(overviewFacts(fixture.summary, null).boundaries[2].description, /失败/);
  } finally { await server.close(); }
});

test("概览使用后端结构化理解，作者文本不能补造成代码观察", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: Overview } = await server.ssrLoadModule("/src/components/pages/ReviewOverviewPage.vue");
    const fixtures = JSON.parse(await readFile(new URL("./fixtures/publication-plans.json", import.meta.url), "utf8"));
    const fixture = structuredClone(fixtures.inconclusive);
    fixture.task.pr = { title: "作者声称已经修复", body: "", head: { sha: "a".repeat(40) } };
    fixture.summary.digest.change_understanding = { author_goal: "真实作者目标", actual_changes: "真实代码观察", alignment: "尚未确认下游契约" };
    fixture.summary.outcome.examined_goals = 2;
    fixture.summary.outcome.total_goals = 2;
    fixture.summary.outcome.review_completeness = "complete";
    fixture.summary.outcome.reason_codes = ["investigation_incomplete"];
    fixture.summary.digest.merge_decision.unresolved_reasons = ["investigation_incomplete"];
    // 旧冻结计划缺少可用标签时，展示层才回退到“未核实事项”措辞。
    fixture.summary.digest.merge_decision.label = "暂不能给出合并建议";
    const props = { task: fixture.task, report: null, summaryPlan: fixture.summary, summary: null, findings: [], warnings: [], terminal: true };
    const html = await renderToString(createSSRApp(Overview, props));
    for (const text of ["存在未核实事项，暂不能给出完整建议", "真实作者目标", "真实代码观察", "尚未确认下游契约", "作者声明 · 未独立验证", "结论依据与可信边界", "本轮审查快照"]) {
      assert.ok(html.includes(text), text);
    }
    assert.ok(!html.includes("审查已通过"));
    fixture.summary.digest.change_understanding = null;
    const fallback = await renderToString(createSSRApp(Overview, props));
    assert.ok(fallback.includes("作者声称已经修复"));
    assert.ok(fallback.includes("未提供结构化代码观察"));
    assert.ok(!fallback.includes("真实代码观察"));
    props.summaryPlan = null;
    const unavailable = await renderToString(createSSRApp(Overview, props));
    assert.ok(unavailable.includes("暂时无法形成完整结论"));
    assert.ok(unavailable.includes("摘要未提供准确数量"));
    assert.ok(!unavailable.includes("已检查范围内未发现需处理的问题"));
  } finally { await server.close(); }
});
