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
      incomplete_investigations: 0, reason_codes: [], dynamic_validation: "not_executed" },
      digest: { merge_decision: { status: "conditional_merge", blocking_finding_keys: [], nonblocking_finding_keys: [] } } };
    const cases = [
      ["clean", clean, "已检查范围内未发现需处理的问题", ["view_report"], "复制 PR 评论", "primary"],
      ["nonblocking", { outcome: { ...clean.outcome, kind: "findings", confirmed_count: 2 },
        digest: { merge_decision: { status: "conditional_merge", blocking_finding_keys: [], nonblocking_finding_keys: ["a", "b"] } } },
        "发现 2 项改进建议，未发现阻断性问题", ["view_findings"], "复制 PR 评论", "primary"],
      ["blockers", { outcome: { ...clean.outcome, kind: "findings", confirmed_count: 1 },
        digest: { merge_decision: { status: "fix_blockers", blocking_finding_keys: ["a"], nonblocking_finding_keys: [] } } },
        "发现 1 项高优先级问题，建议修复后再合并", ["view_blockers"], "复制审查意见", "secondary"],
      ["evidence_gaps", { outcome: { ...clean.outcome, kind: "inconclusive", incomplete_investigations: 2, reason_codes: ["investigation_incomplete"] },
        digest: { merge_decision: { status: "resolve_unknowns", blocking_finding_keys: [], nonblocking_finding_keys: [] } } },
        "2 项关联调查尚未核实，暂不能给出完整建议", ["view_investigations", "rereview"], "复制审查记录", "secondary"],
      ["scope_incomplete", { outcome: { ...clean.outcome, kind: "inconclusive", examined_goals: 8, reason_codes: ["scope_incomplete"] },
        digest: { merge_decision: { status: "resolve_unknowns", blocking_finding_keys: [], nonblocking_finding_keys: [] } } },
        "仍有 2 项检查目标未完成", ["view_gaps", "rereview"], "复制审查记录", "secondary"],
      ["runtime_limits", { outcome: { ...clean.outcome, kind: "inconclusive", review_completeness: "complete",
        runtime_reliability: "unreliable", reason_codes: ["usage_integrity_unknown"] },
        digest: { merge_decision: { status: "resolve_unknowns", blocking_finding_keys: [], nonblocking_finding_keys: [] } } },
        "审查运行记录存在限制，需核对结果完整性", ["view_limits", "rereview"], "复制审查记录", "secondary"],
      ["validation_failed", { outcome: { ...clean.outcome, dynamic_validation: "failed" }, digest: clean.digest },
        "当前版本存在验证失败记录，需检查原因", ["view_validation", "rereview"], "复制审查记录", "secondary"],
      ["failed", { outcome: { ...clean.outcome, kind: "failed" },
        digest: { merge_decision: { status: "unavailable", blocking_finding_keys: [], nonblocking_finding_keys: [] } } },
        "审查因执行错误中断，已保留部分记录", ["view_error", "rereview"], "复制部分审查记录", "secondary"],
      ["cancelled", { outcome: { ...clean.outcome, kind: "cancelled", confirmed_count: 1 },
        digest: { merge_decision: { status: "unavailable", blocking_finding_keys: [], nonblocking_finding_keys: [] } } },
        "审查已停止，已保留停止前的记录", ["view_retained", "rereview"], "复制部分审查记录", "secondary"],
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
    assert.equal(reviewSituation(clean, { stale: true }).title, "PR 已更新，本轮结论不适用于最新提交");
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
    const combined = { outcome: { ...clean.outcome, kind: "findings", dynamic_validation: "failed" },
      digest: { merge_decision: { status: "fix_blockers", label: "发现 1 项高优先级问题，另有当前版本验证失败",
        blocking_finding_keys: ["a"], nonblocking_finding_keys: [] } } };
    const blockedAndFailed = reviewSituation(combined);
    assert.equal(blockedAndFailed.key, "blockers");
    assert.equal(blockedAndFailed.title, "发现 1 项高优先级问题，另有当前版本验证失败");
    assert.deepEqual(blockedAndFailed.actions.map(item => item.key), ["view_blockers", "view_validation"]);
    assert.match(blockedAndFailed.description, /另有验证失败记录/);
    // 仅有验证失败且后端已给出标签时，展示层不另造标题。
    const failedOnly = { outcome: { ...clean.outcome, dynamic_validation: "failed" },
      digest: { merge_decision: { status: "resolve_unknowns", label: "当前版本存在验证失败记录，需检查原因",
        blocking_finding_keys: [], nonblocking_finding_keys: [] } } };
    assert.equal(reviewSituation(failedOnly).title, "当前版本存在验证失败记录，需检查原因");
    // 旧冻结计划缺少标签时保留展示层回退，不显示占位文案。
    const legacy = { outcome: { ...clean.outcome, kind: "findings", confirmed_count: 1 },
      digest: { merge_decision: { status: "fix_blockers", label: "暂不能给出合并建议",
        blocking_finding_keys: ["a"], nonblocking_finding_keys: [] } } };
    assert.equal(reviewSituation(legacy).title, "发现 1 项高优先级问题，建议修复后再合并");
  } finally { await server.close(); }
});

// 历史复核是补充结果：超时或计量缺失不得把已完成的主审查显示为运行限制。
test("历史讨论复核的独立状态不改变主审查情景", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { reviewSituation } = await server.ssrLoadModule("/src/utils/reviewSituation.ts");
    const clean = { outcome: { kind: "clean", confirmed_count: 0, unresolved_count: 0, examined_goals: 10, total_goals: 10,
      incomplete_investigations: 0, reason_codes: [], dynamic_validation: "not_executed", review_completeness: "complete",
      runtime_reliability: "reliable",
      historical_discussion: { status: "timed_out", collected: 2, checked: 1, unchecked: 1, verified_defects: [],
        termination_reason: "deadline", limitations: ["history_recheck:deadline"],
        usage: { sent_calls: 1, settled_tokens: 0, unknown_attempts: 1, usage_integrity: "unknown", total_tokens: null },
        affects_risk: false } },
      digest: { merge_decision: { status: "conditional_merge", blocking_finding_keys: [], nonblocking_finding_keys: [] } } };
    const situation = reviewSituation(clean);
    assert.equal(situation.key, "clean");
    assert.equal(situation.title, "已检查范围内未发现需处理的问题");

    // 只有经独立代码核验确认仍存在的历史问题才进入证据缺口情景。
    const affected = structuredClone(clean);
    affected.outcome.kind = "inconclusive";
    affected.outcome.reason_codes = ["historical_discussion_unresolved"];
    affected.outcome.historical_discussion.status = "completed";
    affected.outcome.historical_discussion.affects_risk = true;
    affected.outcome.historical_discussion.verified_defects = [{ discussion_id: "inline:1", path: "src/a.py",
      reason: "当前实现仍返回空值", evidence_ids: ["e1"] }];
    affected.digest.merge_decision.status = "resolve_unknowns";
    const verified = reviewSituation(affected);
    assert.equal(verified.key, "evidence_gaps");
    assert.equal(verified.title, "历史讨论中的问题经代码核验仍存在，需先核对");
    assert.match(verified.description, /已用当前固定代码确认/);
    assert.doesNotMatch(verified.description, /未决事项不等于已确认代码缺陷/);
  } finally { await server.close(); }
});

test("缺口按实际原因区分，混合限制和旧记录不伪装成代码缺陷", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { reviewSituation } = await server.ssrLoadModule("/src/utils/reviewSituation.ts");
    const fixtures = JSON.parse(await readFile(new URL("./fixtures/publication-plans.json", import.meta.url), "utf8"));
    const runtime = structuredClone(fixtures.runtime_limits.summary);
    assert.equal(reviewSituation(runtime).title, runtime.digest.merge_decision.label);
    assert.match(reviewSituation(runtime).description, /已检查 1\/1 项/);
    assert.match(reviewSituation(runtime).description, /不代表代码缺陷/);
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
    const fixtures = JSON.parse(await readFile(new URL("./fixtures/publication-plans.json", import.meta.url), "utf8"));
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
