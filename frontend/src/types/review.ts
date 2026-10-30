export type Severity = 'critical' | 'high' | 'medium' | 'low';
export type RunStatus = 'queued' | 'running' | 'completed' | 'partial' | 'failed' | 'cancelled';

export interface ReviewLimits {
  concurrency: number | null;
  model_calls: number | null;
  tool_calls: number | null;
  timeout_seconds: number | null;
  output_tokens: number | null;
  total_model_calls: number | null;
  total_input_tokens: number | null;
  total_output_tokens: number | null;
  context_tokens: number | null;
}

export interface ReviewOptions {
  depth: 'quick' | 'standard' | 'deep';
  paths: string[] | null;
  focus: ('correctness' | 'security' | 'performance' | 'compatibility')[];
  include_pr_discussions: boolean;
  limits: ReviewLimits;
}

export interface Snapshot {
  review_id: string;
  repository: string;
  repository_id: number;
  pr_number: number;
  declared_base_sha: string;
  base_sha: string;
  head_sha: string;
}

export interface SourceRef {
  id: string;
  review_id: string;
  commit_sha: string;
  side: 'base' | 'head';
  path: string;
  start_line: number;
  end_line: number;
  content_hash: string;
}

export interface Location {
  path: string;
  line: number;
  side: 'base' | 'head';
}

export interface Unit {
  id: string;
  paths: string[];
  required_hunk_ids: string[];
  risk: 'low' | 'medium' | 'high';
  focus: string[];
  estimated_input_tokens: number;
  reused_from: string | null;
}

export interface Plan {
  version: string;
  units: Unit[];
  excluded: Record<string, string>;
  incremental_from: string | null;
  history: Record<string, unknown>[];
  incremental_reason: string | null;
}

export interface CandidateInput {
  claim: string;
  trigger: string;
  impact: string;
  source_refs: string[];
  location: Location | null;
}

export interface VerificationOutput {
  verdict: 'confirmed' | 'refuted' | 'insufficient';
  attribution: 'introduced' | 'exposed' | 'pre_existing' | 'uncertain';
  severity: Severity | null;
  reason: string;
  source_refs: string[];
  author_comment: string | null;
}

export interface Finding {
  id: string;
  candidate_id: string;
  claim: string;
  trigger: string;
  impact: string;
  severity: Severity;
  attribution: 'introduced' | 'exposed' | 'pre_existing' | 'uncertain';
  author_comment: string;
  location: Location | null;
  sources: SourceRef[];
  publishable: boolean;
  admission_digest: string;
}

export interface Candidate {
  id: string;
  unit_id: string;
  attempt_id: string;
  tool_call_id: string;
  proposal: CandidateInput;
  sources: SourceRef[];
  verification: VerificationOutput | null;
  verification_sources: SourceRef[];
  admission_reason: string | null;
  finding: Finding | null;
  previous_candidate_id: string | null;
}

export interface FindingDisposition {
  status: 'open' | 'resolved' | 'dismissed';
  note: string | null;
}

export type FindingDispositions = Record<string, FindingDisposition>;

export interface WorkItem {
  id: string;
  kind: 'review' | 'verification';
  unit_id: string;
  candidate_id: string | null;
  status: 'queued' | 'running' | 'completed' | 'interrupted' | 'failed' | 'skipped';
  attempt: number;
  attempt_id: string | null;
  delivered_hunk_ids: string[];
  visible_source_ids: string[];
  delivered_materials: Record<string, unknown>;
  missing_material_reasons: Record<string, string>;
  material_delivery: 'complete' | 'incomplete' | 'not_started';
  error_code: string | null;
  result: Record<string, unknown> | null;
  tool_calls: number;
  reused_from: string | null;
}

export interface OverviewParagraph {
  text: string;
  source_refs: string[];
}

export interface Overview {
  status: 'pending' | 'running' | 'ready' | 'failed' | 'skipped_deadline' | 'skipped_cancelled' | 'skipped_empty';
  input_hash: string | null;
  output: {
    change_overview: OverviewParagraph[];
    finding_order: string[];
  } | null;
  sources: SourceRef[];
  omitted: Record<'changes' | 'findings' | 'decision_boundaries' | 'sources', number>;
  error_code: string | null;
}

export interface Report {
  version: string;
  status: RunStatus;
  findings: Finding[];
  candidates: Candidate[];
  units: WorkItem[];
  excluded: Record<string, string>;
  overview: Overview;
  usage: Record<string, unknown>;
  reasons: string[];
}

export interface PullRequestInfo {
  owner: string;
  repo: string;
  number: number;
  title: string;
  body: string | null;
  html_url: string;
}

export interface DiffLine {
  kind: 'added' | 'deleted' | 'context';
  content: string;
  old_line_no: number | null;
  new_line_no: number | null;
}

export interface DiffHunk {
  old_start: number;
  old_length: number;
  new_start: number;
  new_length: number;
  hunk_id: string;
  lines: DiffLine[];
}

export interface ChangedFile {
  file_path: string;
  old_file_path: string | null;
  change_type: string;
  additions: number;
  deletions: number;
  is_binary: boolean;
  hunks: DiffHunk[];
}

export interface Run {
  id: string;
  status: RunStatus;
  phase: string;
  pr_url: string;
  pr: PullRequestInfo;
  snapshot: Snapshot;
  configuration: {
    provider: string;
    endpoint: string;
    model: string;
    options: ReviewOptions;
    prompt_version: string;
  };
  changed_files: ChangedFile[];
  plan: Plan;
  report: Report | null;
  discussion_uri: string | null;
  review_cutoff: string;
  hard_deadline: string;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
  error_code: string | null;
}

export interface FileContext {
  file_id: number;
  file_path: string;
  snapshot: Snapshot;
  base_lines: string[];
  head_lines: string[];
}

export interface ReviewCreateRequest {
  pr_url: string;
  depth: ReviewOptions['depth'];
  paths: string[] | null;
  focus: ReviewOptions['focus'];
  limits: Partial<ReviewLimits>;
  model?: string;
  include_pr_discussions?: boolean;
}

export interface ReviewPreviewResponse {
  snapshot: Snapshot;
  plan: Plan;
  changed_files: ChangedFile[];
}
