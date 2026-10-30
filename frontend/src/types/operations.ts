import type { Run } from './review';

export type AppPage = 'dashboard' | 'history' | 'settings';

export interface ReviewTaskListResponse {
  items: Run[];
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
  worker_status: 'running' | 'idle';
  artifact_directory_writable: boolean;
  langsmith_enabled: boolean;
  retention_days: number;
  configured_secrets: Record<string, boolean>;
}

export interface PublicationCapability {
  enabled: boolean;
  mode: 'publish' | 'copy_only';
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
