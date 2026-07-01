export type TaskStatus = "pending" | "running" | "completed" | "failed";
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

export interface PatchResult {
  id: string;
  issue_id?: string | null;
  diff_content: string;
  status: string;
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
  report_markdown?: string | null;
  error?: string | null;
  created_at: string;
  updated_at: string;
  context_snippets?: ContextSnippet[];
  repo_snapshot?: RepoSnapshot | null;
}
