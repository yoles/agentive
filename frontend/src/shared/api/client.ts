/**
 * HTTP client — fetch wrapper avec auth token.
 *
 * Sprint 0 minimal. Sera remplacé par un client typé auto-généré via
 * `openapi-typescript` + fonction wrapper (Story 1.1 T5 ou Sprint 1+).
 */

const API_URL =
  (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8000";

const API_TOKEN =
  (import.meta.env.VITE_AGENTIVE_API_TOKEN as string | undefined) ?? "";

export type ApiError = {
  type: string;
  title: string;
  status: number;
  detail?: string;
  correlation_id?: string;
};

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (API_TOKEN && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${API_TOKEN}`);
  }
  if (!headers.has("Content-Type") && init.body) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers,
  });

  if (!response.ok) {
    let body: ApiError;
    try {
      body = (await response.json()) as ApiError;
    } catch {
      body = {
        type: "/errors/network",
        title: response.statusText,
        status: response.status,
      };
    }
    throw body;
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}
