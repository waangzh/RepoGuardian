import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { readFile } from "node:fs/promises";

// 展示层情景矩阵：内部状态组合成用户可理解的一条结论，后端不新增顶层状态。
test("结果概览情景矩阵与主要操作", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { reviewSituation } = await server.ssrLoadModule("/src/utils/reviewSituation.ts");
    const clean = { outcome: { kind: "clean", confirmed_count: 0, unresolved_count: 0, examined_goals: 10, total_goals: 10,
      incomplete_investigations: 0, reason_codes: [] },
      digest: { merge_decision: { status: "conditional_merge", blocking_finding_keys: [], nonblocking_finding_keys: [] } } };
    const cases = [
      ["clean", clean, "本次检查未发现需在合并前处理的问题", ["view_report"], "复制 PR 评论", "primary"],
      ["nonblocking", { outcome: { ...clean.outcome, kind: "findings", confirmed_count: 2 },
        digest: { merge_decision: { status: "conditional_merge", blocking_finding_keys: [], nonblocking_finding_keys: ["a", "b"] } } },
        "发现 2 项改进建议，暂无高优先级问题", ["view_findings"], "复制 PR 评论", "primary"],
      ["blockers", { outcome: { ...clean.outcome, kind: "findings", confirmed_count: 1 },
        digest: { merge_decision: { status: "fix_blockers", blocking_finding_keys: ["a"], nonblocking_finding_keys: [] } } },
        "发现 1 项高优先级问题，建议修复后复审", ["view_blockers"], "复制审查意见", "secondary"],
      ["evidence_gaps", { outcome: { ...clean.outcome, kind: "inconclusive", incomplete_investigations: 2, reason_codes: ["investigation_incomplete"] },
        digest: { merge_decision: { status: "resolve_unknowns", blocking_finding_keys: [], nonblocking_finding_keys: [] } } },
        "有 2 项关联调查尚未完成", ["view_investigations", "rereview"], "复制审查记录", "secondary"],
      ["scope_incomplete", { outcome: { ...clean.outcome, kind: "inconclusive", examined_goals: 8, reason_codes: ["scope_incomplete"] },
        digest: { merge_decision: { status: "resolve_unknowns", blocking_finding_keys: [], nonblocking_finding_keys: [] } } },
        "还有 2 项约定检查未完成", ["view_gaps", "rereview"], "复制审查记录", "secondary"],
      ["runtime_limits", { outcome: { ...clean.outcome, kind: "inconclusive", review_completeness: "complete",
        runtime_reliability: "unreliable", reason_codes: ["usage_integrity_unknown"] },
        digest: { merge_decision: { status: "resolve_unknowns", blocking_finding_keys: [], nonblocking_finding_keys: [] } } },
        "审查已结束，但结果可靠性仍需核对", ["view_limits", "rereview"], "复制审查记录", "secondary"],
      ["failed", { outcome: { ...clean.outcome, kind: "failed" },
        digest: { merge_decision: { status: "unavailable", blocking_finding_keys: [], nonblocking_finding_keys: [] } } },
        "审查中断，尚无完整结论", ["view_error", "rereview"], "复制部分审查记录", "secondary"],
      ["cancelled", { outcome: { ...clean.outcome, kind: "cancelled", confirmed_count: 1 },
        digest: { merge_decision: { status: "unavailable", blocking_finding_keys: [], nonblocking_finding_keys: [] } } },
        "审查已停止，已保留部分结果", ["view_retained", "rereview"], "复制部分审查记录", "secondary"],
    ];
    for (const [name, plan, title, actions, copyLabel, prominence] of cases) {
      const situation = reviewSituation(plan);
      assert.equal(situation.key, name);
      assert.equal(situation.title, title);
      assert.deepEqual(situation.actions.map(item => item.key), actions);
      // 失败、取消和不完整结论只能复制明确标注的部分记录，不能暗示审查已完成。
      assert.equal(situation.copy?.label, copyLabel, name);
      assert.equal(situation.copy?.prominence, prominence, name);
      // 只有失败/取消会自述为“部分审查记录”。
      assert.equal(situation.copy?.partial, ["failed", "cancelled"].includes(name), name);
    }
    assert.equal(reviewSituation(clean, { stale: true }).key, "stale");
    assert.equal(reviewSituation(clean, { stale: true }).title, "PR 已更新，当前审查结果已过期");
    const stale = reviewSituation(clean, { stale: true });
    assert.equal(stale.copy.label, "复制历史审查记录");
    assert.equal(stale.copy.prominence, "secondary");
    assert.deepEqual(stale.actions.map(action => action.key), ["rereview"]);
    for (const kind of ["failed", "cancelled"]) {
      const partial = reviewSituation({ ...clean, outcome: { ...clean.outcome, kind } }, { stale: true });
      assert.equal(partial.copy.partial, true);
      assert.equal(partial.copy.label, "复制历史部分审查记录");
    }
    // 无风险时不应提供“查看待确认风险”之类无意义操作。
    assert.ok(!reviewSituation(clean).actions.some(item => item.key === "view_unresolved"));
    assert.equal(reviewSituation(null), null);

    // 主结论以后端标签为准：同一审查在概览与详细报告中必须得到同一标题。
    const combined = { outcome: { ...clean.outcome, kind: "findings" },
      digest: { merge_decision: { status: "fix_blockers", label: "发现 1 项高优先级问题",
        blocking_finding_keys: ["a"], nonblocking_finding_keys: [] } } };
    const blocked = reviewSituation(combined);
    assert.equal(blocked.key, "blockers");
    assert.equal(blocked.title, "发现 1 项高优先级问题");
    assert.deepEqual(blocked.actions.map(item => item.key), ["view_blockers"]);
    // 旧冻结计划缺少标签时保留展示层回退，不显示占位文案。
    const legacy = { outcome: { ...clean.outcome, kind: "findings", confirmed_count: 1 },
      digest: { merge_decision: { status: "fix_blockers", label: "暂不能给出合并建议",
        blocking_finding_keys: ["a"], nonblocking_finding_keys: [] } } };
    assert.equal(reviewSituation(legacy).title, "发现 1 项高优先级问题，建议修复后复审");
  } finally { await server.close(); }
});

// 历史复核是补充结果：超时或计量缺失不得把已完成的主审查显示为运行限制。
test("历史讨论复核的独立状态不改变主审查情景", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { reviewSituation } = await server.ssrLoadModule("/src/utils/reviewSituation.ts");
    const clean = { outcome: { kind: "clean", confirmed_count: 0, unresolved_count: 0, examined_goals: 10, total_goals: 10,
      incomplete_investigations: 0, reason_codes: [], review_completeness: "complete",
      runtime_reliability: "reliable",
      historical_discussion: { status: "timed_out", collected: 2, checked: 1, unchecked: 1, verified_defects: [],
        termination_reason: "deadline", limitations: ["history_recheck:deadline"],
        usage: { sent_calls: 1, settled_tokens: 0, unknown_attempts: 1, usage_integrity: "unknown", total_tokens: null },
        affects_risk: false } },
      digest: { merge_decision: { status: "conditional_merge", blocking_finding_keys: [], nonblocking_finding_keys: [] } } };
    const situation = reviewSituation(clean);
    assert.equal(situation.key, "clean");
    assert.equal(situation.title, "本次检查未发现需在合并前处理的问题");

    // 主审查保持 clean，综合建议独立披露历史已核验风险。
    const affected = structuredClone(clean);
    affected.outcome.historical_discussion.status = "completed";
    affected.outcome.historical_discussion.affects_risk = true;
    affected.outcome.historical_discussion.verified_defects = [{ discussion_id: "inline:1", path: "src/a.py",
      reason: "当前实现仍返回空值", evidence_ids: ["e1"] }];
    affected.digest.merge_decision.status = "resolve_unknowns";
    affected.digest.merge_decision.unresolved_reasons = ["historical_discussion_unresolved"];
    const verified = reviewSituation(affected);
    assert.equal(verified.key, "evidence_gaps");
    assert.equal(verified.title, "历史讨论中的问题经代码核验仍存在，需先核对");
    assert.match(verified.description, /已用当前固定代码确认/);
    assert.doesNotMatch(verified.description, /未决事项不等于已确认代码缺陷/);
    assert.equal(affected.outcome.kind, "clean");
    assert.deepEqual(affected.outcome.reason_codes, []);
    const { overviewConclusionDescription } = await server.ssrLoadModule("/src/utils/overviewPresentation.ts");
    // 概览的通用说明同样不能把已核验的历史风险写成未决事项。
    assert.match(overviewConclusionDescription(verified), /历史复核/);
    assert.doesNotMatch(overviewConclusionDescription(verified), /未决事项不等于已确认缺陷/);
  } finally { await server.close(); }
});

test("缺口按实际原因区分，混合限制和旧记录不伪装成代码缺陷", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { reviewSituation } = await server.ssrLoadModule("/src/utils/reviewSituation.ts");
    const fixtures = JSON.parse(await readFile(new URL("../fixtures/publication-plans.json", import.meta.url), "utf8"));
    const runtime = structuredClone(fixtures.runtime_limits.summary);
    assert.equal(reviewSituation(runtime).title, runtime.digest.merge_decision.label);
    assert.equal(reviewSituation(runtime).key, "clean");
    assert.match(reviewSituation(runtime).description, /运行计量记录不完整/);
    runtime.digest.merge_decision.status = "resolve_unknowns";
    runtime.outcome.reason_codes.push("investigation_incomplete");
    assert.equal(reviewSituation(runtime).key, "evidence_gaps");
    runtime.outcome.reason_codes.push("scope_incomplete");
    assert.equal(reviewSituation(runtime).key, "scope_incomplete");
    runtime.outcome.reason_codes = [];
    runtime.outcome.runtime_reliability = "reliable";
    runtime.digest.merge_decision.unresolved_reasons = ["author_context_truncated"];
    assert.equal(reviewSituation(runtime).key, "evidence_gaps");
    runtime.digest.merge_decision.unresolved_reasons = [];
    // 主结论取后端标签；标签缺失（旧冻结计划）才回退到展示层措辞。
    assert.equal(reviewSituation(runtime).title, runtime.digest.merge_decision.label);
    runtime.digest.merge_decision.label = "暂不能给出合并建议";
    assert.equal(reviewSituation(runtime).title, "审查结论暂不完整");
    assert.doesNotMatch(reviewSituation(runtime).description, /仍有候选/);
    runtime.outcome.total_goals = 0;
    runtime.outcome.runtime_reliability = "unknown";
    assert.match(reviewSituation(runtime).description, /检查范围尚不明确/);
    assert.doesNotMatch(reviewSituation(runtime).description, /已检查 0\/0/);
  } finally { await server.close(); }
});

// 概览主操作卡必须按情景切换，失败/取消不能继续呈现“完成审查 + 复制 PR 评论”。
test("结果概览按情景渲染主操作与复制语义", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: Overview } = await server.ssrLoadModule("/src/components/pages/ReviewOverviewPage.vue");
    const fixtures = JSON.parse(await readFile(new URL("../fixtures/publication-plans.json", import.meta.url), "utf8"));
    const task = fixtures.clean.task;
    const expectations = [
      ["clean", "查看完整报告", "复制 PR 评论", false],
      ["findings", "查看建议", "复制 PR 评论", false],
      ["inconclusive", "查看未检查范围", "复制审查记录", false],
      ["failed", "查看错误", "复制部分审查记录", true],
      ["cancelled", "查看已保留结果", "复制部分审查记录", true],
    ];
    for (const [name, action, copyLabel, partial] of expectations) {
      const { summary, plan } = fixtures[name];
      const base = { task: { ...task, id: plan.review_id, status: name === "cancelled" ? "cancelled" : name === "failed" ? "failed" : "completed" },
        summaryPlan: summary, summary: null, findings: [], warnings: [], terminal: true };
      const html = await renderToString(createSSRApp(Overview, base));
      assert.ok(html.includes(action), `${name}: ${action}`);
      assert.ok(html.includes(copyLabel), `${name}: ${copyLabel}`);
      assert.equal(html.includes("重新审查"), ["inconclusive", "failed", "cancelled"].includes(name), `${name}: rereview`);
      // 失败与取消必须明确标注为部分记录；正常结论不能出现这一措辞。
      assert.equal(html.includes("部分审查记录"), partial, name);
      // 已完成或部分完成的审查不再引导用户“完成审查”。
      assert.ok(!html.includes("完成审查"), `${name}: 不应出现完成审查`);
    }
  } finally { await server.close(); }
});


test("计量附注和历史分级采用后端事实并引导到可见证据", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { reviewSituation } = await server.ssrLoadModule("/src/utils/reviewSituation.ts");
    const fixtures = JSON.parse(await readFile(new URL("../fixtures/publication-plans.json", import.meta.url), "utf8"));
    const meter = structuredClone(fixtures.runtime_limits.summary);
    assert.equal(meter.outcome.kind, "inconclusive");
    assert.equal(meter.outcome.coverage, "complete");
    assert.equal(reviewSituation(meter).key, "clean");
    assert.equal(reviewSituation(meter).title, "已检查范围内未发现已确认问题");
    assert.match(reviewSituation(meter).description, /计量记录不完整/);
    const history = structuredClone(fixtures.clean.summary);
    history.digest.merge_decision = { status: "fix_blockers", label: "历史高优先级问题", blocking_finding_keys: [],
      nonblocking_finding_keys: [], blocking_historical_keys: ["inline:1"], nonblocking_historical_keys: [] };
    const { overviewFacts } = await server.ssrLoadModule("/src/utils/overviewPresentation.ts");
    assert.equal(overviewFacts(history, null).blockers, 1);
    assert.equal(reviewSituation(history).blockingCount, 1);
    assert.equal(reviewSituation(history).key, "blockers");
    // 历史风险已进入问题工作台，可直接按来源核对，不再降级为只看报告。
    assert.equal(reviewSituation(history).actions[0].key, "view_blockers");
    history.digest.merge_decision.status = "conditional_merge";
    history.digest.merge_decision.blocking_historical_keys = [];
    history.digest.merge_decision.nonblocking_historical_keys = ["inline:1"];
    assert.equal(reviewSituation(history).nonblockingCount, 1);
    assert.equal(reviewSituation(history).key, "nonblocking");
    assert.equal(reviewSituation(history).actions[0].key, "view_findings");
    // 同一缺陷同时出现在本轮与历史复核时，标题与概览只计一次；旧计划缺少去重字段才相加。
    const duplicate = structuredClone(history);
    duplicate.digest.merge_decision.blocking_finding_keys = ["key-a"];
    duplicate.digest.merge_decision.nonblocking_finding_keys = [];
    duplicate.digest.merge_decision.nonblocking_historical_keys = [];
    duplicate.digest.merge_decision.blocking_historical_keys = ["inline:1"];
    duplicate.digest.merge_decision.blocking_independent_count = 1;
    duplicate.digest.merge_decision.deduplicated_historical_keys = ["inline:1"];
    assert.equal(reviewSituation(duplicate).blockingCount, 1);
    assert.equal(overviewFacts(duplicate, null).blockers, 1);
    duplicate.digest.merge_decision.blocking_independent_count = null;
    // 旧计划仍有去重标注时按标注合并，否则数量会大于列表中可核对的条目。
    assert.equal(reviewSituation(duplicate).blockingCount, 1, "旧计划按去重标注合并");
    assert.equal(overviewFacts(duplicate, null).blockers, 1);
    duplicate.digest.merge_decision.deduplicated_historical_keys = [];
    assert.equal(reviewSituation(duplicate).blockingCount, 2, "无去重标注的旧冻结计划保留来源相加");
    assert.equal(overviewFacts(duplicate, null).blockers, 2);
    const suggestions = structuredClone(history);
    suggestions.digest.merge_decision = { status: "conditional_merge", label: "", blocking_finding_keys: [],
      nonblocking_finding_keys: ["key-a"], blocking_historical_keys: [], nonblocking_historical_keys: ["inline:1"],
      nonblocking_independent_count: 1 };
    assert.equal(reviewSituation(suggestions).nonblockingCount, 1);
    suggestions.digest.merge_decision.nonblocking_independent_count = null;
    assert.equal(reviewSituation(suggestions).nonblockingCount, 2, "旧冻结计划保留来源相加");
    const plan = structuredClone(fixtures.clean.plan);
    plan.outcome.historical_discussion = { findings: [{ discussion_id: "inline:1", status: "still_present",
      severity: "high", priority: "fix_before_merge", affected_behavior: "受保护分支仍可被匿名访问",
      recommendation: "在访问前校验身份", reason: "固定源码证据", evidence_ids: ["history-e1"],
      discussion_url: "https://github.com/o/r/pull/1#discussion_r1" }] };
    const Readable = (await server.ssrLoadModule("/src/components/report/ReportReadableView.vue")).default;
    const html = await renderToString(createSSRApp(Readable, { plan }));
    for (const text of ["建议合并前修复", "受保护分支仍可被匿名访问", "在访问前校验身份", "history-e1", "查看已有讨论"])
      assert.ok(html.includes(text), text);
  } finally { await server.close(); }
});
