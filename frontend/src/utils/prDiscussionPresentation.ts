import type { DiscussionItem, DiscussionRecheck, DiscussionStats } from "../types/prDiscussion";

export const recheckLabels: Record<DiscussionRecheck["status"], string> = {
  still_present: "问题仍存在", not_present: "当前主张不成立", fixed: "已核验修复",
  insufficient_evidence: "证据不足", not_checked: "尚未核验", background: "背景说明",
};
export const sourceLabels = { conversation: "PR 普通讨论", review: "Review 审查意见", inline: "行级审查评论" };
export type DiscussionFilter = "all" | "unverified" | "verified" | "inline";
export function recheckLabel(status: string) { return recheckLabels[status as DiscussionRecheck["status"]] || "未知核验状态"; }
export function recheckTone(status: string) {
  return ({ still_present: "red", not_present: "teal", fixed: "teal",
    insufficient_evidence: "amber", not_checked: "gray", background: "blue" } as Record<string, string>)[status] || "gray";
}
export function safeDiscussionLink(value?: string | null) {
  try {
    const url = new URL(value || "");
    return url.protocol === "https:" && url.hostname === "github.com" && !url.username && !url.password && !url.port ? url.href : undefined;
  } catch { return undefined; }
}
export function discussionTitle(item: DiscussionItem) {
  const excerpt = item.comments[0]?.body.replace(/^\s*#+\s*/gm, "").trim().split(/\r?\n/).find(line => line.trim());
  return excerpt ? excerpt.trim().slice(0, 110) : `${sourceLabels[item.source] || "历史讨论"}${item.path ? ` · ${item.path}` : " · 整个 PR"}`;
}
export function matchesDiscussion(item: DiscussionItem, filter: DiscussionFilter, query: string) {
  const status = item.recheck.status;
  if (filter === "verified" && !["still_present", "fixed", "not_present"].includes(status)) return false;
  if (filter === "unverified" && !["insufficient_evidence", "not_checked"].includes(status)) return false;
  if (filter === "inline" && item.source !== "inline") return false;
  return [item.id, item.path, item.recheck.reason, ...item.comments.map(c => `${c.author} ${c.body}`),
    ...Object.values(item.summary || {}).flatMap(v => Array.isArray(v) ? v : [])].join(" ").toLowerCase().includes(query.trim().toLowerCase());
}
export function discussionNotice(stats?: DiscussionStats | null) {
  if (!stats || stats.status === "not_recorded") return "未记录历史讨论。这是旧任务或未采集记录，不代表没有评论。新建审查才会采集，恢复任务不会补采。";
  if (stats.status === "unavailable") return "讨论采集不可用，无法判断该 PR 是否存在历史讨论。请检查采集限制与数据来源。";
  if (stats.status === "partial") return "讨论采集不完整。仅展示本轮已保存的线程，不应将未采集的讨论视为不存在。";
  if (stats.collected === 0) return "采集已完成，未发现可用的 PR 讨论线程。这不是代码安全的证明。";
  return "本页区分 GitHub 线程状态与当前代码核验结果。历史讨论不能代替代码证据，也不会自动成为本轮确认问题。";
}
