import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";

// 导航分组：三个主要任务平级，证据页归入补充入口，发布是操作而非信息页。
test("审查导航突出概览、问题与报告，证据页与发布入口不再平级", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const workspace = await server.ssrLoadModule("/src/types/workspace.ts");
    assert.deepEqual(workspace.reviewPrimarySections.map(item => item.id), ["overview", "findings", "report"]);
    assert.deepEqual(workspace.reviewSecondarySections.map(item => item.id), ["scope", "files", "activity"]);
    assert.equal(workspace.reviewPublishSection.id, "publish");
    // 页面标题仍能查找全部入口，分组不丢失页面。
    assert.deepEqual(workspace.reviewSections.map(item => item.id),
      ["overview", "findings", "report", "scope", "files", "activity", "publish"]);

    const { default: Sidebar } = await server.ssrLoadModule("/src/components/layout/AppSidebar.vue");
    const sidebar = await renderToString(createSSRApp(Sidebar, { modelValue: "dashboard", section: "overview",
      hasReview: true, taskActive: false, mobileOpen: false, busy: false }));
    const sideText = sidebar.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
    assert.match(sideText, /补充证据/);
    for (const label of ["结果概览", "问题与证据", "审查报告", "覆盖与限制", "变更文件", "技术记录"]) assert.ok(sideText.includes(label), label);
    // 发布入口不再出现在侧栏主导航中。
    assert.ok(!sideText.includes("发布或复制到 GitHub"), "侧栏不应平铺发布入口");

    const { default: Workspace } = await server.ssrLoadModule("/src/components/review/ReviewWorkspace.vue");
    const task = { id: "run", status: "completed", engine: "task_review_v1", changed_files: [], issues: [], pr_url: "https://github.com/o/r/pull/1" };
    const html = await renderToString(createSSRApp(Workspace, { task, report: null, section: "overview",
      statusText: "执行已完成", duration: "1 秒", cancelling: false, retryingUnitId: null }));
    const nav = /<nav[^>]*aria-label="审查结果导航"[^>]*>([\s\S]*?)<\/nav>/.exec(html)?.[1] || "";
    const labels = [...nav.matchAll(/<button[^>]*>([^<]*)</g)].map(match => match[1].trim());
    assert.deepEqual(labels, ["结果概览", "问题与证据", "审查报告", "覆盖与限制", "变更文件", "技术记录", "发布或复制到 GitHub"]);
    assert.match(nav, /role="group" aria-label="补充证据"/);
    assert.match(nav, /class="tab-action"/);
  } finally { await server.close(); }
});
