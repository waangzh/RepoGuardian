import type { ReviewCreateResponse, ReviewTask } from "../types/review";

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

export async function createReview(prUrl: string, model?: string): Promise<ReviewCreateResponse> {
  return request<ReviewCreateResponse>("/api/reviews", {
    method: "POST",
    body: JSON.stringify({ pr_url: prUrl, model: model || null })
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

