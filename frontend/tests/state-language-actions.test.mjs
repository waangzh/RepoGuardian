import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "vite";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";

test("状态文案区分事实、合并影响与可执行入口", async t => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  const render = (component, props) => renderToString(createSSRApp(component, props));
  try {
    const fixtures = JSON.parse(await readFile(new URL("./fixtures/publication-plans.json", import.meta.url), "utf8"));
    const { reviewSituation, situationActionTarget } = await server.ssrLoadModule("/src/utils/reviewSituation.ts");
    await t.test("概览和后端标签一致，候选与调查不相加，未知计数不补造", () => {
      for (const name of ["clean", "findings", "blockers", "inconclusive", "runtime_limits", "failed", "cancelled"]) {
        assert.equal(reviewSituation(fixtures[name].summary).title, fixtures[name].plan.digest.merge_decision.label, name);
      }
      // 主结论始终是后端标签；展示层只补操作与说明，不另造标题。
      const mixed = structuredClone(fixtures.runtime_limits.summary);
      mixed.outcome.unresolved_count = 2;
      mixed.outcome.incomplete_investigations = 3;
      assert.equal(reviewSituation(mixed).title, mixed.digest.merge_decision.label);
      assert.deepEqual(reviewSituation(mixed).actions.map(item => item.key), ["view_unresolved", "view_investigations", "rereview"]);
      // 旧冻结计划没有可用标签时才回退到展示层措辞。
      const unknown = structuredClone(fixtures.blockers.summary);
      delete unknown.digest.merge_decision.blocking_finding_keys;
      unknown.digest.merge_decision.label = "暂不能给出合并建议";
      assert.equal(reviewSituation(unknown).blockingCount, null);
      assert.equal(reviewSituation(unknown).title, "发现高优先级问题，建议修复后复审");
      const inconsistent = structuredClone(fixtures.clean.summary);
      inconsistent.outcome.kind = "inconclusive";
      inconsistent.outcome.reason_codes = ["scope_incomplete"];
      inconsistent.digest.merge_decision.status = "resolve_unknowns";
      inconsistent.digest.merge_decision.label = "暂不能给出合并建议";
      assert.equal(reviewSituation(inconsistent).title, "检查范围完整性尚待核对");
    });
    await t.test("操作目标保留筛选与记录区，不止跳到通用页面", () => {
      assert.deepEqual(situationActionTarget("view_blockers"), { section: "findings", findingFilter: "blockers" });
      assert.deepEqual(situationActionTarget("view_unresolved"), { section: "findings", findingFilter: "unresolved" });
      assert.deepEqual(situationActionTarget("view_gaps"), { section: "scope", scopeFilter: "incomplete" });
      assert.deepEqual(situationActionTarget("view_investigations"), { section: "scope", investigations: true });
      assert.deepEqual(situationActionTarget("view_validation"), { section: "activity", activityFocus: "validation" });
      assert.deepEqual(situationActionTarget("view_limits"), { section: "activity", activityFocus: "limits" });
    });
    await t.test("发现页入口实际筛选后端阻断键、建议和未决记录", async () => {
      const { default: Findings } = await server.ssrLoadModule("/src/components/pages/ReviewFindingsPage.vue");
      const rows = [
        ["high", "高优先级记录", "confirmed", "confirmed"],
        ["downgraded", "尚待加载服务端严重度的记录", "confirmed", "confirmed"],
        ["medium", "中级建议记录", "confirmed", "confirmed"],
        ["pending", "未核验候选记录", "candidate", "unresolved"],
        ["human", "待人工确认记录", "needs_human", "unknown"],
      ].map(([key, title, status, group]) => ({
        key, title, status, group, filePath: "", line: null, issue: null,
        affectedBehavior: "", failureScenario: "", recommendation: "", reason: "", rawReason: "",
      }));
      for (const [filter, visible] of [["blockers", ["high"]], ["suggestions", ["medium"]], ["unresolved", ["pending", "human"]]]) {
        const html = await render(Findings, { task: fixtures.clean.task, findings: rows, focusKey: "",
          publicationFindings: [], initialFilter: filter, blockingKeys: ["high"], suggestionKeys: ["medium"] });
        for (const row of rows) assert.equal(html.includes(row.title), visible.includes(row.key), `${filter}:${row.key}`);
      }
    });
    await t.test("范围入口筛选未检查目标并展开调查，验证入口展示真实绑定记录", async () => {
      const { default: Scope } = await server.ssrLoadModule("/src/components/pages/ReviewScopePage.vue");
      const runtime = { ...fixtures.clean.task.task_runtime_report, initial_scope_coverage: [
        { goal_id: "done", target_ref: "already-examined.py", status: "examined" },
        { goal_id: "gap", target_ref: "remaining-target.py", status: "not_examined" },
      ] };
      const html = await render(Scope, { task: fixtures.clean.task, runtime, terminal: false, retryingUnitId: null,
        initialFilter: "incomplete", investigationsOpen: true });
      const table = /<tbody>([\s\S]*?)<\/tbody>/.exec(html)?.[1] || "";
      assert.ok(table.includes("remaining-target.py"));
      assert.ok(!table.includes("already-examined.py"));
      assert.match(html, /<details[^>]*open/);
      const { default: Activity } = await server.ssrLoadModule("/src/components/pages/ReviewActivityPage.vue");
      const summary = structuredClone(fixtures.clean.summary);
      summary.digest.validations = [{ id: "check-1", source: "受控外部执行端", stage: "head", status: "failed",
        reason: "仅检查缓存模块", trusted: true, snapshot_matches: true }];
      const activity = await render(Activity, { task: fixtures.clean.task, runtime: null, summary, duration: "", initialFocus: "validation" });
      assert.ok(activity.includes("仅检查缓存模块") && activity.includes("已绑定本次快照"));
      assert.ok(activity.includes("不代表整个项目 CI 全部通过"));
      const legacySummary = structuredClone(summary);
      delete legacySummary.digest.merge_decision;
      delete legacySummary.digest.validations;
      for (const optionalSummary of [null, legacySummary]) {
        const missing = await render(Activity, { task: { ...fixtures.clean.task, validation: [] }, runtime: null,
          summary: optionalSummary, duration: "", initialFocus: "limits" });
        assert.ok(missing.includes("未保存明确的限制条目"));
        assert.ok(missing.includes("尚无可核对的动态验证结果"));
      }
    });
    await t.test("摘要失败有明确重试入口，无问题时复制是主要交付操作", async () => {
      const { default: Overview } = await server.ssrLoadModule("/src/components/pages/ReviewOverviewPage.vue");
      const base = { task: fixtures.clean.task, summary: null, findings: [], warnings: [], terminal: true };
      const failed = await render(Overview, { ...base, summaryPlan: null, summaryError: true });
      assert.ok(failed.includes("当前无法读取完整审查结论") && failed.includes("重试加载摘要"));
      const clean = await render(Overview, { ...base, summaryPlan: fixtures.clean.summary });
      const copyButton = [...clean.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)]
        .find(([, , content]) => content.replace(/<[^>]*>/g, "").trim() === "复制 PR 评论");
      assert.ok(copyButton && /\bbutton--primary\b/.test(copyButton[1]), "复制评论应呈现为主要操作");
      assert.ok(clean.includes("阅读报告，或预览并复制 PR 评论。"));
      assert.ok(!clean.includes("请点击“复制 PR 评论”"), "结论区不再重复操作教学");
    });
    await t.test("执行完成不是质量结论，验证通过只覆盖已记录检查", async () => {
      const { reportValidationLabel } = await server.ssrLoadModule("/src/utils/reportPresentation.ts");
      assert.equal(reportValidationLabel("passed"), "当前 Head 的受控检查通过");
      const { default: Badge } = await server.ssrLoadModule("/src/components/common/StatusBadge.vue");
      const completed = await render(Badge, { status: "completed" });
      assert.ok(completed.includes("执行已完成") && completed.includes("不代表 PR 安全或可以合并"));
      const passed = await render(Badge, { status: "passed" });
      assert.ok(passed.includes("所选检查通过") && passed.includes("不代表整个项目 CI 全部通过"));
      const { default: Confirmation } = await server.ssrLoadModule("/src/components/review/ReviewCommentConfirmation.vue");
      const unknown = await render(Confirmation, { pending: { status: "unknown", reviewed_head_sha: "a".repeat(40) }, feedback: "" });
      assert.ok(unknown.includes("暂时无法确认 PR 是否有新提交") && unknown.includes("重新检查 PR 版本"));
    });
  } finally { await server.close(); }
});
