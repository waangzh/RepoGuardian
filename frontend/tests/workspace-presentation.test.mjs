import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";
const source = readFileSync(new URL("../src/utils/workspacePresentation.ts", import.meta.url), "utf8");
const javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const { validPullRequestUrl, evidenceLabel, highlightedLine, fileMatches } = await import(`data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`);

test("PR 导入拒绝伪造域名、凭据、协议和无效路径", () => {
  assert.equal(validPullRequestUrl(" https://github.com/owner/repo/pull/123 "), true);
  for (const value of ["http://github.com/a/b/pull/1", "https://github.com.evil.test/a/b/pull/1", "https://user:pass@github.com/a/b/pull/1", "https://github.com/a/b/pull/0", "https://github.com/a/b/issues/1", "https://github.com/a/b/pull/1/files", "javascript:alert(1)"]) assert.equal(validPullRequestUrl(value), false, value);
});
test("证据已定位不冒充核验，未决与未知不冒充已确认", () => {
  assert.match(evidenceLabel("evidence_resolved", null), /尚未核验/);
  assert.match(evidenceLabel("unresolved", null), /证据不足/);
  assert.match(evidenceLabel("candidate", { requires_human_confirmation: true }), /人工确认/);
  assert.match(evidenceLabel("new_status", null), /未知/);
  assert.match(evidenceLabel("confirmed", null), /静态核验/);
});
test("删除证据按 Base 行高亮，未知侧别和未定位证据不能高亮", () => {
  const evidence = { expected_side: "base", resolved_side: "base", resolution_status: "exact", resolved_start_line: 88, resolved_end_line: 89 };
  assert.equal(highlightedLine(evidence, 88, null), true);
  assert.equal(highlightedLine(evidence, 87, 88), false);
  assert.equal(highlightedLine({ ...evidence, resolved_side: null, expected_side: "either" }, 88, 88), false);
  assert.equal(highlightedLine({ ...evidence, resolution_status: "unresolved" }, 88, 88), false);
  assert.equal(highlightedLine(null, 88, 88), false);
});
test("重命名文件保留旧路径发现关联", () => {
  assert.equal(fileMatches({ file_path: "new.py", old_file_path: "old.py" }, "old.py"), true);
  assert.equal(fileMatches({ file_path: "new.py", old_file_path: "old.py" }, "else.py"), false);
});
