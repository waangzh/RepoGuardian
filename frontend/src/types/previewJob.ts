import type { ReviewMode, ReviewPreviewResponse } from './review';
import type { ReviewPlanOptions } from './reviewPlan';

export interface PreviewRequest {
  pr_url: string;
  model?: string | null;
  reuse_review_id?: string | null;
  force_full_review?: boolean;
  mode: ReviewMode;
  plan?: ReviewPlanOptions;
}
export interface PreviewJob {
  id: string;
  request: PreviewRequest;
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'expired' | 'consumed';
  stages: Array<{ phase: string; message: string; started_at: string; finished_at: string | null; percent: number | null; operation: string | null }>;
  cache_hit: boolean;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  error: string | null;
  result: ReviewPreviewResponse | null;
  consumed_task_id: string | null;
}
