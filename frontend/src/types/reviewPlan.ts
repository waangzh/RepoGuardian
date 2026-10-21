export interface ReviewLimits {
  max_model_calls: number | null;
  max_tokens: number | null;
  max_cost_microusd: number | null;
  timeout_seconds: number | null;
}
export interface ReviewPlanOptions {
  version: 'review-plan-v1';
  depth: 'quick' | 'standard' | 'deep';
  focus: ('correctness' | 'security' | 'performance' | 'compatibility')[];
  scope: { mode: 'all' | 'selected'; primary_files: string[] };
  limits: ReviewLimits;
  include_pr_discussions?: boolean;
}
export interface PlanSnapshot { head_sha: string; effective_base_sha: string; declared_base_sha: string }
export interface ReviewCapabilities {
  plan_version: string;
  depth_presets: { id: ReviewPlanOptions['depth']; label: string; limits: ReviewLimits }[];
  focus_categories: ReviewPlanOptions['focus'];
  scope_selection_supported: boolean;
  pr_discussions_available?: boolean;
  pr_discussions_default?: boolean;
  limits: { defaults: ReviewLimits; maxima: ReviewLimits };
}
export interface FindingDisposition { status: 'open' | 'confirmed' | 'ignored' | 'resolved'; reason: string }
export interface FindingView {
  id: string; status: string; evidence_status: 'verified' | 'insufficient' | 'unresolved' | 'unknown';
  review_status: 'candidate' | 'confirmed' | 'unresolved' | 'dismissed' | 'unknown';
  title: string | null; message: string | null; impact: string | null;
  recommendation: string | null; category: string | null;
  location: FindingLocation | null; verification: FindingVerification;
  severity: string | null; anchor: Record<string, unknown> | null; source_ids: string[];
  human_confirmation_reason: string | null; dynamic_validation_status: string;
  limitations: string[]; disposition: FindingDisposition | null; updated_at: string | null;
}
export interface FindingLocation {
  path: string; start_line: number; end_line: number; side: 'left' | 'right';
}
export interface FindingVerification {
  method: 'static_model_review';
  evidence_status: FindingView['evidence_status'];
  dynamic_validation_status: 'not_executed' | 'passed' | 'failed' | 'unknown';
}
