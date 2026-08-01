export type KnownTaskStatus =
  | "pending"
  | "running"
  | "queued"
  | "planning"
  | "reviewing"
  | "resolving_evidence"
  | "verifying_issues"
  | "generating_patches"
  | "validating"
  | "waiting_for_human"
  | "completed"
  | "completed_with_warnings"
  | "failed"
  | "cancelled";
export type TaskStatus = KnownTaskStatus | (string & {});
export type ReviewMode = "review" | "review_and_suggest" | "review_suggest_and_validate";
export type ValidationBackend = "none" | "local" | "gvisor";
export type ValidationStatus =
  | "not_requested"
  | "unsupported"
  | "queued"
  | "running"
  | "passed"
  | "failed"
  | "infrastructure_error"
  | "timed_out"
  | "inconclusive"
  | "cancelled"
  | (string & {});
export type ReviewPhase =
  | "prepare"
  | "project_detection"
  | "baseline"
  | "discovery"
  | "verification"
  | "repair"
  | "validation"
  | "publishing"
  | "completed"
  | "failed";
export type StepStatus = "pending" | "running" | "completed" | "failed";
export type Severity = "low" | "medium" | "high" | "critical";

export interface ReviewCreateResponse {
  task_id: string;
  status: TaskStatus;
}

export interface ReviewSummary {
  mode: ReviewMode;
  status: TaskStatus;
  completed: boolean;
}

export interface TaskStep {
  name: string;
  status: StepStatus;
  message?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface PullRequestRef {
  ref: string;
  sha: string;
  repo_clone_url: string;
}

export interface PullRequestInfo {
  owner: string;
  repo: string;
  number: number;
  title: string;
  html_url: string;
  clone_url: string;
  base: PullRequestRef;
  head: PullRequestRef;
}

export interface ChangedLine {
  line_no: number | null;
  content: string;
}

export interface DiffHunk {
  old_start: number;
  old_length: number;
  new_start: number;
  new_length: number;
  hunk_id: string;
  lines: DiffLine[];
  added_lines: ChangedLine[];
  removed_lines: ChangedLine[];
}

export interface DiffLine {
  kind: "added" | "context" | "deleted";
  content: string;
  old_line_no?: number | null;
  new_line_no?: number | null;
}

export interface ChangedFile {
  file_path: string;
  old_file_path?: string | null;
  change_type: string;
  additions: number;
  deletions: number;
  is_binary: boolean;
  hunks: DiffHunk[];
}

export type ReviewUnitComplexity = "small" | "medium" | "large";
export type ReviewUnitStatus =
  | "pending"
  | "planning"
  | "reviewing"
  | "completed"
  | "failed"
  | "timed_out"
  | "cancelled";

export interface PlannedChangedFile {
  file_path: string;
  old_file_path?: string | null;
  change_type: string;
  additions: number;
  deletions: number;
  classifications: string[];
  included: boolean;
  excluded_reason?: string | null;
}

export interface ExcludedReviewFile {
  file_path: string;
  reason: string;
  classifications: string[];
}

export interface ReviewUnit {
  id: string;
  primary_files: string[];
  related_files: string[];
  diff_hunk_ids: string[];
  changed_symbols: string[];
  rule_ids: string[];
  risk_tags: string[];
  estimated_tokens: number;
  complexity: ReviewUnitComplexity;
  fingerprint: string;
  grouping_reason: string;
}

export interface ReviewPreviewResponse {
  changed_files: PlannedChangedFile[];
  review_units: ReviewUnit[];
  excluded_files: ExcludedReviewFile[];
  matched_rules: string[];
  risk_tags: string[];
  estimated_model_calls: number;
  estimated_tokens: number;
  warnings: string[];
}

export interface ReviewIssue {
  id: string;
  review_unit_id: string;
  severity: Severity;
  category: string;
  title: string;
  confidence: number;
  affected_behavior: string;
  failure_scenario: string;
  recommendation: string;
  primary_evidence: EvidenceAnchor;
  supporting_evidence: EvidenceAnchor[];
  assumptions: string[];
  related_tests: string[];
  requires_human_confirmation: boolean;
  auto_fix_eligible: boolean;
  status: "candidate" | "evidence_resolved" | "confirmed" | "dismissed" | "needs_human" | "published";
  placement: "inline" | "summary" | "suppressed" | "needs_human";
  unresolved_reason?: string | null;
  source_review_unit_ids: string[];
  source_issue_ids: string[];
}

export interface IssueMetrics {
  candidate_issue_count: number;
  deterministic_drop_count: number;
  verifier_drop_count: number;
  needs_human_count: number;
  duplicate_count: number;
  confirmed_count: number;
  severity_adjustment_count: number;
  verifier_call_count: number;
  verifier_token_count: number;
}

export interface EvidenceCandidate {
  file_path: string;
  side: "head" | "base";
  start_line: number;
  end_line: number;
  hunk_id?: string | null;
}

export interface EvidenceAnchor {
  file_path: string;
  existing_code: string;
  symbol?: string | null;
  expected_side: "head" | "base" | "either";
  expected_hunk_id?: string | null;
  context_before: string[];
  context_after: string[];
  resolved_start_line?: number | null;
  resolved_end_line?: number | null;
  resolution_method: "diff_exact" | "diff_normalized" | "file_exact" | "symbol_assisted" | "unresolved";
  match_count: number;
  anchor_hash?: string | null;
  resolved_side?: "head" | "base" | null;
  candidate_locations: EvidenceCandidate[];
  unresolved_reason?: string | null;
}

export interface HumanReviewRequest {
  missing_information: string[];
  known_evidence: string[];
  questions: string[];
  prohibited_operations: string[];
}

export interface ContextSnippet {
  file: string;
  start_line: number;
  end_line: number;
  content: string;
  relevance: string;
  symbol?: string | null;
  review_unit_id?: string | null;
}

export interface RepoSnapshot {
  language: string;
  framework?: string | null;
  test_framework?: string | null;
  total_files: number;
}

export interface TestRunResult {
  tool: string;
  command: string;
  exit_code: number;
  stdout: string;
  stderr: string;
  passed: boolean;
  duration: number;
}

export interface FailureFingerprint {
  tool: string;
  identity: string;
  test_node_id?: string | null;
  error_type?: string | null;
  file_path?: string | null;
  line_no?: number | null;
  column?: number | null;
  rule_code?: string | null;
  message?: string | null;
  normalized_summary: string;
}

export type FailureKind =
  | "dependency_missing"
  | "test_collection_error"
  | "timeout"
  | "infrastructure"
  | "code_regression"
  | "unknown";

export interface ProjectProfile {
  adapter_id: string;
  language: string;
  detected_files: string[];
  validation_command_ids: string[];
}

export interface ValidationSnapshot {
  id: string;
  stage: "base" | "head" | "patched";
  sha: string;
  patch_id?: string | null;
  command_results: TestRunResult[];
  collected_test_count?: number | null;
  failure_fingerprints: FailureFingerprint[];
  passed: boolean;
  failure_kind?: FailureKind | null;
  failure_detail?: string | null;
}

export interface ValidationDelta {
  from_stage: "base" | "head" | "patched";
  to_stage: "head" | "patched";
  patch_id?: string | null;
  previous_passed: boolean;
  current_passed: boolean;
  failure_kind?: FailureKind | null;
  introduced_failure: boolean;
  resolved_failure: boolean;
  introduced_failures: FailureFingerprint[];
  resolved_failures: FailureFingerprint[];
}

export interface ValidationResult {
  id: string;
  patch_id?: string | null;
  backend: ValidationBackend;
  status: ValidationStatus;
  detail?: string | null;
  snapshot_id?: string | null;
}

export interface PatchResult {
  id: string;
  issue_id?: string | null;
  diff_content: string;
  status:
    | "suggested"
    | "unverified"
    | "validation_pending"
    | "verified"
    | "validation_failed"
    | "validation_inconclusive"
    | "abandoned"
    | "superseded"
    | (string & {});
  revision_of?: string | null;
  attempt_number: number;
  validation_snapshot_id?: string | null;
  validation_backend?: ValidationBackend | null;
  validation_result_id?: string | null;
  error?: string | null;
  created_at: string;
}

export interface AgentEvent {
  action: string;
  reason: string;
  status: string;
  message?: string | null;
  review_unit_id?: string | null;
  created_at: string;
}

export interface ReviewUnitResult {
  review_unit_id: string;
  status: ReviewUnitStatus;
  plan_skipped: boolean;
  issues: ReviewIssue[];
  issue_metrics: IssueMetrics;
  context_snippets: ContextSnippet[];
  messages: AgentEvent[];
  tool_events: Array<{
    review_unit_id: string;
    tool: string;
    status: string;
    result_count: number;
    detail?: string | null;
  }>;
  execution_budget: Record<string, number>;
  error?: string | null;
}

export interface ReviewTask {
  id: string;
  status: TaskStatus;
  phase: ReviewPhase;
  pr_url: string;
  model?: string | null;
  mode: ReviewMode;
  generate_patches: boolean;
  validation_backend: ValidationBackend;
  review: ReviewSummary;
  steps: TaskStep[];
  pr?: PullRequestInfo | null;
  changed_files: ChangedFile[];
  review_units: ReviewUnit[];
  review_unit_results: ReviewUnitResult[];
  excluded_files: ExcludedReviewFile[];
  issues: ReviewIssue[];
  static_results: TestRunResult[];
  patches: PatchResult[];
  test_results: TestRunResult[];
  agent_events: AgentEvent[];
  human_request?: HumanReviewRequest | null;
  report_markdown?: string | null;
  error?: string | null;
  created_at: string;
  updated_at: string;
  context_snippets?: ContextSnippet[];
  repo_snapshot?: RepoSnapshot | null;
  project_profile?: ProjectProfile | null;
  validation_snapshots: ValidationSnapshot[];
  validation_deltas: ValidationDelta[];
  validation: ValidationResult[];
  warnings: string[];
}
