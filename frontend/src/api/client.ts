import type {
  ReviewCreateResponse,
  ReviewMode,
  ReviewTask,
  ValidationBackend,
} from "../types/review";

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

export async function getReview(taskId: string): Promise<ReviewTask> {
  return request<ReviewTask>(`/api/reviews/${taskId}`);
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
    onStepProgress?: (data: { node: string; status: string; message?: string }) => void;
    onDone?: (data: { status: string }) => void;
    onError?: (data: { message: string }) => void;
  }
): EventSource {
  const es = new EventSource(`${API_BASE}/api/reviews/${taskId}/stream`);
  es.addEventListener("step_progress", (e: MessageEvent) => {
    callbacks.onStepProgress?.(JSON.parse(e.data));
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

