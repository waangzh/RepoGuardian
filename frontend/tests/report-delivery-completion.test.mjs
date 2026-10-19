import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { readFile } from "node:fs/promises";

test("变更理解只呈现三个结论性问题，不展开证据审计面板", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/review/PrChangeSummary.vue");
    const fixtures = JSON.parse(await readFile(new URL("./fixtures/publication-plans.json", import.meta.url), "utf8"));
    const task = fixtures.clean.task;
    const understanding = { author_goal: "作者声明：拒绝空值", actual_changes: "空值分支先返回，再读取字段。",
      alignment: "1 项目标获得局部证据支持；调用方尚未核验。" };
    const html = await renderToString(createSSRApp(component, { task, understanding }));
    for (const value of ["作者想解决什么", "代码实际改变了什么", "吻合与未确认", "拒绝空值", "空值分支先返回"]) assert.ok(html.includes(value));
    // 证据 ID、逐目标展开和检查理由不应出现在概览卡片。
    for (const value of ["evidence_ids", "e1", "已检查的关键行为", "目标符合性："]) assert.ok(!html.includes(value));
  } finally { await server.close(); }
});

test("概览使用统一合并建议并区分补丁结果与 Head 验证", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/review/ReviewOutcomePanel.vue");
    const plan = { outcome: { kind: "clean", dynamic_validation: "independent_results", reason_codes: [] }, withheld_findings: [],
      digest: { merge_decision: { status: "conditional_merge", label: "未发现阻断性问题", advice: "统一事实要求补查缓存路径",
        blocking_finding_keys: ["a"], nonblocking_finding_keys: [], limits: ["调用方尚未核验"] },
        validations: [{ id: "v", source: "user_runner", stage: "patched", status: "passed", reason: "不代表当前 Head 已通过" }] } };
    const html = await renderToString(createSSRApp(component, { plan }));
    for (const value of ["已有独立补丁结果，Head 未知", "未发现阻断性问题", "统一事实要求补查缓存路径",
      "不代表当前 Head 已通过", "调用方尚未核验"]) assert.ok(html.includes(value));
  } finally { await server.close(); }
});

test("作者声明截断或历史完整性未知时，概览保留服务端限制", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/review/PrChangeSummary.vue");
    const fixtures = JSON.parse(await readFile(new URL("./fixtures/publication-plans.json", import.meta.url), "utf8"));
    for (const [status, limitation] of [["truncated", "送审内容已截断；未输入的部分尚未核验"], ["unknown", "历史审查未记录作者声明是否完整输入"]]) {
      const understanding = { author_goal: "作者声明（输入不完整）", actual_changes: "已归纳变更", alignment: "部分目标未核验" };
      const html = await renderToString(createSSRApp(component, { task: fixtures.clean.task, understanding, authorLimitation: limitation }));
      assert.ok(html.includes(limitation)); assert.match(html, /role="status"/);
    }
  } finally { await server.close(); }
});

test("问题列表使用服务端有效等级，保留原始运行记录", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const fixtures = JSON.parse(await readFile(new URL("./fixtures/publication-plans.json", import.meta.url), "utf8"));
    const task = structuredClone(fixtures.findings.task);
    task.task_runtime_report.findings[0].issue.severity = "high";
    const { runtimeFindingRows } = await server.ssrLoadModule("/src/utils/taskRuntimePresentation.ts");
    const { default: component } = await server.ssrLoadModule("/src/components/pages/ReviewFindingsPage.vue");
    const findings = runtimeFindingRows(task.task_runtime_report, task.issues);
    const publicationFindings = fixtures.findings.plan.inline_drafts.map(item => ({ ...item, severity: "medium", body_markdown: "" }));
    const html = await renderToString(createSSRApp(component, { task, findings, publicationFindings, focusKey: "" }));
    assert.match(html, /class="(?:badge neutral|neutral badge)">中<\/span>/);
    assert.equal(task.task_runtime_report.findings[0].issue.severity, "high");
  } finally { await server.close(); }
});
