import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';

test('完整旧结果、原预算续查与强制全新入口保留各自语义', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const { default: component } = await server.ssrLoadModule('/src/components/pages/ReviewPlanPage.vue');
    const props = {
      preview: { preview_id: 'saved', changed_files: [], review_units: [], excluded_files: [], risk_tags: [], warnings: [], included_file_count: 0, changed_file_count: 0, existing_review_id: 'a'.repeat(32) },
      capabilities: null, prUrl: 'https://github.com/o/r/pull/1', model: '', models: [], defaultModel: 'fixture', modelsLoading: false, modelsError: null, submitting: false, dirty: false, previewing: false,
    };
    const saved = await renderToString(createSSRApp(component, props));
    assert.match(saved, /查看已保存结果/);
    assert.match(saved, /历史讨论以该记录保存时为准/);
    assert.doesNotMatch(saved, /开始 AI 审查/);
    assert.match(saved, /重新执行 AI 审查（不复用已有结果）/);
    assert.ok(saved.indexOf('重新执行 AI 审查（不复用已有结果）') < saved.indexOf('rg-plan-advanced'));
    assert.match(saved, /仍然重新审查/);
    assert.doesNotMatch(saved, /本次方案已设为重新审查/);
    for (const busy of ['submitting', 'previewing']) {
      props[busy] = true;
      const blocked = await renderToString(createSSRApp(component, props));
      assert.match(blocked, /<button[^>]*disabled[^>]*>仍然重新审查<\/button>/);
      props[busy] = false;
    }
    props.preview.existing_review_id = null;
    props.preview.continuation = { review_id: 'b'.repeat(32), can_resume: true, unit_ids: ['u'], reason: 'available', calls_consumed: 3, max_calls: 5, tokens_consumed: 200, max_tokens: 2000, remaining_seconds: 60 };
    const resumable = await renderToString(createSSRApp(component, props));
    assert.match(resumable, /继续未完成审查/);
    assert.ok(resumable.includes('已用 3 / 5 次调用、200 / 2000 Tokens'));
    assert.match(resumable, /原预算、期限和纠错次数/);
    assert.doesNotMatch(resumable, /开始 AI 审查/);
    props.preview.continuation.can_resume = false;
    props.preview.continuation.reason = 'deadline';
    const expired = await renderToString(createSSRApp(component, props));
    assert.match(expired, /原审查期限已到/);
    assert.doesNotMatch(expired, />继续未完成审查</);
    assert.match(expired, /开始 AI 审查/);
    props.preview.continuation = null;
    props.preview.force_full_review = true;
    props.forceFullReview = true;
    const fresh = await renderToString(createSSRApp(component, props));
    assert.match(fresh, /本次方案已设为重新审查，确认后将重新调用 AI/);
    assert.match(fresh, /<input[^>]*checked[^>]*aria-describedby="fresh-review-hint"/);
    assert.match(fresh, /开始 AI 审查/);
    assert.doesNotMatch(fresh, /仍然重新审查|查看已保存结果/);
  } finally { await server.close(); }
});

test('强制全新标记与续查快照从客户端传到 API', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  const originalFetch = globalThis.fetch;
  const sent = [];
  globalThis.fetch = async (url, options) => {
    sent.push({ url, body: JSON.parse(options.body) });
    return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } });
  };
  try {
    const { createReview, resumeReview } = await server.ssrLoadModule('/src/api/client.ts');
    const preview = { preview_id: 'preview', plan_hash: 'plan', force_full_review: true, mode: 'review', validation_backend: { name: 'none' }, snapshot: { head_sha: 'head', effective_base_sha: 'base' } };
    await createReview('https://github.com/o/r/pull/1', '', 'review', 'none', preview);
    assert.equal(sent[0].body.force_full_review, true);
    await resumeReview('source', 'https://github.com/o/r/pull/1', preview);
    assert.match(sent[1].url, /\/source\/resume$/);
    assert.equal(sent[1].body.preview_id, 'preview');
    assert.equal(sent[1].body.expected_head_sha, 'head');
    assert.equal(sent[1].body.expected_effective_base_sha, 'base');
    assert.equal(sent[1].body.force_full_review, undefined);
  } finally { globalThis.fetch = originalFetch; await server.close(); }
});
