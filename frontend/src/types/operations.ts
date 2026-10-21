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

export interface VersionDiagnostics {
  config: string;
  prompt: string;
  rule: string;
  tool_schema: string;
  review_policy: string;
  patch_policy: string;
}

export interface SystemDiagnostics {
  version: string;
  provider: string;
  default_model: string;
  database_schema_current: boolean;
  worker_status: "running" | "idle";
  artifact_directory_writable: boolean;
  langsmith_enabled: boolean;
  security_mode: "restricted" | "unsafe_local";
  patch_max_files: number;
  patch_max_changed_lines: number;
  retention_days: number;
  configured_secrets: Record<string, boolean>;
  versions: VersionDiagnostics;
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
