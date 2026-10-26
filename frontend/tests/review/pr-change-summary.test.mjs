import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "vite";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";

function task(overrides = {}) {
  return {
    id: "summary-fixture", engine: "task_review_v1",
    pr: { title: "fix(stats): the point estimate sat outside its own interval", body: "Found by the deep-review audit.\n\n`weighted_posterior` builds two estimators.\n\n| point | weighted mean |\n| --- | --- |\n| low | percentile |\n\n```python\nweighted_posterior(wilson(45, 45, 1.0))\n```" },
    changed_files: [
      { file_path: "src/prove/stats.py", change_type: "modified", additions: 17, deletions: 0, is_binary: false },
      { file_path: "tests/test_stats.py", change_type: "modified", additions: 34, deletions: 0, is_binary: false },
    ],
    review_units: [{ id: "u1" }], review_unit_results: [], report_markdown: null,
    repo_snapshot: { language: "rust", languages: ["python", "rust"] }, ...overrides,
  };
}

test("概览使用简短变更摘要，作者 Markdown 与审查结论保持分离", async t => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { prChangeSummary } = await server.ssrLoadModule("/src/utils/prChangeSummary.ts");
    const { default: component } = await server.ssrLoadModule("/src/components/review/diff/PrChangeSummary.vue");

    await t.test("英文长正文不能变成默认摘要；使用实际标题意图与变更语言", () => {
      const result = prChangeSummary(task());
      assert.equal(result.text, "修复 stats 相关问题，并更新相关测试。");
      assert.equal(result.source, "title");
      assert.match(result.subtitle, /根据 PR 标题/);
      assert.deepEqual(result.languages, ["Python"]);
      assert.doesNotMatch(result.text, /Found by|weighted_posterior|```|\|/);
    });
    await t.test("优先读取中文作用摘要，不截取统计结论或代码块", () => {
      const report = "## 审查摘要\n\n0 个问题。\n\n## PR 作用\n\n**作者意图（中文概括）：** 修正统计区间包含点估计值的边界行为，并新增对应的单元测试。\n\n## 详细证据\n\n其他结论。";
      const result = prChangeSummary(task(), report);
      assert.equal(result.text, "修正统计区间包含点估计值的边界行为，并新增对应的单元测试。");
      assert.equal(result.source, "report");
    });
    await t.test("未获得摘要的占位文案不冒充语义摘要，新引擎不使用旧分组结果", () => {
      const value = task({ report_markdown: "## PR 作用\n\n**作用概括：** 未获得可靠的中文作者说明，以下根据实际改动归纳。", review_unit_results: [{ plan: { change_summary: "旧引擎缓存摘要" } }] });
      const result = prChangeSummary(value);
      assert.equal(result.source, "title");
      assert.doesNotMatch(result.text, /旧引擎|未获得/);
      assert.equal(prChangeSummary({ ...value, engine: "legacy_units" }).source, "review");
    });
    await t.test("中文作者说明只取自然语言段落，压缩为最多两句", () => {
      const result = prChangeSummary(task({ pr: { title: "update statistics", body: "<!-- 内部备注 -->\n\n# 更新说明\n\n修正 **统计边界**。补充对应测试。更多说明放在原文。\n\n| 字段 | 说明 |\n| --- | --- |\n| lo | 下界 |" } }));
      assert.equal(result.text, "修正 统计边界。补充对应测试。");
      assert.equal(result.source, "author");
    });
    await t.test("缺少作者说明时按文件兜底，不推断已验证或测试通过", () => {
      const result = prChangeSummary(task({ pr: null }));
      assert.equal(result.source, "files");
      assert.match(result.text, /Python.*相关测试/);
      assert.doesNotMatch(result.text, /通过|已验证|安全|没有问题/);
      assert.equal(prChangeSummary(task({ pr: null, changed_files: [] })).text, "尚无可概括的变更记录。");
    });
    await t.test("卡片还原文件、分组、语言标签；原文默认折叠且不注入 HTML", async () => {
      const value = task({ pr: { title: "修正统计区间边界", body: '<img src=x onerror="alert(1)">\n\n原始说明。' } });
      const html = await renderToString(createSSRApp(component, { task: value }));
      assert.match(html, /修正统计区间边界/);
      assert.match(html, /2 个变更文件/);
      assert.match(html, /1 个审查分组/);
      assert.match(html, /Python/);
      assert.match(html, /查看作者原始说明/);
      assert.doesNotMatch(html, /<img|onerror|原始说明。|\+51|初始分组/);
    });
    await t.test("变更理解优先于标题兜底，作者声明与代码行为分开陈述", async () => {
      const understanding = { author_goal: '修正空值处理 <script>恶意脚本</script>',
        actual_changes: '空值分支先返回，再读取非空对象的字段。', alignment: '尚未核对作者目标的全部前提' };
      const html = await renderToString(createSSRApp(component, { task: task(), understanding }));
      for (const text of ['作者想解决什么', '修正空值处理', '空值分支先返回', '尚未核对作者目标的全部前提']) assert.ok(html.includes(text));
      assert.doesNotMatch(html, /修复 stats 相关问题|<script>/);
      // 旧的证据清单式审计面板不再出现在概览卡片中。
      for (const text of ['实际改动 · ', '已检查的关键行为', '证据 ']) assert.ok(!html.includes(text));
      const historical = await renderToString(createSSRApp(component, { task: task() }));
      assert.match(historical, /修复 stats 相关问题/);
    });
  } finally { await server.close(); }
});
