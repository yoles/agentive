/**
 * HTTP client — same-origin fetch wrapper.
 *
 * Auth strategy (Sprint 0) :
 * - All API calls are RELATIVE (`/api/v1/...`) and routed through Caddy
 *   (same-origin on :8443 dev, :443 prod). No CORS, no Authorization header
 *   baked into the bundle.
 * - Story 1.7 uses one static Bearer token for the single-user MVP. The
 *   plaintext token is entered at runtime and kept only in this module's
 *   memory; it is never persisted or embedded in the Vite bundle.
 * - `credentials: "include"` remains ready for the HTTP-only session cookie
 *   flow planned for Growth.
 *
 * Critical : NEVER read `import.meta.env.VITE_*` for secrets — Vite inlines
 * them into the client bundle, making any secret PUBLIC. Any auth value must
 * come from a cookie or login flow, not build-time env.
 */

/** API base — relative by default (same-origin via Caddy). Override only if
 *  the frontend is genuinely talking to a different origin during dev (rare). */
const API_BASE =
  (import.meta.env.VITE_API_BASE as string | undefined)?.trim() || "";

let apiToken: string | null = null;

/** Configure the single-user MVP token for this browser tab. */
export function setApiToken(token: string): void {
  const trimmed = token.trim();
  apiToken = trimmed.length > 0 ? trimmed : null;
}

/** Forget the in-memory token, primarily for logout and isolated tests. */
export function clearApiToken(): void {
  apiToken = null;
}

export function isApiTokenConfigured(): boolean {
  return apiToken !== null;
}

export type ApiError = {
  type: string;
  title: string;
  status: number;
  detail?: string;
  correlation_id?: string;
};

const ERROR_NETWORK: Omit<ApiError, "status"> = {
  type: "/errors/network",
  title: "Network error",
  detail: "Could not reach the server. Check your connection.",
};

function isApiError(x: unknown): x is ApiError {
  return (
    typeof x === "object" &&
    x !== null &&
    "type" in x &&
    "title" in x &&
    "status" in x
  );
}

async function safeParseJson<T>(response: Response): Promise<T | undefined> {
  if (response.status === 204) return undefined;
  const text = await response.text();
  if (text.length === 0) return undefined;
  try {
    return JSON.parse(text) as T;
  } catch {
    return undefined;
  }
}

export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (!headers.has("Content-Type") && init.body) {
    headers.set("Content-Type", "application/json");
  }
  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }
  if (!headers.has("Authorization") && apiToken !== null) {
    headers.set("Authorization", `Bearer ${apiToken}`);
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers,
      credentials: "include", // Session cookie flow (Story 1.7+)
    });
  } catch (cause) {
    // fetch rejects only for network-level failures (DNS, offline, CORS).
    const err: ApiError = { ...ERROR_NETWORK, status: 0 };
    throw Object.assign(err, { cause });
  }

  if (!response.ok) {
    const parsed = await safeParseJson<ApiError>(response);
    if (isApiError(parsed)) {
      throw parsed;
    }
    // Fallback: synthesize ApiError from status code.
    const err: ApiError = {
      type: "/errors/unknown",
      title: response.statusText || "Request failed",
      status: response.status,
    };
    throw err;
  }

  const body = await safeParseJson<T>(response);
  // 204 / empty body : caller MUST type as `T | undefined` or use `Promise<void>`.
  // We cast to T here — callers must ensure their type matches.
  return body as T;
}
