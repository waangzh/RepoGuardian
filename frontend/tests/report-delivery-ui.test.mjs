import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createServer } from 'vite';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';

test('报告交付保留状态、快照与原始边界，代码依据不会将未决观察改为确认', async t => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const fixtures = JSON.parse(await readFile(new URL('./fixtures/publication-plans.json', import.meta.url), 'utf8'));
    const { default: Readable } = await server.ssrLoadModule('/src/components/report/ReportReadableView.vue');
    const { default: Audit } = await server.ssrLoadModule('/src/components/report/AuditSummaryView.vue');
    const { default: Context } = await server.ssrLoadModule('/src/components/report/ReportContext.vue');
    const { reportSourceLink, reportValidationLabel, coverageLabel, reportLimitations } = await server.ssrLoadModule('/src/utils/reportPresentation.ts');
    for (const name of ['clean', 'findings', 'inconclusive', 'failed', 'cancelled', 'withheld']) {
      await t.test(name, async () => {
        const { task, plan } = fixtures[name];
        const before = JSON.stringify(plan);
        const html = await renderToString(createSSRApp(Readable, { plan }));
        for (const id of ['conclusion', 'changes', 'evidence', 'gaps', 'actions']) assert.ok(html.includes(`id="rd-detail-${id}"`));
        assert.ok(html.includes('作者声明 · 尚需独立核验'));
        assert.ok(html.includes(reportValidationLabel(plan.outcome.dynamic_validation)));
        const audit = await renderToString(createSSRApp(Audit, { task, plan }));
        assert.ok(audit.includes('查看原始结构化记录'));
        assert.ok(audit.includes(plan.snapshot.head_sha));
        const context = await renderToString(createSSRApp(Context, { task, plan }));
        if (name === 'failed') assert.ok(context.includes('审查未完成'));
        if (name === 'cancelled') assert.ok(context.includes('审查已停止'));
        assert.equal(JSON.stringify(plan), before);
      });
    }
    await t.test('旧字段与缺失验证保留未知，文本按 Vue 转义', async () => {
      assert.equal(coverageLabel(undefined), '未知');
      assert.equal(reportValidationLabel(undefined), '未知');
      const plan = structuredClone(fixtures.clean.plan);
      plan.digest.change_understanding = null;
      plan.digest.author_goal = '<img src=x onerror="alert(1)">';
      const html = await renderToString(createSSRApp(Readable, { plan }));
      assert.ok(html.includes('&lt;img'));
      assert.ok(!html.includes('<img src=x'));
      plan.digest.incomplete_goals.push({ path: 'src/gap.ts', reason: '证据尚未核验' });
      assert.ok(reportLimitations(plan).some(item => item.includes('证据尚未核验')));
      const context = await renderToString(createSSRApp(Context, { task: fixtures.clean.task, plan, stale: true }));
      assert.ok(context.includes('审查结果基于旧代码版本'));
    });
    await t.test('快照链接拒绝外域、凭据、遍历和未知 SHA，保留 Base 行号', () => {
      const sha = 'a'.repeat(40), pr = 'https://github.com/owner/repo/pull/95';
      assert.equal(reportSourceLink(pr, 'src/a b.ts', sha, 12), `https://github.com/owner/repo/blob/${sha}/src/a%20b.ts#L12`);
      for (const url of ['https://evil.test/o/r/pull/95', 'javascript:alert(1)', 'https://user:secret@github.com/o/r/pull/1']) assert.equal(reportSourceLink(url, 'a.ts', sha, 1), null);
      for (const path of ['../secret', 'a/../secret', '/etc/passwd', 'a\\b', '']) assert.equal(reportSourceLink(pr, path, sha), null);
      assert.equal(reportSourceLink(pr, 'a.ts', null), null);
      assert.equal(reportSourceLink(pr, 'a.ts', 'short'), null);
      assert.equal(reportSourceLink(pr, 'a.ts', sha, -1), `https://github.com/owner/repo/blob/${sha}/a.ts`);
    });
  } finally { await server.close(); }
});
