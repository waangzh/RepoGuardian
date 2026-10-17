import type { ChangedFile, DiffLine } from "../types/review";

export interface FileContext {
  file_id: number;
  file_path: string;
  snapshot: { head_sha: string; effective_base_sha: string; declared_base_sha: string };
  base_lines: string[];
  head_lines: string[];
}
export interface DiffGap {
  type: "gap";
  key: string;
  oldStart: number;
  newStart: number;
  count: number | null;
  lines: DiffLine[] | null;
  up: boolean;
  down: boolean;
}
export type DiffDisplayRow = { type: "line"; line: DiffLine } | DiffGap;
export type ExpandedContext = Record<string, { up: number; down: number }>;
const lineRow = (line: DiffLine): DiffDisplayRow => ({ type: "line", line });

function fold(lines: DiffLine[], expanded: ExpandedContext): DiffDisplayRow[] {
  const result: DiffDisplayRow[] = [];
  for (let i = 0; i < lines.length;) {
    if (lines[i]!.kind !== "context") { result.push(lineRow(lines[i++]!)); continue; }
    const start = i;
    while (i < lines.length && lines[i]!.kind === "context") i++;
    const keepStart = start > 0 ? Math.min(3, i - start) : 0;
    const keepEnd = i < lines.length ? Math.min(3, i - start - keepStart) : 0;
    const hidden = lines.slice(start + keepStart, i - keepEnd);
    result.push(...lines.slice(start, start + keepStart).map(lineRow));
    if (hidden.length) {
      const key = `${hidden[0]!.old_line_no}:${hidden[0]!.new_line_no}`;
      const state = expanded[key] || { up: 0, down: 0 };
      const down = Math.min(state.down, hidden.length);
      const up = Math.min(state.up, hidden.length - down);
      result.push(...hidden.slice(0, down).map(lineRow));
      const remaining = hidden.slice(down, hidden.length - up);
      if (remaining.length) result.push({ type: "gap", key, oldStart: remaining[0]!.old_line_no!,
        newStart: remaining[0]!.new_line_no!, count: remaining.length, lines: hidden,
        up: i < lines.length, down: start > 0 });
      result.push(...(up ? hidden.slice(-up) : []).map(lineRow));
    }
    result.push(...lines.slice(i - keepEnd, i).map(lineRow));
  }
  return result;
}

/** Extra lines must match BOTH committed versions; a partial/stale diff is never bridged. */
export function completeDiff(file: ChangedFile, source: FileContext): DiffLine[] {
  const result: DiffLine[] = [];
  let old = 1, next = 1;
  const gap = (oldEnd: number, newEnd: number) => {
    if (oldEnd - old !== newEnd - next || oldEnd < old || newEnd < next) throw new Error("源码与 Diff 范围不一致");
    while (old < oldEnd) {
      const content = source.base_lines[old - 1];
      if (content === undefined || content !== source.head_lines[next - 1]) throw new Error("两侧上下文不一致，无法安全展开");
      result.push({ kind: "context", old_line_no: old++, new_line_no: next++, content });
    }
  };
  for (const hunk of file.hunks) {
    gap(hunk.old_start + (hunk.old_length === 0 ? 1 : 0), hunk.new_start + (hunk.new_length === 0 ? 1 : 0));
    let oldCount = 0, newCount = 0;
    for (const line of hunk.lines) {
      if (line.kind !== "added") {
        if (line.old_line_no !== old || source.base_lines[old - 1] !== line.content) throw new Error("Base 原文与 Diff 不一致");
        old++; oldCount++;
      }
      if (line.kind !== "deleted") {
        if (line.new_line_no !== next || source.head_lines[next - 1] !== line.content) throw new Error("Head 原文与 Diff 不一致");
        next++; newCount++;
      }
      result.push(line);
    }
    if (oldCount !== hunk.old_length || newCount !== hunk.new_length) throw new Error("Diff 区块不完整");
  }
  gap(source.base_lines.length + 1, source.head_lines.length + 1);
  return result;
}

export function diffDisplayRows(file: ChangedFile, source: FileContext | null, expanded: ExpandedContext): DiffDisplayRow[] {
  if (source) return fold(completeDiff(file, source), expanded);
  const result: DiffDisplayRow[] = [];
  let old = 1, next = 1;
  for (const hunk of file.hunks) {
    const count = hunk.old_start - old;
    if (count > 0 && count === hunk.new_start - next) result.push({ type: "gap", key: `${old}:${next}`,
      oldStart: old, newStart: next, count, lines: null, up: true, down: result.length > 0 });
    result.push(...fold(hunk.lines, expanded));
    old = hunk.old_start + hunk.old_length;
    next = hunk.new_start + hunk.new_length;
  }
  if (file.hunks.length && !["added", "deleted"].includes(file.change_type)) result.push({ type: "gap", key: `${old}:${next}`,
    oldStart: old, newStart: next, count: null, lines: null, up: false, down: true });
  const merged: DiffDisplayRow[] = [];
  for (const row of result) {
    const previous = merged[merged.length - 1];
    if (row.type === "gap" && previous?.type === "gap" && previous.count !== null &&
        previous.oldStart + previous.count === row.oldStart && previous.newStart + previous.count === row.newStart) {
      // A saved hunk boundary is not a separate context boundary. Fetch the full
      // snapshot before expanding a range that includes unsaved context.
      previous.count = row.count === null ? null : previous.count + row.count;
      previous.lines = null;
      previous.up ||= row.up;
      previous.down ||= row.down;
    } else merged.push(row);
  }
  return merged;
}
