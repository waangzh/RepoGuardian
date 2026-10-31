import type {
  Candidate,
  FileContext,
  Finding,
  FindingDisposition,
  FindingDispositions,
  Report,
  Run,
  ReviewCreateRequest,
  ReviewPreviewResponse,
  WorkItem,
} from '../types/review';
import type { ReviewCapabilities } from '../types/reviewPlan';
import type {
  ModelCatalogResponse,
  ReviewTaskListResponse,
  SystemDiagnostics,
} from '../types/operations';
import type {
  PublicationAccepted,
  PublicationAttempt,
  PublicationPlan,
  PublicationPreflightResult,
  PublicationSelection,
} from '../types/publication';
import { publicationErrorText } from '../utils/publicationPresentation';

const API_BASE = import.meta.env.VITE_API_BASE ?? '';
const retryableCreateStatuses = new Set([408, 429, 500, 502, 503, 504]);

export class ApiError extends Error {
  constructor(message: string, readonly status: number | null) {
    super(message);
    this.name = 'ApiError';
  }
}

function apiPath(path: string) {
  return API_BASE + path;
}

function reviewPath(id: string, suffix = '') {
  return '/api/reviews/' + encodeURIComponent(id) + suffix;
}

function requestError(payload: unknown, status: number) {
  const detail = (payload as { detail?: unknown } | null)?.detail;
  if (Array.isArray(detail)) {
    const message = detail.map((issue: { loc: (string | number)[]; msg: string }) => {
      const field = issue.loc.filter(part => part !== 'body').join('.');
      return field + ': ' + issue.msg;
    }).join('; ');
    return new ApiError(message, status);
  }
  const message = typeof detail === 'string'
    ? detail
    : typeof detail === 'object' && detail !== null
      ? String((detail as { message?: string; code?: string }).message
        ?? (detail as { code?: string }).code
        ?? '请求失败 (' + status + ')')
      : '请求失败 (' + status + ')';

  return new ApiError(message, status);
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(apiPath(path), {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init.headers },
  });

  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw requestError(payload, response.status);
  }

  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const getReviewCapabilities = () =>
  request<ReviewCapabilities>('/api/system/review-capabilities');
export const getAvailableModels = () => request<ModelCatalogResponse>('/api/system/models');
export const getDiagnostics = () => request<SystemDiagnostics>('/api/system/diagnostics');
export const updateSystemSettings = (value: {
  model_base_url: string;
  model_name: string;
  retention_days: number;
}) => request<SystemDiagnostics>('/api/system/settings', {
  method: 'PATCH',
  body: JSON.stringify(value),
});
export const getPublicationCapability = () =>
  request<import('../types/operations').PublicationCapability>('/api/system/publication-capability');
export const getReview = (id: string, signal?: AbortSignal) =>
  request<Run>(reviewPath(id), { signal });

export function subscribeToReview(id: string, refresh: () => void) {
  const stream = new EventSource(apiPath(reviewPath(id, '/events')));
  for (const event of ['task', 'progress', 'done']) stream.addEventListener(event, refresh);
  stream.onerror = () => stream.close();
  return () => stream.close();
}

export const listReviews = (query: string, page: number, signal?: AbortSignal) =>
  request<ReviewTaskListResponse>(
    '/api/reviews?q=' + encodeURIComponent(query) + '&page=' + page + '&page_size=20',
    { signal },
  );
export const deleteReview = (id: string) => request<void>(reviewPath(id), { method: 'DELETE' });
export const cancelReview = (id: string) =>
  request<{ status: string }>(reviewPath(id, '/cancel'), { method: 'POST' });
export const retryReview = (id: string) => request<Run>(reviewPath(id, '/retry'), { method: 'POST' });
export const retryOverview = (id: string) =>
  request<Run>(reviewPath(id, '/overview/retry'), { method: 'POST' });

function reviewBody(input: ReviewCreateRequest) {
  return {
    pr_url: input.pr_url,
    depth: input.depth,
    paths: input.paths,
    focus: input.focus,
    limits: Object.fromEntries(
      Object.entries(input.limits).filter(([, value]) => value != null),
    ),
    ...(input.model ? { model: input.model } : {}),
    ...(input.include_pr_discussions === undefined
      ? {}
      : { include_pr_discussions: input.include_pr_discussions }),
  };
}

function aborted(signal: AbortSignal | undefined, error: unknown) {
  return signal?.aborted || (error instanceof DOMException && error.name === 'AbortError');
}

function retryableCreateError(error: unknown) {
  return !(error instanceof ApiError) || retryableCreateStatuses.has(error.status ?? 0);
}

export async function createReview(
  input: ReviewCreateRequest,
  signal?: AbortSignal,
  key = crypto.randomUUID(),
): Promise<Run> {
  const init: RequestInit = {
    method: 'POST',
    headers: { 'Idempotency-Key': key },
    body: JSON.stringify(reviewBody(input)),
    signal,
  };

  try {
    return await request<Run>('/api/reviews', init);
  } catch (error) {
    if (aborted(signal, error) || !retryableCreateError(error)) throw error;
    return request<Run>('/api/reviews', init);
  }
}

export const previewReview = (input: ReviewCreateRequest, signal?: AbortSignal) =>
  // Preview is deterministic; semantic grouping is frozen by the review worker.
  request<ReviewPreviewResponse>('/api/reviews/preview', {
    method: 'POST',
    body: JSON.stringify(reviewBody(input)),
    signal,
  });
export const getUnits = (id: string, signal?: AbortSignal) =>
  request<WorkItem[]>(reviewPath(id, '/units'), { signal });
export const getFindings = (id: string, signal?: AbortSignal) =>
  request<{
    findings: Finding[];
    candidates: Candidate[];
    report_version: string | null;
    dispositions: FindingDispositions;
  }>(reviewPath(id, '/findings'), { signal });
export const updateFindingDisposition = (
  reviewId: string,
  findingId: string,
  value: FindingDisposition,
  signal?: AbortSignal,
) => request<{
  findings: Finding[];
  candidates: Candidate[];
  report_version: string | null;
  dispositions: FindingDispositions;
}>(reviewPath(reviewId, '/findings/' + encodeURIComponent(findingId) + '/disposition'), {
  method: 'PATCH',
  body: JSON.stringify(value),
  signal,
});
export const getReport = (id: string, signal?: AbortSignal) =>
  request<{ report: Report | null; markdown: string }>(reviewPath(id, '/report'), { signal });
export const getFileContext = (id: string, file: number, signal?: AbortSignal) =>
  request<FileContext>(reviewPath(id, '/files/' + file + '/context'), { signal });

async function publicationRequest<T>(path: string, token: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(apiPath(path), {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      Authorization: 'Bearer ' + token,
      ...init.headers,
    },
  });

  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const code = typeof payload?.detail?.code === 'string'
      ? payload.detail.code
      : 'HTTP ' + response.status;
    throw new Error(publicationErrorText(code));
  }

  return response.json() as Promise<T>;
}

export const preflightPublication = (reviewId: string, keys: string[], token: string, signal?: AbortSignal) =>
  publicationRequest<PublicationPreflightResult>(reviewPath(reviewId, '/publication-preflight'), token, {
    method: 'POST',
    body: JSON.stringify({ finding_keys: keys }),
    signal,
  });
export const createPublication = (reviewId: string, selection: PublicationSelection, key: string, token: string, signal?: AbortSignal) =>
  publicationRequest<PublicationAccepted>(reviewPath(reviewId, '/publications'), token, {
    method: 'POST',
    headers: { 'Idempotency-Key': key },
    body: JSON.stringify(selection),
    signal,
  });
export const getPublications = (reviewId: string, token: string, signal?: AbortSignal) =>
  publicationRequest<PublicationAttempt[]>(reviewPath(reviewId, '/publications'), token, { signal });
export const getPublication = (reviewId: string, identity: string, token: string, signal?: AbortSignal) =>
  publicationRequest<PublicationAttempt>(
    reviewPath(reviewId, '/publications/' + encodeURIComponent(identity)),
    token,
    { signal },
  );
export const retryPublication = (reviewId: string, identity: string, token: string, signal?: AbortSignal) =>
  publicationRequest<PublicationAttempt>(
    reviewPath(reviewId, '/publications/' + encodeURIComponent(identity) + '/retry'),
    token,
    { method: 'POST', signal },
  );
export const getPublicationPlan = (taskId: string, signal?: AbortSignal) =>
  request<PublicationPlan>(reviewPath(taskId, '/publication-plan'), { signal });
export const checkReviewComment = (
  taskId: string,
  signal?: AbortSignal,
  options?: import('../types/publication').ReviewCommentOptions,
) => request<import('../types/publication').ReviewCommentCheck>(reviewPath(taskId, '/review-comment-check'), {
  method: 'POST',
  signal,
  body: JSON.stringify(options),
});

