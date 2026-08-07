import type { ReviewTask } from "./review";

export type AppPage = "dashboard" | "history" | "validation" | "settings";

export interface ReviewTaskListResponse {
  items: ReviewTask[];
  total: number;
  page: number;
  page_size: number;
}

export type BackendHealthStatus = "healthy" | "degraded" | "unavailable";

export interface ValidationBackendInfo {
  name: string;
  display_name: string;
  available: boolean;
  supported_languages: string[];
  supported_profiles: string[];
  executes_untrusted_code: boolean;
  requires_user_configuration: boolean;
  unavailable_reason?: string | null;
  health_status: BackendHealthStatus;
  last_health_check_at: string;
  safety_boundary: string;
  documentation_url: string;
  registered_runner_count?: number | null;
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
  validation_backends: ValidationBackendInfo[];
  configured_secrets: Record<string, boolean>;
  versions: VersionDiagnostics;
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
