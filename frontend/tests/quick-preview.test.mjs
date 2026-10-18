import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';

const defer = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

test('快速概览取消及迟到响应不覆盖新请求，失败后可重试', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const { useQuickPreview } = await server.ssrLoadModule('/src/composables/useQuickPreview.ts');
    const first = defer(), second = defer();
    const signals = [];
    let count = 0;
    const tracker = useQuickPreview(async (url, signal) => { signals.push(signal); return ++count === 1 ? first.promise : second.promise; });
    const old = tracker.load('old');
    const next = tracker.load('new');
    assert.equal(signals[0].aborted, true);
    first.resolve({ title: 'old' });
    await old;
    assert.equal(tracker.overview.value, null);
    assert.equal(tracker.loading.value, true);
    second.resolve({ title: 'new' });
    await next;
    assert.equal(tracker.overview.value.title, 'new');
    tracker.reset();
    assert.equal(tracker.overview.value, null);
    const failed = useQuickPreview(async () => { throw Error('offline'); });
    await failed.load('url');
    assert.equal(failed.error.value, 'offline');
    failed.reset();
    assert.equal(failed.error.value, '');
  } finally { await server.close(); }
});

test('概览明确截断与非审查边界，正文转义且只能继续准备完整方案', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const { default: Page } = await server.ssrLoadModule('/src/components/pages/QuickPreviewPage.vue');
    const html = await renderToString(createSSRApp(Page, { loading: false, error: '', prUrl: 'url',
      overview: { title: '<script>title</script>', repository: 'o/r', number: 1, head_sha: 'b'.repeat(40), files_complete: false,
        files: [{ path: '<img>.py', previous_path: 'old.py', status: 'renamed', additions: 3, deletions: 1 }] } }));
    assert.match(html, /当前文件列表可能不完整/);
    assert.match(html, /尚未进行审查/);
    assert.match(html, /准备完整审查方案/);
    assert.match(html, /原路径/);
    assert.doesNotMatch(html, /<script>|<img>|确认方案并开始审查/);
    const waiting = await renderToString(createSSRApp(Page, { overview: null, loading: true, error: '', prUrl: 'url' }));
    assert.match(waiting, /取消并返回/);
    assert.match(waiting, /disabled[^>]*>准备完整审查方案/);
  } finally { await server.close(); }
});
