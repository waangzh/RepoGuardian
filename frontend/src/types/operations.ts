import type { ReviewTask } from "./review";
import type { MergeDecision, ReviewOutcome } from "./publication";

export type AppPage = "dashboard" | "history" | "settings";

export interface ReviewHistoryTask extends ReviewTask {
  history_result?: { outcome: ReviewOutcome; merge_decision: MergeDecision } | null;
}

export interface ReviewTaskListResponse {
  items: ReviewHistoryTask[];
  total: number;
  page: number;
  page_size: number;
}

export interface SystemDiagnostics {
  version: string;
  provider: string;
  default_model: string;
  model_base_url: string;
  database_schema_current: boolean;
  worker_status: "running" | "idle";
  artifact_directory_writable: boolean;
  langsmith_enabled: boolean;
  retention_days: number;
  configured_secrets: Record<string, boolean>;
}

export interface SystemSettingsUpdate {
  retention_days?: number;
  model_base_url?: string;
  model_name?: string;
}

/** 发布能力探测结果；不包含凭据、仓库或操作口令。 */
export interface PublicationCapability {
  enabled: boolean;
  mode: "publish" | "copy_only";
}

export interface WorkspaceCleanupPreview {
  mode: "expired_only";
  ttl_seconds: number;
  scanned: number;
  eligible: number;
  eligible_bytes: number;
  failed: number;
  skipped_active: number;
  skipped_recent: number;
}

export interface WorkspaceCleanupResponse {
  mode: "expired_only";
  ttl_seconds: number;
  scanned: number;
  removed: number;
  reclaimed_bytes: number;
  failed: number;
  skipped_active: number;
  skipped_recent: number;
}

export interface ProviderModelInfo {
  id: string;
  owned_by?: string | null;
}

export interface ModelCatalogResponse {
  provider: string;
  default_model: string;
  models: ProviderModelInfo[];
}
