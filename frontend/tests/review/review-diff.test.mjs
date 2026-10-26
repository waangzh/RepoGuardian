import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";

test("Diff 保留双侧行号、原文转义和删除证据定位", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { default: component } = await server.ssrLoadModule("/src/components/review/diff/ReviewDiff.vue");
    const file = {
      file_path: "src/new.py", old_file_path: "src/old.py", change_type: "renamed", is_binary: false, additions: 1, deletions: 1,
      hunks: [{ hunk_id: "one", old_start: 7, old_length: 2, new_start: 7, new_length: 2, added_lines: [], removed_lines: [], lines: [
        { kind: "context", old_line_no: 7, new_line_no: 7, content: "    unchanged()" },
        { kind: "deleted", old_line_no: 8, new_line_no: null, content: "<script>alert('old')</script>" },
        { kind: "added", old_line_no: null, new_line_no: 8, content: "    corrected()" },
      ] }],
    };
    const evidence = { file_path: file.file_path, existing_code: "", expected_side: "either", resolution_status: "exact", resolved_side: "base", resolved_start_line: 8, resolved_end_line: 8 };
    const html = await renderToString(createSSRApp(component, { file, evidence }));
    assert.match(html, /原路径：src\/old.py/);
    assert.match(html, /@@ -7,2 \+7,2 @@/);
    assert.match(html, /aria-label="旧行号">8<\/span><span class="ln" aria-label="新行号"><\/span>/);
    assert.match(html, /<div\b(?=[^>]*data-kind="deleted")(?=[^>]*class="[^"]*\bhighlighted\b)[^>]*>/);
    assert.match(html, /data-kind="added"/);
    assert.ok(html.includes("&lt;script&gt;"));
    assert.ok(!html.includes("<script>"));
    assert.match(html, /    corrected\(\)/);
    assert.doesNotMatch(html, /没有可展示的文本差异/);
    assert.doesNotMatch(html, /aria-label="证据片段"/);

    const progressive = await renderToString(createSSRApp(component, { file, taskId: "saved", fileId: 0 }));
    assert.doesNotMatch(progressive, /没有可展示的文本差异/);
    assert.match(progressive, /向下展开 20 行上下文/);

    const binary = await renderToString(createSSRApp(component, { file: { ...file, is_binary: true, hunks: [] } }));
    assert.match(binary, /二进制文件没有文本 Diff/);
    const unresolved = await renderToString(createSSRApp(component, { evidence: { ...evidence, existing_code: "unknown()", resolution_status: "unresolved", resolved_side: null } }));
    assert.match(unresolved, /侧别未定位/);
    assert.doesNotMatch(unresolved, /aria-label="(?:旧|新)行号">\d/);
  } finally { await server.close(); }
});
