import type { ChangedFile, EvidenceAnchor, ReviewIssue } from "../types/review";
export function validPullRequestUrl(value: string): boolean {
  try {
    const url = new URL(value.trim());
    return url.protocol === "https:" && url.hostname === "github.com" && !url.port && !url.username && !url.password
      && /^\/[^/]+\/[^/]+\/pull\/[1-9]\d*\/?$/.test(url.pathname);
  } catch { return false; }
}
export function evidenceLabel(status: string, issue: ReviewIssue | null): string {
  if (status === "dismissed") return "已排除";
  if (["confirmed", "published"].includes(status)) return "静态核验已确认";
  if (issue?.requires_human_confirmation || status === "needs_human") return "待人工确认";
  if (["unresolved", "candidate"].includes(status)) return "证据不足 / 待核验";
  return status === "evidence_resolved" ? "仅定位，尚未核验" : `状态未知：${status}`;
}
export function severityLabel(value?: string): string {
  return ({ critical: "严重", high: "高", medium: "中", low: "低" } as Record<string, string>)[value || ""] || "未分级";
}
export function evidenceSide(evidence: EvidenceAnchor): "base" | "head" | null {
  return evidence.resolved_side || (evidence.expected_side === "either" ? null : evidence.expected_side);
}
export function fileMatches(file: ChangedFile, path: string): boolean {
  return file.file_path === path || file.old_file_path === path;
}
export function highlightedLine(evidence: EvidenceAnchor | null | undefined, oldLine?: number | null, newLine?: number | null): boolean {
  if (!evidence || evidence.resolution_status === "unresolved" || !evidence.resolved_start_line) return false;
  const side = evidenceSide(evidence);
  const line = side === "base" ? oldLine : side === "head" ? newLine : null;
  return line != null && line >= evidence.resolved_start_line && line <= (evidence.resolved_end_line || evidence.resolved_start_line);
}
