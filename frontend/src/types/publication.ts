export type ReviewOutcomeKind = 'clean' | 'findings' | 'inconclusive' | 'failed' | 'cancelled';
export type CoverageKind = 'complete' | 'partial' | 'unknown';

export interface ReviewOutcome {
  kind: ReviewOutcomeKind;
  coverage: CoverageKind;
  confirmed_count: number;
  publishable_count: number;
  unresolved_count: number;
  reason_codes: string[];
}

export interface ReviewDigest {
  pr_url: string | null;
  execution_status: string | null;
  actual_changes: string[];
}

export interface ReviewCommentOptions {
  detail: 'compact' | 'full';
  exclude_resolved: boolean;
  finding_key?: string | null;
}

export interface PublicationSnapshot {
  declared_base_sha: string | null;
  effective_base_sha: string | null;
  head_sha: string | null;
  repository_id: number | null;
}

export interface PublicationAnchor {
  file_path: string;
  start_line: number | null;
  end_line: number | null;
  side: 'head' | 'base' | null;
  hunk_id: string | null;
}

export interface PublicationFinding {
  finding_key: string;
  candidate_id: string;
  attribution: 'introduced' | 'exposed';
  severity: 'critical' | 'high' | 'medium' | 'low';
  placement: 'inline' | 'summary';
  anchor: PublicationAnchor;
  title: string;
  body_markdown: string;
  affected_behavior: string;
  failure_scenario: string;
  recommendation: string;
  evidence_ids: string[];
}

export interface WithheldFinding {
  candidate_id: string;
  reason_code: string;
}

export interface PublicationPlan {
  review_id: string;
  report_version: string;
  snapshot: PublicationSnapshot;
  outcome: ReviewOutcome;
  digest: ReviewDigest;
  summary_markdown: string;
  user_report_markdown: string;
  review_comment_markdown: string;
  inline_drafts: PublicationFinding[];
  summary_findings: PublicationFinding[];
  withheld_findings: WithheldFinding[];
  content_eligible: boolean;
  blocking_reasons: string[];
  overview_markdown: string;
  overview_status: string;
  scope_reasons: string[];
}

export interface ReviewSummary {
  review_id: string;
  report_version: string;
  snapshot: PublicationSnapshot;
  outcome: ReviewOutcome;
  review_comment_markdown: string;
  withheld_findings: WithheldFinding[];
  content_eligible: boolean;
  blocking_reasons: string[];
}

export interface ReviewCommentCheck {
  review_id: string;
  status: 'current' | 'stale' | 'unknown';
  reviewed_head_sha: string | null;
  latest_head_sha: string | null;
  checked_at: string;
  review_comment_markdown: string;
  preview_markdown: string;
  included_finding_keys: string[];
  omitted_finding_keys: string[];
  retained_blocking_keys: string[];
  detail: 'compact' | 'full';
}

export type PublicationState =
  | 'queued'
  | 'preparing'
  | 'publishing'
  | 'succeeded'
  | 'partial'
  | 'failed'
  | 'uncertain'
  | 'stale';

export interface PublicationSelection {
  expected_plan_hash: string;
  expected_report_version: string;
  expected_head_sha: string;
  publish_summary: true;
  finding_keys: string[];
  acknowledge_partial_review: boolean;
}

export interface GitHubInlineLocation {
  path: string;
  line: number;
  side: 'LEFT' | 'RIGHT';
}

export interface PublicationInlineCounts {
  selected: number;
  ready: number;
  already_published: number;
  summary_fallback: number;
  blocked: number;
}

export interface PublicationInlinePreview {
  finding_key: string;
  action: 'create' | 'already_published' | 'summary_fallback' | 'summary_only';
  body_markdown: string;
  location: GitHubInlineLocation | null;
}

export interface PublicationPreflightResult {
  ready: boolean;
  review_id: string;
  report_version: string;
  plan_hash: string;
  head_sha: string;
  head_sha_matches: boolean;
  pr_open: boolean;
  permissions_ok: boolean;
  summary_action: 'create' | 'update';
  summary_comment_id: number | null;
  finding_keys: string[];
  inline: PublicationInlineCounts;
  inline_previews: PublicationInlinePreview[];
  summary_markdown: string;
  requires_partial_acknowledgement: boolean;
  warnings: string[];
}

export interface PublicationItem {
  id: string;
  publication_id: string;
  kind: 'summary' | 'inline';
  finding_key: string;
  fingerprint: string;
  payload_hash: string;
  state: 'pending' | 'publishing' | 'published' | 'failed' | 'uncertain' | 'summary_only';
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
