import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createServer } from 'vite';

test('评论分节保留真实问题、选择说明、快照限制和历史正文', async t => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const { parseCommentPreview } = await server.ssrLoadModule('/src/utils/commentPreview.ts');
    const variants = JSON.parse(await readFile(new URL('./fixtures/review-comment-options.json', import.meta.url), 'utf8'));
    for (const [scenario, options] of Object.entries(variants)) {
      await t.test(scenario, () => {
        for (const [option, value] of Object.entries(options)) {
          if (option.split(':').length > 2) continue; // 单条草稿不是完整交付摘要。
          const document = parseCommentPreview(value.review_comment_markdown);
          assert.ok(document, `${scenario}:${option}`);
          const sourceHeadings = [...value.review_comment_markdown.matchAll(/^## (.+)$/gm)].map(match => match[1]);
          assert.deepEqual(document.sections.map(item => item.title), sourceHeadings);
          assert.ok(document.snapshot.includes('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'));
          const renderedBody = document.sections.map(item => item.markdown).join('\n');
          for (const phrase of ['本次未包含的事项', '未通过发布准入', '已标记处理的阻断问题', '动态验证状态未知']) {
            if (value.review_comment_markdown.includes(phrase)) assert.ok(renderedBody.includes(phrase));
          }
          assert.equal(renderedBody.includes('### 审查范围'), value.detail === 'full');
        }
      });
    }
    await t.test('历史讨论前言保留原文且不导致评论退回普通 Markdown', () => {
      const preamble = '当前结论针对本轮变更范围；已参考 2 项历史讨论，另有 2 项历史事项未核验。';
      const overview = '**PR 目标：** 保留原有目标描述。\n\n**代码观察：** 保留 `lo = min(lo, point)` 与 `hi = max(hi, point)`。';
      const reminder = '- 限制：未运行测试，仅静态核对\n- 限制：未实际执行 pytest，结合 CI、业务预期和人工评审决定是否合并。';
      const snapshot = '本结论基于提交 `4d9d79bda4b1` 的只读审查；未执行目标仓库代码、测试或构建。不替代 CI 与人工审批。';
      const markdown = `RepoGuardian · PR Code Review\n\n✅ 结论：已检查范围内未发现需处理的问题\n\n${preamble}\n\n## 变更概述\n\n${overview}\n\n## 合并前提醒\n\n${reminder}\n\n---\n\n${snapshot}`;
      const document = parseCommentPreview(markdown);
      assert.ok(document);
      assert.equal(document.conclusion, '结论：已检查范围内未发现需处理的问题');
      assert.equal(document.preamble, preamble);
      assert.deepEqual(document.sections.map(item => item.title), ['变更概述', '合并前提醒']);
      assert.equal(document.sections[0].markdown.trim(), overview);
      assert.equal(document.sections[1].markdown.trim(), reminder);
      assert.equal(document.snapshot, snapshot);
      const note = '> 以下为执行失败前已保留的部分审查记录，不是完整审查结论。';
      const partial = parseCommentPreview(markdown.replace(preamble, `${note}\n\n${preamble}`));
      assert.equal(partial.preamble, `${note}\n\n${preamble}`);
      assert.ok(!partial.sections[0].markdown.includes('部分审查记录'));
    });
    await t.test('失败与取消的部分审查记录保留提示且不冒充正常章节', () => {
      for (const [verdict, note] of [
        ['❌ 结论：审查因执行错误中断（部分审查记录）', '以下为执行失败前已保留的部分审查记录'],
        ['⏹ 结论：审查已由用户停止（部分审查记录）', '以下为用户停止前已保留的部分审查记录'],
      ]) {
        const markdown = `RepoGuardian · PR Code Review

${verdict}

> ${note}，不是完整审查结论。

## 变更概述

**代码观察：** 局部观察。

## 已确认问题

本轮没有达到发布证据门槛的已确认缺陷。

## 审查局限

- 未完成范围未知。

## 建议

请重新审查。`;
        const document = parseCommentPreview(markdown);
        assert.ok(document, verdict);
        assert.equal(document.positive, false);
        assert.deepEqual(document.sections.slice(0, 4).map(item => item.title), ['变更概述', '已确认问题', '审查局限', '建议']);
        assert.ok(document.sections[0].markdown.includes('部分审查记录'));
      }
    });
    await t.test('阻断问题与建议分章计数，不将总问题数重复显示', async () => {
      const value = variants.blockers['compact:false'];
      const document = parseCommentPreview(value.review_comment_markdown);
      assert.deepEqual(document.sections.map(item => item.title), ['阻断问题', '变更理解', '其他局限', '建议']);
      const blockers = document.sections[0], suggestions = document.sections[3];
      assert.equal(blockers.findingCount, 1);
      assert.equal(suggestions.findingCount, 1);
      assert.ok(!blockers.markdown.includes('缓存改进建议'));
      assert.ok(suggestions.markdown.includes('缓存改进建议'));
      const { createSSRApp } = await import('vue');
      const { renderToString } = await import('@vue/server-renderer');
      const { default: Preview } = await server.ssrLoadModule('/src/components/report/PrCommentPreview.vue');
      const { default: Markdown } = await server.ssrLoadModule('/src/components/report/ReportMarkdown.vue');
      const render = Markdown.ssrRender;
      // 本测试只检查章节计数；正文清洗依赖浏览器 DOM，不在 Node 中执行。
      Markdown.ssrRender = () => {};
      try {
        const html = await renderToString(createSSRApp(Preview, { content: value.review_comment_markdown, findingCount: 2 }));
        assert.equal((html.match(/>1 项<\/span>/g) || []).length, 2);
        assert.ok(!html.includes('>2 项</span>'));
      } finally { Markdown.ssrRender = render; }
    });
    await t.test('旧版标题兼容，代码块与额外章节不丢失', () => {
      const markdown = 'RepoGuardian · PR Code Review\n\n❓ 结论：需要补充检查\n\n## 变更概述\n\n**实际变更：** 真实观察。\n\n## 发现的问题\n\n```md\n## 不能当成章节\n---\n```\n\n## 其他审查结果\n\n证据不足。\n\n## 额外说明\n\n保留额外段落。\n\n---\n\n本结论基于 Commit `head` 的只读静态审查。\n\nPR 已有新提交，仅适用于旧 Head。';
      const document = parseCommentPreview(markdown);
      assert.equal(document.positive, false);
      assert.equal(document.sections[0].markdown.trim(), '**代码观察：** 真实观察。');
      assert.ok(document.sections[1].markdown.includes('## 不能当成章节\n---'));
      assert.ok(document.sections[3].markdown.includes('保留额外段落'));
      assert.ok(document.snapshot.includes('PR 已有新提交'));
      for (const unknown of ['# 历史审计\n\n原文', markdown.replace('❓ 结论：需要补充检查', '未知结论'), '不可丢失的前言\n\n' + markdown]) assert.equal(parseCommentPreview(unknown), null);
    });
    await t.test('缺口前置与部分记录保留全部章节和快照', () => {
      for (const subject of ['本结论', '本记录']) {
        const markdown = `RepoGuardian · PR Code Review\n\n❓ 结论：仅适用于已检查范围\n\n## 审查局限\n\n需补查调用方。\n\n## 建议\n\n补查后重审。\n\n## 变更概述\n\n局部观察。\n\n---\n\n${subject}基于提交 \`head\`；未执行测试。`;
        const document = parseCommentPreview(markdown);
        assert.deepEqual(document.sections.map(item => item.title), ['审查局限', '建议', '变更概述']);
        assert.ok(document.snapshot.startsWith(subject));
        assert.ok(document.sections[0].markdown.includes('需补查调用方'));
      }
    });
  } finally { await server.close(); }
});
