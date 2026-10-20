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
        "未发现阻断性问题，但有改进建议", ["view_findings"], "复制 PR 评论", "primary"],
      ["blockers", { outcome: { ...clean.outcome, kind: "findings", confirmed_count: 1 },
        digest: { merge_decision: { status: "fix_blockers", blocking_finding_keys: ["a"], nonblocking_finding_keys: [] } } },
        "发现高优先级问题，建议修复后再合并", ["view_blockers"], "复制审查意见", "secondary"],
      ["evidence_gaps", { outcome: { ...clean.outcome, kind: "inconclusive", reason_codes: ["investigation_incomplete"] },
        digest: { merge_decision: { status: "resolve_unknowns", blocking_finding_keys: [], nonblocking_finding_keys: [] } } },
        "部分问题尚未核实，暂不能给出完整建议", ["view_gaps", "rereview"], "复制审查记录", "secondary"],
      ["scope_incomplete", { outcome: { ...clean.outcome, kind: "inconclusive", examined_goals: 8, reason_codes: ["scope_incomplete"] },
        digest: { merge_decision: { status: "resolve_unknowns", blocking_finding_keys: [], nonblocking_finding_keys: [] } } },
        "部分检查尚未完成", ["view_gaps", "rereview"], "复制审查记录", "secondary"],
      ["validation_failed", { outcome: { ...clean.outcome, dynamic_validation: "failed" }, digest: clean.digest },
        "当前版本已有验证失败记录", ["view_validation", "rereview"], "复制审查记录", "secondary"],
      ["failed", { outcome: { ...clean.outcome, kind: "failed" },
        digest: { merge_decision: { status: "unavailable", blocking_finding_keys: [], nonblocking_finding_keys: [] } } },
        "审查因执行错误中断", ["view_error", "rereview"], "复制部分审查记录", "secondary"],
      ["cancelled", { outcome: { ...clean.outcome, kind: "cancelled", confirmed_count: 1 },
        digest: { merge_decision: { status: "unavailable", blocking_finding_keys: [], nonblocking_finding_keys: [] } } },
        "审查已由用户停止", ["view_retained", "rereview"], "复制部分审查记录", "secondary"],
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
    assert.equal(reviewSituation(clean, { stale: true }).title, "PR 已更新，当前结论可能已过期");
    // 旧版本结果不提供复制，避免继续交付已经过期的结论。
    assert.equal(reviewSituation(clean, { stale: true }).copy, null);
    // 无风险时不应提供“查看待确认风险”之类无意义操作。
    assert.ok(!reviewSituation(clean).actions.some(item => item.key === "view_unresolved"));
    assert.equal(reviewSituation(null), null);
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
      ["findings", "查看问题", "复制 PR 评论", false],
      ["inconclusive", "查看证据缺口", "复制审查记录", false],
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
