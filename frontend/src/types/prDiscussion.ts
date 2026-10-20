export interface DiscussionStats {
  status: "not_recorded" | "complete" | "partial" | "unavailable";
  collected: number; comments: number; referenced: number; referenced_comments: number;
  checked: number; unchecked: number; limitations: string[]; termination_reason: string | null;
}
export interface DiscussionRecheck {
  discussion_id: string;
  status: "still_present" | "not_present" | "fixed" | "insufficient_evidence" | "not_checked" | "background";
  reason: string; evidence_ids: string[]; limitations: string[];
}
export interface DiscussionItem {
  id: string; source: "conversation" | "review" | "inline"; path: string | null;
  resolved: boolean | null; outdated: boolean | null; complete: boolean; referenced: boolean;
  comment_count: number; url: string | null;
  comments: { id: string; body: string; author: string; url: string | null; created_at: string | null }[];
  summary: { claims: string[]; conditions: string[]; objections: string[]; repair_claims: string[];
    decisions: string[]; open_questions: string[]; quotes: Record<string, string>;
    partial: boolean; omitted_comment_ids: string[] } | null;
  recheck: DiscussionRecheck;
  evidence: { id?: string; evidence_id?: string; file_path: string; start_line: number; end_line: number;
    content: string; side?: "head" | "base" }[];
}
export interface DiscussionPage {
  stats: DiscussionStats; items: DiscussionItem[]; total: number; page: number; page_size: number;
}
