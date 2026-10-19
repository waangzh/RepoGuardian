import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";

// 展示层情景矩阵：内部状态组合成用户可理解的一条结论，后端不新增顶层状态。
test("结果概览情景矩阵与主要操作", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { reviewSituation } = await server.ssrLoadModule("/src/utils/reviewSituation.ts");
    const clean = { outcome: { kind: "clean", confirmed_count: 0, unresolved_count: 0, examined_goals: 10, total_goals: 10,
      incomplete_investigations: 0, reason_codes: [], dynamic_validation: "not_executed" },
      digest: { merge_decision: { status: "conditional_merge", blocking_finding_keys: [], nonblocking_finding_keys: [] } } };
    const cases = [
      ["clean", clean, "本次静态审查未发现需处理的问题", []],
      ["nonblocking", { outcome: { ...clean.outcome, kind: "findings", confirmed_count: 2 },
        digest: { merge_decision: { status: "conditional_merge", blocking_finding_keys: [], nonblocking_finding_keys: ["a", "b"] } } },
        "发现非阻断性问题", ["view_findings"]],
      ["blockers", { outcome: { ...clean.outcome, kind: "findings", confirmed_count: 1 },
        digest: { merge_decision: { status: "fix_blockers", blocking_finding_keys: ["a"], nonblocking_finding_keys: [] } } },
        "发现需要优先修复的问题", ["view_blockers"]],
      ["evidence_gaps", { outcome: { ...clean.outcome, kind: "inconclusive", reason_codes: ["investigation_incomplete"] },
        digest: { merge_decision: { status: "resolve_unknowns", blocking_finding_keys: [], nonblocking_finding_keys: [] } } },
        "审查结论尚不充分", ["view_gaps", "rereview"]],
      ["scope_incomplete", { outcome: { ...clean.outcome, kind: "inconclusive", examined_goals: 8, reason_codes: ["scope_incomplete"] },
        digest: { merge_decision: { status: "resolve_unknowns", blocking_finding_keys: [], nonblocking_finding_keys: [] } } },
        "部分审查已完成", ["view_gaps", "rereview"]],
      ["validation_failed", { outcome: { ...clean.outcome, dynamic_validation: "failed" }, digest: clean.digest },
        "已有验证未通过", ["view_validation", "rereview"]],
      ["failed", { outcome: { ...clean.outcome, kind: "failed" },
        digest: { merge_decision: { status: "unavailable", blocking_finding_keys: [], nonblocking_finding_keys: [] } } },
        "审查未完成", ["view_activity", "rereview"]],
      ["cancelled", { outcome: { ...clean.outcome, kind: "cancelled", confirmed_count: 1 },
        digest: { merge_decision: { status: "unavailable", blocking_finding_keys: [], nonblocking_finding_keys: [] } } },
        "审查已停止", ["view_findings", "rereview"]],
    ];
    for (const [name, plan, title, actions] of cases) {
      const situation = reviewSituation(plan);
      assert.equal(situation.key, name);
      assert.equal(situation.title, title);
      assert.deepEqual(situation.actions.map(item => item.key), actions);
    }
    assert.equal(reviewSituation(clean, { stale: true }).key, "stale");
    assert.equal(reviewSituation(clean, { stale: true }).title, "审查结果基于旧代码版本");
    // 无风险时不应提供“查看待确认风险”之类无意义操作。
    assert.ok(!reviewSituation(clean).actions.some(item => item.key === "view_unresolved"));
    assert.equal(reviewSituation(null), null);
  } finally { await server.close(); }
});
