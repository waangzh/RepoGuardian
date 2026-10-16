import type {
  ReviewCreateResponse,
  ReviewMode,
  ReviewPreviewResponse,
  ReviewTask,
  TaskStepProgress,
  ReviewUnitResult,
  TaskRuntimeRetryResult,
  ValidationBackend,
} from "../types/review";
import type {
  ModelCatalogResponse,
  ReviewTaskListResponse,
  SystemDiagnostics,
  WorkspaceCleanupPreview,
  WorkspaceCleanupResponse,
} from "../types/operations";
import type { PublicationPlan } from "../types/publication";
import type { PublicationSelection, PublicationPreflightResult, PublicationAccepted, PublicationAttempt } from "../types/publication";

const API_BASE = import.meta.env.VITE_API_BASE ?? "";

async function publicationRequest<T>(path: string, token: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers: {
    "Content-Type": "application/json", Authorization: `Bearer ${token}`, ...init.headers,
  } });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const code = typeof payload?.detail?.code === "string" ? payload.detail.code : `HTTP ${response.status}`;
    const labels: Record<string, string> = {
      publication_disabled: "服务端尚未启用 GitHub 发布，请先完成独立凭据与仓库授权配置。",
      publication_operator_unauthorized: "发布操作口令无效，或当前连接不符合本地单用户访问限制。",
      publication_stale: "PR 版本已改变，请重新审查。", github_permission_denied: "发布身份的 GitHub 权限不足。",
      repository_not_authorized: "目标仓库不在发布授权范围。", partial_acknowledgement_required: "请明确确认部分审查或部分发布。",
      summary_operation_uncertain: "Summary 存在未确认操作，请从发布历史检查远端状态。",
      summary_newer_review: "已有更新的审查摘要，旧结果不能覆盖。", idempotency_key_conflict: "本次确认标识对应不同内容，请重新预检。",
    };
    throw new Error(labels[code] || `发布请求失败：${code}`);
  }
  return response.json() as Promise<T>;
}

function publicationPath(reviewId: string) { return `/api/reviews/${encodeURIComponent(reviewId)}`; }
export function preflightPublication(reviewId: string, keys: string[], token: string, signal?: AbortSignal) {
  return publicationRequest<PublicationPreflightResult>(`${publicationPath(reviewId)}/publication-preflight`, token,
    { method: "POST", body: JSON.stringify({ finding_keys: keys }), signal });
}
export function createPublication(reviewId: string, selection: PublicationSelection, key: string, token: string, signal?: AbortSignal) {
  return publicationRequest<PublicationAccepted>(`${publicationPath(reviewId)}/publications`, token,
    { method: "POST", headers: { "Idempotency-Key": key }, body: JSON.stringify(selection), signal });
}
export function getPublications(reviewId: string, token: string, signal?: AbortSignal) {
  return publicationRequest<PublicationAttempt[]>(`${publicationPath(reviewId)}/publications`, token, { signal });
}
export function getPublication(reviewId: string, identity: string, token: string, signal?: AbortSignal) {
  return publicationRequest<PublicationAttempt>(`${publicationPath(reviewId)}/publications/${encodeURIComponent(identity)}`, token, { signal });
}
export function retryPublication(reviewId: string, identity: string, token: string, signal?: AbortSignal) {
  return publicationRequest<PublicationAttempt>(`${publicationPath(reviewId)}/publications/${encodeURIComponent(identity)}/retry`, token,
    { method: "POST", signal });
}

export function getPublicationPlan(taskId: string, signal?: AbortSignal): Promise<PublicationPlan> {
  return request<PublicationPlan>(`/api/reviews/${encodeURIComponent(taskId)}/publication-plan`, { signal });
}

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
  // 引擎由服务端冻结为 task_review_v1；客户端不再选择引擎。
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
  signal?: AbortSignal,
): Promise<ReviewPreviewResponse> {
  return request<ReviewPreviewResponse>("/api/reviews/preview", {
    method: "POST",
    signal,
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
  signal?: AbortSignal;
} = {}): Promise<ReviewTaskListResponse> {
  const params = new URLSearchParams({
    page: String(options.page ?? 1),
    page_size: String(options.pageSize ?? 20),
  });
  if (options.status) params.set("status", options.status);
  return request<ReviewTaskListResponse>(`/api/reviews?${params.toString()}`, { signal: options.signal });
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
): Promise<ReviewUnitResult | TaskRuntimeRetryResult> {
  return request<ReviewUnitResult | TaskRuntimeRetryResult>(`/api/reviews/${taskId}/units/${unitId}/retry`, {
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

