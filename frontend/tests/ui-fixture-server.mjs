// Browser-only fixtures. This server never loads production credentials or calls a model/GitHub.
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { resolve, extname, sep } from "node:path";
const fixtures = JSON.parse(await readFile(new URL("./fixtures/publication-plans.json", import.meta.url), "utf8"));
const tasks = new Map(Object.values(fixtures).map(value => [value.task.id, structuredClone(value.task)]));
const issue = structuredClone(fixtures.findings.task.task_runtime_report.findings[0].issue);
issue.status = "confirmed";
const changedFile = { file_path: 'src/a.py', change_type: 'modified', additions: 1, deletions: 1, is_binary: false,
  hunks: [{ old_start: 6, old_length: 2, new_start: 6, new_length: 2, hunk_id: 'h1', added_lines: [], removed_lines: [], lines: [
    { kind: 'context', content: 'def result():', old_line_no: 6, new_line_no: 6 },
    { kind: 'deleted', content: '    return {}', old_line_no: 7, new_line_no: null },
    { kind: 'added', content: '    return None', old_line_no: null, new_line_no: 7 },
  ] }] };
for (const task of tasks.values()) {
  task.pr ||= { owner: 'fixture', repo: 'review-ui', number: 95, title: '静态证据检查', html_url: 'https://github.com/fixture/review-ui/pull/95', clone_url: '', base: { ref: 'main', sha: 'c'.repeat(40), repo_clone_url: '' }, head: { ref: 'test-fixture', sha: 'a'.repeat(40), repo_clone_url: '' } };
  task.changed_files = [structuredClone(changedFile)];
  task.pr.body = '浏览器验收夹具：仅用于界面交互测试。';
  task.pr.title = `[界面测试夹具] ${task.pr.title}`;
  task.steps = [{ name: 'intake', status: 'completed', message: '已读取测试 PR' }, { name: 'task_review_runtime', status: 'completed', message: '已完成静态检查' }];
}
const findings = tasks.get(fixtures.findings.task.id);
findings.issues = [issue];
const human = structuredClone(issue);
Object.assign(human, { id: 'human-candidate', title: '业务预期待人工确认', status: 'unresolved', requires_human_confirmation: true });
const uncertain = { key: 'no-issue', status: 'unresolved', candidate: { claim: '没有完整 Issue 的待核验候选', severity: 'low' }, reason: 'candidate_derivation_incomplete', issue: null };
findings.issues.push(human);
findings.task_runtime_report.findings.push({ key: 'human', status: 'unresolved', issue: human }, uncertain);
findings.task_runtime_report.initial_scope_coverage.push({ goal_id: 'g2', target_ref: 'tests/test_a.py', status: 'not_examined', reason: '测试夹具中的未完成目标' });
let run = null;
const streams = new Set();
const requests = [];
const dist = resolve(fileURLToPath(new URL('../dist/', import.meta.url)));
const type = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };
function json(response, value, status = 200) { response.writeHead(status, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(value)); }
const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url, 'http://localhost');
    const path = url.pathname;
    let body = '';
    for await (const chunk of request) body += chunk;
    requests.push({ method: request.method, path, body: body ? JSON.parse(body) : null });
    if (path === '/__test/requests') return json(response, requests);
    if (path === '/__test/complete') {
      run = structuredClone(findings); run.id = 'ui-running';
      for (const stream of streams) { stream.write('event: done\ndata: {"status":"completed"}\n\n'); stream.end(); }
      streams.clear(); return json(response, { completed: true });
    }
    if (path === '/api/system/models') return json(response, { provider: 'test-fixture', default_model: 'fixture-default', models: [{ id: 'fixture-default' }, { id: 'fixture-other' }] });
    if (path === '/api/system/diagnostics') return json(response, { version: 'test-fixture', provider: 'fixture', default_model: 'fixture-default', database_schema_current: true, worker_status: 'idle', artifact_directory_writable: true, langsmith_enabled: false, security_mode: 'restricted', patch_max_files: 3, patch_max_changed_lines: 100, retention_days: 30, configured_secrets: {}, versions: {} });
    if (path === '/api/reviews/preview') return json(response, { mode: 'review', changed_file_count: 1, included_file_count: 1, changed_files: [{ ...changedFile, classifications: ['Python'], included: true }], review_units: [], excluded_files: [], matched_rules: [], risk_tags: ['跨模块'], planning_model_calls: 0, estimated_model_calls: 1, max_model_calls: 10, estimated_tokens: 1000, patch_generation_enabled: false, validation_backend: { name: 'none', available: true }, warnings: [] });
    if (path === '/api/reviews' && request.method === 'POST') {
      run = structuredClone(findings); Object.assign(run, { id: 'ui-running', status: 'reviewing', model: JSON.parse(body).model });
      run.steps[1].status = 'running'; run.task_runtime_report.execution_status = 'running';
      return json(response, { task_id: run.id, status: 'queued' }, 202);
    }
    if (path === '/api/reviews') {
      const items = [...tasks.values()].filter(task => !url.searchParams.get('status') || task.status === url.searchParams.get('status'));
      const size = Number(url.searchParams.get('page_size') || 20), page = Number(url.searchParams.get('page') || 1);
      return json(response, { items: items.slice((page - 1) * size, page * size), total: items.length, page, page_size: size });
    }
    const match = path.match(/^\/api\/reviews\/([^/]+)(.*)$/);
    if (match) {
      const [, id, action] = match;
      const task = id === 'ui-running' ? run : tasks.get(id);
      if (!task) return json(response, { detail: 'not_found' }, 404);
      if (action === '/stream') { response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' }); response.write(': fixture stream\n\n'); streams.add(response); request.on('close', () => streams.delete(response)); return; }
      if (action === '/cancel') { run.status = 'cancelled'; run.task_runtime_report.execution_status = 'cancelled'; return json(response, { task_id: id, status: 'cancelled' }); }
      const fixture = Object.values(fixtures).find(value => value.task.id === id) || fixtures.findings;
      if (action === '/publication-summary') return json(response, { ...fixture.summary, review_id: id });
      if (action === '/publication-plan') return json(response, { ...fixture.plan, review_id: id, user_report_markdown: `${fixture.plan.user_report_markdown}\n\n<img src=x onerror="window.__unsafeReport=true">` });
      if (action === '/report') { response.writeHead(200, { 'Content-Type': 'text/plain' }); return response.end(task.report_markdown || '# 测试审计报告'); }
      if (action === '/publication-preflight') return json(response, { ready: true, review_id: id, plan_hash: 'fixture-hash', head_sha: task.pr.head.sha, head_sha_matches: true, pr_open: true, permissions_ok: true, summary_action: 'create', summary_comment_id: null, finding_keys: ['key-a'], inline: { selected: 1, ready: 1, already_published: 0, summary_fallback: 0, blocked: 0 }, inline_previews: [], summary_markdown: '# 测试预检摘要', requires_partial_acknowledgement: false, warnings: [] });
      if (action === '/publications' && request.method === 'GET') return json(response, []);
      if (action === '/publications') return json(response, { detail: { code: 'publication_disabled' } }, 403);
      if (!action) return json(response, task);
    }
    const file = resolve(dist, path === '/' ? 'index.html' : `.${decodeURIComponent(path)}`);
    if (file !== dist && !file.startsWith(dist + sep)) return json(response, { error: 'outside_fixture_root' }, 403);
    const bytes = await readFile(file);
    response.writeHead(200, { 'Content-Type': type[extname(file)] || 'application/octet-stream' }); response.end(bytes);
  } catch (error) { json(response, { error: error.message }, 500); }
});
const port = Number(process.env.RG_UI_FIXTURE_PORT || 4191);
server.listen(port, '127.0.0.1', () => console.log(`UI fixture server http://127.0.0.1:${port} (no external writes)`));
