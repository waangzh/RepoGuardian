import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";

test("变更文件目录按层级分组、合并单路径并保留精确选择路径", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { buildFileTree } = await server.ssrLoadModule("/src/utils/fileTree.ts");
    const paths = ["tests/test_stats.py", "README.md", "src/clickllm/prove/stats.py"];
    const tree = buildFileTree(paths);
    assert.deepEqual(tree.map(node => node.label), ["src/clickllm/prove", "tests", "README.md"]);
    assert.equal(tree[0].children[0].label, "stats.py");
    assert.equal(tree[0].children[0].path, paths[2]);
    assert.deepEqual(buildFileTree([]), []);

    const branches = buildFileTree(["src/a.ts", "src/lib/x.ts", "src/lib/y.ts", "src/a.ts"]);
    assert.equal(branches[0].label, "src");
    assert.deepEqual(branches[0].children.map(n => n.label), ["lib", "a.ts"]);
    assert.equal(branches[0].children[0].children.length, 2);
    assert.equal(buildFileTree(["A.py", "a.py"]).length, 2);
    assert.equal(buildFileTree(["old", "old/new.txt"]).length, 2);

    const { default: component } = await server.ssrLoadModule("/src/components/review/diff/ReviewFileTree.vue");
    const html = await renderToString(createSSRApp(component, { nodes: tree, selected: paths[2] }));
    assert.match(html, /<summary[^>]*title="src\/clickllm\/prove"/);
    assert.match(html, /aria-label="src\/clickllm\/prove\/stats.py" aria-current="true"/);
    assert.match(html, />stats.py<\/span>/);
    assert.doesNotMatch(html, /aria-current="true"[^>]*title="tests\/test_stats.py"/);
    const escaped = await renderToString(createSSRApp(component, {
      nodes: buildFileTree(["src/<script>.ts"]), selected: "",
    }));
    assert.ok(escaped.includes("&lt;script&gt;.ts"));
    assert.ok(!escaped.includes("<script>"));
  } finally { await server.close(); }
});
