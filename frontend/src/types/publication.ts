export type ReviewOutcomeKind = "clean" | "findings" | "inconclusive" | "failed" | "cancelled";
export type CoverageKind = "complete" | "partial" | "unknown";

export interface ReviewOutcome {
  kind: ReviewOutcomeKind;
  coverage: CoverageKind;
  confirmed_count: number;
  publishable_count: number;
  unresolved_count: number;
  examined_goals: number;
  total_goals: number;
  incomplete_investigations: number;
  reason_codes: string[];
}

export interface PublicationSnapshot {
  head_sha: string | null;
  effective_base_sha: string | null;
}

export interface PublicationAnchor {
  file_path: string;
  start_line: number | null;
  end_line: number | null;
  side: "head" | "base" | null;
  hunk_id: string | null;
}

export interface PublicationFinding {
  finding_key: string;
  candidate_id: string;
  severity: "critical" | "high" | "medium" | "low";
  placement: "inline" | "summary";
  anchor: PublicationAnchor;
  title: string;
  body_markdown: string;
}

export interface WithheldFinding {
  candidate_id: string;
  reason_code: string;
}

export interface PublicationPlan {
  schema_version: "review-publication-v1";
  review_id: string;
  snapshot: PublicationSnapshot;
  outcome: ReviewOutcome;
  summary_markdown: string;
  user_report_markdown: string;
  inline_drafts: PublicationFinding[];
  summary_findings: PublicationFinding[];
  withheld_findings: WithheldFinding[];
  content_eligible: boolean;
  blocking_reasons: string[];
}
