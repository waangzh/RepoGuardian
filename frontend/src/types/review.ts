export type TaskStatus = "pending" | "running" | "completed" | "failed";
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
  added_lines: ChangedLine[];
  removed_lines: ChangedLine[];
}

export interface ChangedFile {
  file_path: string;
  change_type: string;
  additions: number;
  deletions: number;
  hunks: DiffHunk[];
}

export interface ReviewIssue {
  id: string;
  file_path: string;
  line_no: number | null;
  severity: Severity;
  category: string;
  title: string;
  description: string;
  suggestion: string;
  confidence: number;
  auto_fixable: boolean;
  evidence: string;
  evidence_locations: EvidenceLocation[];
  affected_behavior: string;
  assumptions: string[];
  related_test_ids: string[];
  fix_risk: "low" | "medium" | "high";
  requires_human_confirmation: boolean;
}

export interface EvidenceLocation {
  file_path: string;
  line_no: number;
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

export interface PatchResult {
  id: string;
  issue_id?: string | null;
  diff_content: string;
  status:
    | "generated"
    | "apply_failed"
    | "applied"
    | "validation_passed"
    | "validation_failed"
    | "abandoned"
    | "superseded";
  revision_of?: string | null;
  attempt_number: number;
  validation_snapshot_id?: string | null;
  error?: string | null;
  created_at: string;
}

export interface AgentEvent {
  action: string;
  reason: string;
  status: string;
  message?: string | null;
  created_at: string;
}

export interface ReviewTask {
  id: string;
  status: TaskStatus;
  phase: ReviewPhase;
  pr_url: string;
  model?: string | null;
  steps: TaskStep[];
  pr?: PullRequestInfo | null;
  changed_files: ChangedFile[];
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
}
