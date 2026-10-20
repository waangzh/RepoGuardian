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
          assert.equal(document.sections.some(item => item.title === '审查范围'), value.detail === 'full');
        }
      });
    }
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
