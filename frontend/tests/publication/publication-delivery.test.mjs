import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";
import { createSSRApp, h } from "vue";
import { renderToString } from "@vue/server-renderer";

const fixtures = {findings:{plan:{review_id:"review",snapshot:{head_sha:"a".repeat(40)},review_comment_markdown:"已核验评论",inline_drafts:[],summary_findings:[]}}};

test("发布面板未启用时只说明复制可用，启用后展示问题选择、授权与预检", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true, entries: [], include: [] }, appType: "custom" });
  try {
    const { default: Panel } = await server.ssrLoadModule("/src/components/review/publication/GitHubPublicationPanel.vue");
    const { plan } = fixtures.findings;
    const render = (capability, capabilityError = false) => renderToString(createSSRApp({ render: () => h(Panel, { reviewId: plan.review_id, plan, capability, capabilityError }) }));

    const disabled = await render({ enabled: false, mode: "copy_only" });
    // 未启用时评论仍可阅读和复制，配置说明按需展开。
    for (const text of ["服务端未启用直接发布", "完整评论仍可在上方阅读和复制", "如何启用直接发布", "REPOGUARDIAN_GITHUB_PUBLISH_ENABLED"]) assert.ok(disabled.includes(text), text);
    assert.ok(!disabled.includes("2. 发布授权"));
    assert.ok(!disabled.includes("检查发布条件并预览"));

    assert.ok((await render(null)).includes("正在检查服务端是否启用直接发布"));
    assert.ok((await render(null, true)).includes("重新检查发布能力"));

    const enabled = await render({ enabled: true, mode: "publish" });
    for (const text of ["2. 发布授权", "检查发布条件并预览", "选择要发布的已核验问题"]) assert.ok(enabled.includes(text), text);
    assert.ok(!enabled.includes("服务端未启用直接发布"));
  } finally { await server.close(); }
});

test("发布结果与预检使用用户语言，不直接展示协议值", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true, entries: [], include: [] }, appType: "custom" });
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
