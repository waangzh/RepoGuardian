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
    const { default: Workspace } = await server.ssrLoadModule('/src/components/review/ReviewWorkspace.vue');
    const { default: Modes } = await server.ssrLoadModule('/src/components/report/ReportModeTabs.vue');
    const { default: Sidebar } = await server.ssrLoadModule('/src/components/report/ReportDeliverySidebar.vue');
    const { reportSourceLink, reportValidationLabel, coverageLabel, reportLimitations } = await server.ssrLoadModule('/src/utils/reportPresentation.ts');
    await t.test('报告名称与密度说明匹配实际输出，技术记录保持高级入口', async () => {
      const { task, plan } = fixtures.clean;
      const html = await renderToString(createSSRApp(Modes, { modelValue: 'comment', panelId: 'report' }))
        + await renderToString(createSSRApp(Sidebar, {
          task, plan, mode: 'comment', detail: 'compact', excludeResolved: false, available: true,
          busy: false, copied: false, headLabel: '尚未检查', headTone: 'neutral', headNote: '',
          canCheck: true, canPublish: true,
        }));
      // 报告标签不再复用主导航“技术记录”，避免两个入口同名但内容不同。
      for (const text of ['PR 评论草稿', '详细审查报告', '技术审计导出',
        '简洁评论 · 包含所有可交付问题及处理建议', '详细评论 · 增加证据、触发场景与审查范围']) assert.ok(html.includes(text));
      for (const text of ['PR 交付摘要', '保留全部问题正文', '附加完整范围信息', '>PR Summary<']) assert.ok(!html.includes(text));
      const audit = await renderToString(createSSRApp(Audit, { task, plan }));
      assert.ok(!audit.includes('<details open'));
      assert.ok(!audit.includes('<pre>'));
    });
    for (const name of ['clean', 'findings', 'inconclusive', 'failed', 'cancelled', 'withheld']) {
      await t.test(name, async () => {
        const { task, plan } = fixtures[name];
        const before = JSON.stringify(plan);
        const html = await renderToString(createSSRApp(Readable, { plan }));
        for (const id of ['conclusion', 'changes', 'evidence', 'gaps', 'actions', 'appendix']) assert.ok(html.includes(`id="rd-detail-${id}"`));
        assert.ok(html.includes('<h4>PR 目标</h4>'));
        assert.ok(html.includes('来源与验证边界'));
        assert.ok(html.includes(reportValidationLabel(plan.outcome.dynamic_validation)));
        const audit = await renderToString(createSSRApp(Audit, { task, plan }));
        assert.ok(audit.includes('查看原始结构化记录'));
        assert.ok(audit.includes(plan.snapshot.head_sha));
        const context = await renderToString(createSSRApp(Context, { task, plan }));
        if (name === 'failed') assert.ok(context.includes('审查因执行错误中断'));
        if (name === 'cancelled') assert.ok(context.includes('审查已停止，已保留停止前的记录'));
        assert.equal(JSON.stringify(plan), before);
      });
    }
    await t.test('交互式详细报告覆盖后端未发布记录与审查附录', async () => {
      const { plan } = fixtures.withheld;
      const html = await renderToString(createSSRApp(Readable, { plan }));
      // 后端 render_user_report 会输出“机器已确认但未形成发布意见”；交互视图必须给出同一事实。
      assert.ok(html.includes('id="rd-detail-retained"'));
      assert.ok(html.includes('机器已确认但未形成发布意见'));
      assert.ok(html.includes('空返回值破坏调用方契约'));
      assert.ok(html.includes('机器已确认，发布准入未通过'));
      // 扣留原因以可读文本呈现，原始 ID 仍可追溯。
      assert.ok(html.includes('核验用量未知，不能视为完整证据'));
      assert.ok(html.includes('candidate-a'));
      // 附录覆盖范围、调查、原因代码与历史讨论复核入口。
      for (const text of ['审查范围与记录', '发布准入扣留记录']) assert.ok(html.includes(text));
      const findingsHtml = await renderToString(createSSRApp(Readable, { plan: fixtures.findings.plan }));
      assert.ok(!findingsHtml.includes('id="rd-detail-retained"'));
      assert.ok(findingsHtml.includes('id="rd-detail-appendix"'));
    });
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
      assert.ok(context.includes('PR 已更新，本轮结论不适用于最新提交'));
    });
    await t.test('新旧问题标题均按纯文本显示，不暴露 Markdown 转义或注入 HTML', async () => {
      const { publicationFindingTitle } = await server.ssrLoadModule('/src/utils/reportPresentation.ts');
      const { default: Publication } = await server.ssrLoadModule('/src/components/review/GitHubPublicationPanel.vue');
      const raw = '修复 config[0] 的校验问题';
      const encoded = '修复 config\\[0\\] 的校验问题';
      for (const legacy of [false, true]) {
        const plan = structuredClone(fixtures.findings.plan);
        const finding = [...plan.inline_drafts, ...plan.summary_findings][0];
        finding.title = legacy ? encoded : raw;
        if (legacy) delete finding.raw_title;
        else finding.raw_title = raw;
        const before = JSON.stringify(plan);
        for (const component of [Readable, Publication]) {
          // 发布能力由页面进入前探测；合成发布面板时直接声明已启用，避免异步探测影响快照。
          const html = await renderToString(createSSRApp(component, { plan, reviewId: plan.review_id,
            capability: { enabled: true, mode: 'publish' } }));
          assert.ok(html.includes(raw));
          assert.ok(!html.includes(encoded));
        }
        assert.equal(JSON.stringify(plan), before);
        finding.title = '&lt;img src=x&gt; &amp;lt; &#64;all &quot;x&quot; &#x27;y&#x27; C:\\\\config\\\\\\[0\\]';
        delete finding.raw_title;
        assert.equal(publicationFindingTitle(finding), '<img src=x> &lt; @all "x" \'y\' C:\\config\\[0]');
        const html = await renderToString(createSSRApp(Readable, { plan }));
        assert.ok(html.includes('&lt;img src=x&gt;'));
        assert.ok(!html.includes('<img src=x>'));
        finding.raw_title = String.raw`原始 \[0] &lt; <img src=x>`;
        assert.equal(publicationFindingTitle(finding), finding.raw_title);
      }
    });
    await t.test('旧目标包装只影响展示，来源、输入缺口与代码观察仍保留', async () => {
      const plan = structuredClone(fixtures.findings.plan);
      plan.digest.change_understanding.author_goal = '作者声明（未经独立验证）：修复缓存读取。';
      plan.digest.author_context_status = 'truncated';
      plan.digest.author_context_limitation = '作者输入已截断，尾部尚未核验。';
      const before = JSON.stringify(plan);
      const html = await renderToString(createSSRApp(Readable, { plan }));
      assert.ok(html.includes('<h4>PR 目标</h4><p>修复缓存读取。</p>'));
      assert.ok(!html.includes('作者声明（未经独立验证）：'));
      assert.equal(html.split('作者输入已截断，尾部尚未核验。').length - 1, 1);
      assert.ok(html.indexOf('来源与验证边界') < html.indexOf('作者输入已截断'));
      assert.equal(html.split('未执行目标仓库代码、测试或构建。').length - 1, 1);
      assert.ok(html.includes('不代表目标已实现'));
      assert.equal(JSON.stringify(plan), before);
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
    await t.test('报告加载期间仍保留通用 PR 头部和结果导航', async () => {
      const task = structuredClone(fixtures.clean.task);
      task.pr = { owner: 'fixture', repo: 'review-ui', number: 95, title: '统一审查页面标题', html_url: 'https://github.com/fixture/review-ui/pull/95',
        base: { ref: 'main' }, head: { ref: 'feature/report' } };
      for (const section of ['report', 'files', 'publish']) {
        const html = await renderToString(createSSRApp(Workspace, { task, report: null, section, statusText: '审查已完成', duration: '13 秒', cancelling: false, retryingUnitId: null }));
        assert.equal(html.match(/class="pr-title"/g)?.length, 1);
        assert.equal(html.match(/统一审查页面标题/g)?.length, 1);
        assert.ok(html.includes('aria-label="审查结果导航"'));
        assert.ok(html.includes('>新建审查</button>'));
        assert.match(html, new RegExp(`aria-current="page"[^>]*>${({ report: '审查报告', files: '变更文件', publish: '交付到 GitHub' })[section]}`));
      }
    });
    await t.test('嵌入式快照卡片保留结论与 Head，不重复 PR 标题和任务状态', async () => {
      const task = { ...fixtures.clean.task, pr: { title: '避免重复的PR标题', owner: 'fixture', repo: 'review-ui', number: 95, base: { ref: 'main' }, head: { ref: 'feature/report' } } };
      const plan = fixtures.clean.plan;
      const embedded = await renderToString(createSSRApp(Context, { task, plan, embedded: true }));
      assert.ok(!embedded.includes('避免重复的PR标题'));
      assert.ok(!embedded.includes('rd-context-top'));
      assert.ok(embedded.includes(plan.snapshot.head_sha.slice(0, 12)));
      assert.ok(embedded.includes('检查覆盖：完整'));
      const standalone = await renderToString(createSSRApp(Context, { task, plan }));
      assert.ok(standalone.includes('避免重复的PR标题'));
      assert.ok(standalone.includes('rd-context-top'));
    });
  } finally { await server.close(); }
});
