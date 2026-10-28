export interface ReviewLimits {
  model_calls: number | null;
  tool_calls: number | null;
  concurrency: number | null;
  timeout_seconds: number | null;
  output_tokens: number | null;
  total_model_calls: number | null;
  total_input_tokens: number | null;
}
export interface ReviewPlanOptions {
  depth: 'quick' | 'standard' | 'deep';
  focus: ('correctness' | 'security' | 'performance' | 'compatibility')[];
  scope: { mode: 'all' | 'selected'; primary_files: string[] };
  limits: ReviewLimits;
  include_pr_discussions?: boolean;
}
export interface PlanSnapshot { head_sha: string; effective_base_sha: string; declared_base_sha: string }
export interface ReviewCapabilities {
  depth_presets: { id: ReviewPlanOptions['depth']; label: string; limits: ReviewLimits }[];
  focus_categories: ReviewPlanOptions['focus'];
  scope_selection_supported: boolean;
  pr_discussions_available?: boolean;
  pr_discussions_default?: boolean;
  limits: { defaults: ReviewLimits; maxima: ReviewLimits };
}
export interface FindingDisposition { status: 'open' | 'confirmed' | 'ignored' | 'resolved'; reason: string }
export interface FindingView {
  attribution: 'introduced' | 'exposed' | 'pre_existing' | 'uncertain';
  id: string; status: string; evidence_status: 'verified' | 'insufficient' | 'unresolved' | 'unknown';
  review_status: 'candidate' | 'confirmed' | 'unresolved' | 'dismissed' | 'unknown';
  title: string | null; message: string | null; impact: string | null;
  recommendation: string | null; category: string | null;
  author_body: string | null; verification_reason: string | null;
  location: FindingLocation | null; verification: FindingVerification;
  severity: string | null; anchor: Record<string, unknown> | null; source_ids: string[];
  human_confirmation_reason: string | null;
  limitations: string[]; disposition: FindingDisposition | null; updated_at: string | null;
}
export interface FindingLocation {
  path: string; start_line: number; end_line: number; side: 'left' | 'right';
}
export interface FindingVerification {
  method: 'static_model_review';
  evidence_status: FindingView['evidence_status'];
}
