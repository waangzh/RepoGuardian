import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "vite";
import { createSSRApp, h } from "vue";
import { renderToString } from "@vue/server-renderer";

const fixtures = JSON.parse(await readFile(new URL("../fixtures/publication-plans.json", import.meta.url), "utf8"));

test("发布页在进入前区分复制评论与直接发布，未启用时不展示口令入口", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: Panel } = await server.ssrLoadModule("/src/components/review/publication/GitHubPublicationPanel.vue");
    const { plan } = fixtures.findings;
    const render = capability => renderToString(createSSRApp({ render: () => h(Panel, { reviewId: plan.review_id, plan, capability }) }));

    const disabled = await render({ enabled: false, mode: "copy_only" });
    // 两种交付路径同时可见，且明确说明复制不写入 GitHub。
    for (const text of ["复制评论", "直接发布到 GitHub", "只把评论正文写入剪贴板", "当前仅支持", "复制评论"]) assert.ok(disabled.includes(text), text);
    assert.ok(disabled.includes("REPOGUARDIAN_GITHUB_PUBLISH_ENABLED"));
    // 未启用时不应让用户先输入操作口令再发现功能不可用。
    assert.ok(!disabled.includes("本地发布操作口令"));
    assert.ok(!disabled.includes("检查发布条件并预览"));

    const enabled = await render({ enabled: true, mode: "publish" });
    assert.ok(enabled.includes("本地发布操作口令"));
    assert.ok(enabled.includes("检查发布条件并预览"));
    assert.ok(!enabled.includes("当前仅支持"));
    // 复制路径在两种状态下都可用，且不依赖操作口令。
    assert.ok(enabled.includes("复制 PR 评论"));
  } finally { await server.close(); }
});

test("发布结果与预检使用用户语言，不直接展示协议值", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const utils = await server.ssrLoadModule("/src/utils/publicationPresentation.ts");
    for (const state of ["queued", "preparing", "publishing", "succeeded", "partial", "failed", "uncertain", "stale"]) {
      const label = utils.publicationStateLabel[state];
      assert.ok(label && !label.includes(state), state);
    }
    for (const state of ["pending", "publishing", "published", "failed", "uncertain", "summary_only"]) {
      assert.ok(utils.publicationItemStateLabel[state] && !utils.publicationItemStateLabel[state].includes(state), state);
    }
    assert.equal(utils.inlineActionLabel.already_published, "相同评论已存在，本次不重复发布");
    assert.equal(utils.inlineActionLabel.summary_fallback, "无法准确定位到代码行，将写入总结评论");
    assert.equal(utils.publicationItemStateLabel.summary_only, "无法定位到代码行，已写入总结评论");
    assert.equal(utils.publicationItemKindLabel("summary"), "PR 总结评论");
    assert.equal(utils.publicationItemKindLabel("inline"), "代码行评论");
    // 错误码翻译保留可排障信息，但不再以英文短句作为用户提示。
    assert.match(utils.publicationErrorText("publication_disabled"), /只能复制评论/);
    assert.match(utils.publicationErrorText("summary_newer_review"), /更新版本/);
    assert.match(utils.publicationErrorText("unknown_code_xyz"), /unknown_code_xyz/);
    assert.match(utils.publicationWarningText("diff_line_not_commentable"), /总结评论/);
  } finally { await server.close(); }
});
