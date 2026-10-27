import type { FileContext, ReviewCreateResponse, ReviewPreviewResponse, ReviewTask } from '../types/review';
import type { ReviewCapabilities, ReviewPlanOptions, FindingView, FindingDisposition } from '../types/reviewPlan';
import type { PreviewJob, PreviewRequest } from '../types/previewJob';
import type { PublicationPlan, PublicationSelection, PublicationPreflightResult, PublicationAccepted, PublicationAttempt } from '../types/publication';
import type { ModelCatalogResponse, ReviewTaskListResponse, SystemDiagnostics } from '../types/operations';
import { publicationErrorText } from '../utils/publicationPresentation';
const API_BASE = import.meta.env.VITE_API_BASE ?? '';

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(API_BASE + path, { ...init, headers: { 'Content-Type': 'application/json', ...init.headers } });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const detail = payload?.detail;
    throw new Error(typeof detail === 'string' ? detail : detail?.message || detail?.code || `请求失败（${response.status}）`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
export const getReviewCapabilities = () => request<ReviewCapabilities>('/api/system/review-capabilities');
export const getAvailableModels = () => request<ModelCatalogResponse>('/api/system/models');
export const getDiagnostics = () => request<SystemDiagnostics>('/api/system/diagnostics');
export const updateSystemSettings = (value: {model_base_url: string; model_name: string; retention_days: number}) => request<SystemDiagnostics>('/api/system/settings', {method: 'PATCH', body: JSON.stringify(value)});
export const getPublicationCapability = () => request<import('../types/operations').PublicationCapability>('/api/system/publication-capability');
export const getReview = (id: string, signal?: AbortSignal) => request<ReviewTask>(`/api/reviews/${encodeURIComponent(id)}`, {signal});
export function subscribeToReview(id: string, refresh: () => void) {
  const stream = new EventSource(`${API_BASE}/api/reviews/${encodeURIComponent(id)}/stream`);
  stream.addEventListener('step_progress', refresh);
  stream.addEventListener('done', () => { refresh(); stream.close(); });
  stream.onerror = () => stream.close();
  return () => stream.close();
}
export const listReviews = (query: string, page: number, signal?: AbortSignal) => request<ReviewTaskListResponse>(`/api/reviews?q=${encodeURIComponent(query)}&page=${page}&page_size=20`, {signal});
export const deleteReview = (id: string) => request<void>(`/api/reviews/${encodeURIComponent(id)}`, {method: 'DELETE'});
export const cancelReview = (id: string) => request<{status: string}>(`/api/reviews/${encodeURIComponent(id)}/cancel`, {method: 'POST'});
export const resumeReview = (id: string) => request<ReviewTask>(`/api/reviews/${encodeURIComponent(id)}/resume`, {method: 'POST'});
export const createReview = (url: string, preview: ReviewPreviewResponse) => request<ReviewCreateResponse>('/api/reviews', {method: 'POST', body: JSON.stringify({pr_url:url, preview_id:preview.preview_id, expected_head_sha:preview.snapshot.head_sha, expected_effective_base_sha:preview.snapshot.effective_base_sha})});
export const previewReview = (input: {pr_url: string; model?: string; plan: ReviewPlanOptions}) => request<ReviewPreviewResponse>('/api/reviews/preview', {method:'POST', body:JSON.stringify(input)});
export const createPreviewJob = (id: string, input: PreviewRequest, signal?: AbortSignal) => request<PreviewJob>('/api/reviews/preview-jobs', {method:'POST', body:JSON.stringify({id,request:input}),signal});
export const getPreviewJob = (id: string, signal?: AbortSignal) => request<PreviewJob>(`/api/reviews/preview-jobs/${encodeURIComponent(id)}`,{signal});
export const cancelPreviewJob = (id: string) => request<PreviewJob>(`/api/reviews/preview-jobs/${encodeURIComponent(id)}/cancel`,{method:'POST'});
export const getFindings = (id: string, signal?: AbortSignal) => request<{items: FindingView[]}>(`${publicationPath(id)}/findings`,{signal});
export const setFindingDisposition = (id: string, finding: string, value: FindingDisposition) => request<{items: FindingView[]}>(`${publicationPath(id)}/findings/${encodeURIComponent(finding)}/disposition`,{method:'PATCH', body:JSON.stringify(value)});
export const getPrDiscussions = (id: string, signal?: AbortSignal) => request<{items: Array<{id:string; source:string; path:string|null; resolved:boolean|null; outdated:boolean|null; comments:Array<{id:string;body:string;url:string|null}>}>;total:number;sources:Record<string,string>;limitations:string[]}>(`${publicationPath(id)}/discussions`,{signal});
export const getFileContext = (id: string, file: number, signal?: AbortSignal) => request<FileContext>(`${publicationPath(id)}/files/${file}/context`,{signal});
export async function getReport(id: string, signal?: AbortSignal): Promise<string> {
 const response = await fetch(`${API_BASE}/api/reviews/${encodeURIComponent(id)}/report`,{signal});
 if (!response.ok) throw new Error('报告尚未生成或读取失败');
 return response.text();
}
async function publicationRequest<T>(path: string, token: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers: {
    "Content-Type": "application/json", Authorization: `Bearer ${token}`, ...init.headers,
  } });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const code = typeof payload?.detail?.code === "string" ? payload.detail.code : `HTTP ${response.status}`;
    throw new Error(publicationErrorText(code));
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

export function getPublicationSummary(taskId: string, signal?: AbortSignal): Promise<import('../types/publication').ReviewSummary> {
  return request<import('../types/publication').ReviewSummary>(`/api/reviews/${encodeURIComponent(taskId)}/publication-summary`, { signal });
}

export function checkReviewComment(taskId: string, signal?: AbortSignal, options?: import('../types/publication').ReviewCommentOptions) {
  return request<import('../types/publication').ReviewCommentCheck>(`/api/reviews/${encodeURIComponent(taskId)}/review-comment-check`, { method: 'POST', signal, body: JSON.stringify(options) });
}
export function previewReviewComment(taskId: string, options: import('../types/publication').ReviewCommentOptions, signal?: AbortSignal) {
  return request<import('../types/publication').ReviewCommentCheck>(`/api/reviews/${encodeURIComponent(taskId)}/review-comment-preview`, { method: 'POST', signal, body: JSON.stringify(options) });
}

