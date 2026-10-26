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
  finding_conclusion: "confirmed" | "unresolved" | "no_confirmed_findings" | "unknown";
  review_completeness: CoverageKind;
  runtime_reliability: "reliable" | "unreliable" | "unknown";
  /** 历史讨论复核的独立结果；其超时或计量缺失不影响上方主审查字段。 */
  historical_discussion?: HistoricalDiscussionOutcome;
}

export interface HistoricalDiscussionOutcome {
  status: "not_recorded" | "completed" | "partial" | "timed_out" | "unavailable";
  collected: number;
  checked: number;
  unchecked: number;
  verified_defects: { discussion_id: string | null; path: string | null; reason: string | null; evidence_ids: string[] }[];
  termination_reason: string | null;
  limitations: string[];
  usage: Record<string, unknown>;
  affects_risk: boolean;
  findings?: HistoricalFindingView[];
}

export interface HistoricalFindingView {
  discussion_id: string;
  path: string | null;
  status: "still_present";
  severity: "critical" | "high" | "medium" | "low" | "unknown";
  priority: "fix_before_merge" | "assess_improvement" | "assess_impact";
  affected_behavior: string | null;
  recommendation: string | null;
  reason: string;
  evidence_ids: string[];
  discussion_url: string | null;
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
export interface DigestInvestigation {
  work_item_id: string | null;
  proposition: string;
  answer: "supported" | "refuted" | "unresolved";
  reason: string | null;
  limitations: string[];
  requested_ranges: DigestGoal[];
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
  discussion_stats?: import("./prDiscussion").DiscussionStats | null;
  discussion_rechecks?: import("./prDiscussion").DiscussionRecheck[];
  discussion_links?: Record<string, string>;
  pr_url?: string | null;
  author_goal?: string | null;
  author_context_status?: "complete" | "truncated" | "unknown" | "not_recorded";
  author_context_limitation?: string | null;
  actual_changes?: string[];
  change_understanding?: ChangeUnderstanding | null;
  intent_alignment?: string;
  intent_assessments?: DigestIntent[];
  findings?: import('./reviewPlan').FindingView[];
  investigations_known?: boolean;
  investigations?: DigestInvestigation[];
  unresolved_leads?: string[];
  execution_status?: string | null;
  examined_goals: DigestGoal[];
  incomplete_goals: DigestGoal[];
  reviewed_files: string[];
  excluded_files: DigestExcludedFile[];
  merge_decision?: MergeDecision;
  checked_areas: ("head_source_ranges" | "base_source_ranges" | "file_changes")[];
  termination_reason: string | null;
}

export interface DigestIntent {
  author_target: string;
  status: "observed" | "contradicted" | "insufficient" | "not_applicable" | "not_checked";
  observations: { goal_id: string; path: string; start_line: number | null; side: "head" | "base";
    author_goal_summary?: string;
    actual_behavior: string; alignment: "supported" | "contradicted" | "insufficient" | "not_applicable";
    evidence_ids: string[]; limitations: string[] }[];
}
export interface MergeDecision {
  /** review_suggestions 仅用于兼容旧冻结计划；当前后端只生成三态。 */
  status: "unavailable" | "fix_blockers" | "resolve_unknowns" | "review_suggestions" | "conditional_merge";
  label: string;
  advice: string;
  blocking_finding_keys: string[];
  nonblocking_finding_keys: string[];
  unresolved_reasons: string[];
  limits: string[];
  blocking_historical_keys?: string[];
  nonblocking_historical_keys?: string[];
  /** 按严格重复关联去重后的独立缺陷数；缺失时（旧冻结计划）回退为来源相加。 */
  blocking_independent_count?: number | null;
  nonblocking_independent_count?: number | null;
  /** 与本轮问题指向同一缺陷、已在计数中合并的历史讨论。 */
  deduplicated_historical_keys?: string[];
}
export interface ReviewCommentOptions {
  detail: "compact" | "full";
  exclude_resolved: boolean;
  include_discussed?: boolean;
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
  finding_key: string;
  default_selected?: boolean;
  default_selection_reason?: string | null;
  discussion_urls?: string[];
  discussion_matches?: { discussion_id: string; reason: string }[];
  candidate_id: string;
  severity: "critical" | "high" | "medium" | "low";
  placement: "inline" | "summary";
  anchor: PublicationAnchor;
  title: string;
  /** Plain text in new plans; missing on legacy plans whose title was Markdown-escaped. */
  raw_title?: string | null;
  body_markdown: string;
  legacy_body_markdown?: string | null;
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
  schema_version: "review-publication-v1";
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
  discussion_stats?: import("./prDiscussion").DiscussionStats | null;
  author_goal?: string | null;
  author_context_status?: "complete" | "truncated" | "unknown" | "not_recorded";
  author_context_limitation?: string | null;
  change_understanding?: ChangeUnderstanding | null;
  actual_changes?: string[];
  intent_alignment?: string;
  merge_decision?: MergeDecision;
  excluded_files: DigestExcludedFile[];
  checked_areas: ("head_source_ranges" | "base_source_ranges" | "file_changes")[];
  termination_reason: string | null;
}
export interface ReviewSummary {
  schema_version: "review-summary-v1";
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

export interface SummaryInputCoverage {
  truncation_reasons: (
    | "author_context_truncated" | "source_text_truncated" | "diff_hunk_truncated"
    | "diff_hunk_omitted" | "diff_metadata_only" | "source_count_limited" | "input_size_limited"
    | "review_scope_excluded" | "sensitive_or_binary_excluded" | "invalid_path_excluded"
  )[];
  omitted_files: number;
  omitted_hunks: number;
}

export interface ReviewPresentation {
  schema_version: "review-presentation-v1" | "review-presentation-v2";
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
  summary_input_coverage?: SummaryInputCoverage | null;
}

export interface ReviewCommentCheck {
  review_id: string;
  status: "current" | "stale" | "unknown";
  reviewed_head_sha: string | null;
  latest_head_sha: string | null;
  checked_at: string;
  review_comment_markdown: string;
  preview_markdown?: string | null;
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
