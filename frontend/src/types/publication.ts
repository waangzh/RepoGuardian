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
  pending_limitation_count: number;
  review_limitations: import('./review').ReviewLimitation[];
  reason_codes: string[];
  finding_conclusion: "confirmed" | "unresolved" | "no_confirmed_findings" | "unknown";
  review_completeness: CoverageKind;
  runtime_reliability: "reliable" | "unreliable" | "unknown";
}

export interface DigestGoal {
  goal_id: string | null;
  path: string | null;
  start_line: number | null;
  end_line: number | null;
  side: "head" | "base" | null;
  status: string;
  reason: string | null;
}
export interface DigestExcludedFile {
  path: string;
  reason: string;
}
export interface ChangeUnderstanding {
  author_goal: string;
  actual_changes: string;
  alignment: string;
  author_goal_source?: "summary" | "description" | "title" | "unknown" | null;
  diff_summary?: string | null;
  implementation_changes?: string | null;
  test_observations?: string | null;
}
export interface ReviewDigest {
  discussion_review_enabled?: boolean | null;
  discussion_stats?: {status:string;collected:number;limitations:string[]} | null;
  pr_url?: string | null;
  author_goal?: string | null;
  author_context_status?: "complete" | "truncated" | "unknown" | "not_recorded";
  author_context_limitation?: string | null;
  actual_changes?: string[];
  change_understanding?: ChangeUnderstanding | null;
  findings?: import('./reviewPlan').FindingView[];
  execution_status?: string | null;
  examined_goals: DigestGoal[];
  incomplete_goals: DigestGoal[];
  reviewed_files: string[];
  excluded_files: DigestExcludedFile[];
  merge_decision?: MergeDecision;
  checked_areas: ("head_source_ranges" | "base_source_ranges" | "file_changes")[];
  termination_reason: string | null;
}

export interface MergeDecision {
  status: "unavailable" | "fix_blockers" | "resolve_unknowns" | "conditional_merge";
  label: string;
  advice: string;
  blocking_finding_keys: string[];
  nonblocking_finding_keys: string[];
  unresolved_reasons: string[];
  limits: string[];
}
export interface ReviewCommentOptions {
  detail: "compact" | "full";
  exclude_resolved: boolean;
  finding_key?: string | null;
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
  attribution: 'introduced' | 'exposed' | 'pre_existing' | 'uncertain';
  finding_key: string;
  candidate_id: string;
  severity: "critical" | "high" | "medium" | "low";
  placement: "inline" | "summary";
  anchor: PublicationAnchor;
  title: string;
  body_markdown: string;
  affected_behavior: string;
  failure_scenario: string;
  recommendation: string;
  category: string | null;
  evidence_summary: string | null;
  evidence_ids: string[];
}

export interface WithheldFinding {
  candidate_id: string;
  reason_code: string;
}

export interface PublicationPlan {
  review_id: string;
  snapshot: PublicationSnapshot;
  outcome: ReviewOutcome;
  digest: ReviewDigest;
  summary_markdown: string;
  user_report_markdown: string;
  review_comment_markdown?: string;
  inline_drafts: PublicationFinding[];
  summary_findings: PublicationFinding[];
  withheld_findings: WithheldFinding[];
  content_eligible: boolean;
  blocking_reasons: string[];
  presentation?: ReviewPresentation | null;
}

export interface ReviewSummaryDigest {
  discussion_review_enabled?: boolean | null;
  discussion_stats?: {status:string;collected:number;limitations:string[]} | null;
  author_goal?: string | null;
  author_context_status?: "complete" | "truncated" | "unknown" | "not_recorded";
  author_context_limitation?: string | null;
  change_understanding?: ChangeUnderstanding | null;
  actual_changes?: string[];
  merge_decision?: MergeDecision;
  excluded_files: DigestExcludedFile[];
  checked_areas: ("head_source_ranges" | "base_source_ranges" | "file_changes")[];
  termination_reason: string | null;
}
export interface ReviewSummary {
  review_id: string;
  snapshot: PublicationSnapshot;
  outcome: ReviewOutcome;
  digest: ReviewSummaryDigest;
  review_comment_markdown?: string;
  withheld_findings: WithheldFinding[];
  content_eligible: boolean;
  blocking_reasons: string[];
  presentation?: ReviewPresentation | null;
}

export interface PresentationDelivery {
  confirmed: number;
  eligible: number;
  selected: number;
  inline_ready: number;
  summary_only: number;
  not_selected: number;
  already_published: number;
  actual_published: number | null;
}

export interface ReviewPresentation {
  review_id: string;
  snapshot: PublicationSnapshot;
  language: "zh-CN";
  synthesis_status: "ready" | "fallback" | "unavailable";
  summary_source?: "model" | "metadata" | "unavailable" | null;
  purpose_text: string | null;
  purpose_source: "author_claim" | "code_inferred" | "unknown";
  change_summary: string;
  test_changes: string[];
  review_summary: string;
  verdict: MergeDecision;
  findings: PublicationFinding[];
  delivery: PresentationDelivery;
  limitations: string[];
}

export interface ReviewCommentCheck {
  review_id: string;
  status: "current" | "stale" | "unknown";
  reviewed_head_sha: string | null;
  latest_head_sha: string | null;
  checked_at: string;
  review_comment_markdown: string;
  preview_markdown: string;
  included_finding_keys: string[];
  omitted_finding_keys: string[];
  retained_blocking_keys: string[];
  detail: "compact" | "full";
}

export type PublicationState = "queued" | "preparing" | "publishing" | "succeeded" | "partial" | "failed" | "uncertain" | "stale";
export interface PublicationSelection {
  expected_plan_hash: string;
  expected_head_sha: string;
  publish_summary: true;
  finding_keys: string[];
  acknowledge_partial_review: boolean;
}
export interface GitHubInlineLocation { path: string; line: number; side: "LEFT" | "RIGHT" }
export interface PublicationInlinePreview {
  finding_key: string;
  action: "create" | "already_published" | "summary_fallback" | "summary_only";
  body_markdown: string;
  location: GitHubInlineLocation | null;
}
export interface PublicationPreflightResult {
  ready: boolean;
  review_id: string;
  plan_hash: string;
  head_sha: string;
  head_sha_matches: boolean;
  pr_open: boolean;
  permissions_ok: boolean;
  summary_action: "create" | "update";
  summary_comment_id: number | null;
  finding_keys: string[];
  inline: { selected: number; ready: number; already_published: number; summary_fallback: number; blocked: number };
  inline_previews: PublicationInlinePreview[];
  summary_markdown: string;
  requires_partial_acknowledgement: boolean;
  warnings: string[];
  presentation?: ReviewPresentation | null;
}
export interface PublicationItem {
  id: string;
  publication_id: string;
  kind: "summary" | "inline";
  finding_key: string;
  fingerprint: string;
  payload_hash: string;
  state: "pending" | "publishing" | "published" | "failed" | "uncertain" | "summary_only";
  github_comment_id: number | null;
  github_review_id: number | null;
  github_url: string | null;
  last_error_code: string | null;
}
export interface PublicationAttempt {
  id: string;
  review_id: string;
  repository_id: number;
  pr_number: number;
  head_sha: string;
  plan_hash: string;
  selection_hash: string;
  publisher_identity: string;
  state: PublicationState;
  requested_at: string;
  completed_at: string | null;
  last_error_code: string | null;
  items: PublicationItem[];
}
export interface PublicationAccepted {
  publication_id: string;
  state: PublicationState;
  status_url: string;
}
