import type {
  ReviewCreateResponse,
  ReviewMode,
  ReviewPreviewResponse,
  ReviewTask,
  TaskStepProgress,
  ReviewUnitResult,
  ValidationBackend,
} from "../types/review";
import type {
  ModelCatalogResponse,
  ReviewTaskListResponse,
  SystemDiagnostics,
  ValidationBackendInfo,
  WorkspaceCleanupPreview,
  WorkspaceCleanupResponse,
} from "../types/operations";

const API_BASE = import.meta.env.VITE_API_BASE ?? "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {})
    },
    ...init
  });
  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || `请求失败：${response.status}`);
  }
  return response.json() as Promise<T>;
}

export async function createReview(
  prUrl: string,
  model?: string,
  mode: ReviewMode = "review",
  generatePatches = false,
  validationBackend: ValidationBackend = "none",
): Promise<ReviewCreateResponse> {
  return request<ReviewCreateResponse>("/api/reviews", {
    method: "POST",
    body: JSON.stringify({
      pr_url: prUrl,
      model: model || null,
      mode,
      generate_patches: generatePatches,
      validation_backend: validationBackend,
    })
  });
}

export async function previewReview(
  prUrl: string,
  mode: ReviewMode = "review",
  generatePatches = false,
  validationBackend: ValidationBackend = "none",
): Promise<ReviewPreviewResponse> {
  return request<ReviewPreviewResponse>("/api/reviews/preview", {
    method: "POST",
    body: JSON.stringify({
      pr_url: prUrl,
      mode,
      generate_patches: generatePatches,
      validation_backend: validationBackend,
    }),
  });
}

export async function getReview(taskId: string): Promise<ReviewTask> {
  return request<ReviewTask>(`/api/reviews/${taskId}`);
}

export async function listReviews(options: {
  status?: string;
  page?: number;
  pageSize?: number;
} = {}): Promise<ReviewTaskListResponse> {
  const params = new URLSearchParams({
    page: String(options.page ?? 1),
    page_size: String(options.pageSize ?? 20),
  });
  if (options.status) params.set("status", options.status);
  return request<ReviewTaskListResponse>(`/api/reviews?${params.toString()}`);
}

export async function getValidationBackends(): Promise<ValidationBackendInfo[]> {
  return request<ValidationBackendInfo[]>("/api/validation/backends");
}

export async function getSystemDiagnostics(): Promise<SystemDiagnostics> {
  return request<SystemDiagnostics>("/api/system/diagnostics");
}

export async function previewWorkspaceCleanup(): Promise<WorkspaceCleanupPreview> {
  return request<WorkspaceCleanupPreview>("/api/system/workspaces/cleanup/preview");
}

export async function cleanupExpiredWorkspaces(): Promise<WorkspaceCleanupResponse> {
  return request<WorkspaceCleanupResponse>("/api/system/workspaces/cleanup", {
    method: "POST",
    body: JSON.stringify({ mode: "expired_only", confirmed: true }),
  });
}

export async function getAvailableModels(): Promise<ModelCatalogResponse> {
  return request<ModelCatalogResponse>("/api/system/models");
}

export async function retryReviewUnit(
  taskId: string,
  unitId: string,
): Promise<ReviewUnitResult> {
  return request<ReviewUnitResult>(`/api/reviews/${taskId}/units/${unitId}/retry`, {
    method: "POST",
  });
}

export async function cancelReview(taskId: string): Promise<{ task_id: string; status: string }> {
  return request(`/api/reviews/${taskId}/cancel`, { method: "POST" });
}

export async function getReport(taskId: string): Promise<string> {
  const response = await fetch(`${API_BASE}/api/reviews/${taskId}/report`);
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.text();
}

export function subscribeToEvents(
  taskId: string,
  callbacks: {
    onStepProgress?: (data: {
      node: string;
      status: string;
      message?: string;
      progress?: TaskStepProgress | null;
      started_at?: string | null;
      updated_at?: string | null;
    }) => void;
    onPatchUpdate?: (data: { id: string; status: string; warning?: string | null }) => void;
    onDone?: (data: { status: string }) => void;
    onError?: (data: { message: string }) => void;
  }
): EventSource {
  const es = new EventSource(`${API_BASE}/api/reviews/${taskId}/stream`);
  es.addEventListener("step_progress", (e: MessageEvent) => {
    callbacks.onStepProgress?.(JSON.parse(e.data));
  });
  es.addEventListener("patch_update", (e: MessageEvent) => {
    callbacks.onPatchUpdate?.(JSON.parse(e.data));
  });
  es.addEventListener("done", (e: MessageEvent) => {
    callbacks.onDone?.(JSON.parse(e.data));
    es.close();
  });
  es.addEventListener("error", (e: MessageEvent) => {
    if (e.data) {
      callbacks.onError?.(JSON.parse(e.data));
    }
    es.close();
  });
  es.onerror = () => {
    callbacks.onError?.({ message: "Event stream disconnected" });
    es.close();
  };
  return es;
}

