import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";

test("渐进上下文保留变更、每次20行、展开至边界并拒绝不一致源码", async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  try {
    const { completeDiff, diffDisplayRows } = await server.ssrLoadModule("/src/utils/diffContext.ts");
    const base = Array.from({ length: 100 }, (_, i) => `line ${i + 1}`);
    const head = [...base]; head[49] = "changed";
    const lines = [47,48,49].map(n => ({ kind: "context", old_line_no: n, new_line_no: n, content: base[n-1] }));
    lines.push({ kind: "deleted", old_line_no: 50, content: base[49] }, { kind: "added", new_line_no: 50, content: "changed" });
    lines.push(...[51,52,53].map(n => ({ kind: "context", old_line_no: n, new_line_no: n, content: base[n-1] })));
    const file = { change_type: "modified", hunks: [{ old_start: 47, old_length: 7, new_start: 47, new_length: 7, lines }] };
    const source = { base_lines: base, head_lines: head };
    const full = completeDiff(file, source);
    assert.equal(full.length, 101);
    const initial = diffDisplayRows(file, source, {});
    assert.equal(initial.filter(r => r.type === "line").length, 8);
    const gaps = initial.filter(r => r.type === "gap");
    assert.deepEqual(gaps.map(g => g.count), [46,47]);
    const expanded = { [gaps[0].key]: { up: 20, down: 0 } };
    assert.equal(diffDisplayRows(file, source, expanded).filter(r => r.type === "line").length, 28);
    expanded[gaps[0].key].up = 100;
    expanded[gaps[1].key] = { up: 0, down: 100 };
    const all = diffDisplayRows(file, source, expanded);
    assert.equal(all.filter(r => r.type === "gap").length, 0);
    assert.deepEqual(all.map(r => r.line), full);
    assert.equal(diffDisplayRows(file, null, {}).filter(r => r.type === "gap").length, 2);
    const stale = { ...source, head_lines: [...head] }; stale.head_lines[0] = "wrong snapshot";
    assert.throws(() => completeDiff(file, stale), /不一致/);
    assert.throws(() => completeDiff(file, { ...source, base_lines: base.slice(0, 40) }), /不一致/);
    const added = { change_type: "added", hunks: [{ old_start: 0, old_length: 0, new_start: 1, new_length: 1,
      lines: [{ kind: "added", new_line_no: 1, content: "new" }] }] };
    assert.equal(completeDiff(added, { base_lines: [], head_lines: ["new"] }).length, 1);
    const deleted = { change_type: "deleted", hunks: [{ old_start: 1, old_length: 1, new_start: 0, new_length: 0,
      lines: [{ kind: "deleted", old_line_no: 1, content: "old" }] }] };
    assert.equal(completeDiff(deleted, { base_lines: ["old"], head_lines: [] }).length, 1);

    const headTwo = [...base]; headTwo[19] = "change 20"; headTwo[79] = "change 80";
    const hunks = [20,80].map(position => ({ old_start: position-3, new_start: position-3, old_length: 7, new_length: 7,
      lines: Array.from({length:7},(_,i)=>position-3+i).flatMap(n => n===position ? [
        {kind:"deleted",old_line_no:n,content:base[n-1]}, {kind:"added",new_line_no:n,content:headTwo[n-1]}] :
        [{kind:"context",old_line_no:n,new_line_no:n,content:base[n-1]}]) }));
    const two = {...file,hunks}, sourceTwo = {...source,head_lines:headTwo};
    const twoGaps = diffDisplayRows(two, sourceTwo, {}).filter(r=>r.type==="gap");
    assert.equal(twoGaps.length,3);
    const merged = Object.fromEntries(twoGaps.map(g=>[g.key,{up:100,down:100}]));
    const mergedRows = diffDisplayRows(two,sourceTwo,merged);
    assert.equal(mergedRows.length,102);
    assert.equal(mergedRows.filter(r=>r.type==="line"&&r.line.kind!=="context").length,4);
    assert.equal(new Set(mergedRows.filter(r=>r.line.old_line_no).map(r=>r.line.old_line_no)).size,100);

    // Long saved context must join the adjacent, not-yet-loaded prefix/tail.
    const longBase = Array.from({ length: 1000 }, (_, i) => `line ${i + 1}`);
    const longHead = [...longBase]; longHead[439] = "changed";
    const longFile = { change_type: "modified", hunks: [{ old_start: 360, new_start: 360, old_length: 161, new_length: 161,
      lines: Array.from({ length: 161 }, (_, i) => i + 360).flatMap(n => n === 440 ? [
        { kind: "deleted", old_line_no: n, content: longBase[n - 1] },
        { kind: "added", new_line_no: n, content: longHead[n - 1] },
      ] : [{ kind: "context", old_line_no: n, new_line_no: n, content: longBase[n - 1] }]) }] };
    const pending = diffDisplayRows(longFile, null, {});
    const pendingGaps = pending.filter(r => r.type === "gap");
    assert.deepEqual(pendingGaps.map(g => [g.oldStart, g.count, g.up, g.down, g.lines]), [
      [1, 436, true, false, null], [444, null, false, true, null],
    ]);
    assert.equal(pending.filter(r => r.type === "line").length, 8);
    const longSource = { base_lines: longBase, head_lines: longHead };
    const loadedGaps = diffDisplayRows(longFile, longSource, {}).filter(r => r.type === "gap");
    assert.deepEqual(loadedGaps.map(g => g.key), pendingGaps.map(g => g.key));
    for (const [index, direction] of [[0, "up"], [1, "down"]]) {
      for (const count of [20, 40]) {
        const state = { [pendingGaps[index].key]: { up: 0, down: 0, [direction]: count } };
        const rows = diffDisplayRows(longFile, longSource, state);
        assert.equal(rows.filter(r => r.type === "line").length, 8 + count);
        assert.equal(rows.filter(r => r.type === "gap").length, 2);
      }
    }
  } finally { await server.close(); }
});
