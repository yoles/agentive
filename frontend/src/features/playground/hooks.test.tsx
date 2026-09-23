/**
 * Hook tests — Playground (Story 2.7 T8, P-04 fix-batch 2026-08-31).
 *
 * Covers what `PlaygroundPage.test.tsx` only exercises indirectly through
 * rendered DOM: the raw hook contract (URL/method/body), error mapping
 * into `mutation.error`, and retry semantics (a failed run must NOT
 * auto-retry — no silent double LLM calls).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { usePlaygroundAssignedTools, usePlaygroundRun } from "./hooks";

const TEMPLATE_ID = "11111111-2222-3333-4444-555555555555";

function jsonResponse(body: unknown, init: ResponseInit = { status: 200 }) {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: {
      "Content-Type":
        init.status && init.status >= 400
          ? "application/problem+json"
          : "application/json",
      ...(init.headers ?? {}),
    },
  });
}

function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return { queryClient, wrapper };
}

describe("Playground hooks", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("usePlaygroundRun POSTs to /playground/agents/{id}/run with the request body", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        prompt_resolved: "hi",
        raw_output: "ok",
        parsed_output: null,
        tokens: { input_tokens: 1, output_tokens: 2 },
        cost_estimate_usd: "0.0001",
        model_used: "claude-sonnet-4-6",
        provider_used: "anthropic",
        tool_invocations: [],
        duration_ms_total: 10,
      }),
    );
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => usePlaygroundRun(TEMPLATE_ID), { wrapper });

    result.current.mutate({ arguments: { topic: "x" }, timeout_seconds: 30 });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(`/api/v1/playground/agents/${TEMPLATE_ID}/run`);
    expect((init as RequestInit).method).toBe("POST");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      arguments: { topic: "x" },
      timeout_seconds: 30,
    });
  });

  it("usePlaygroundRun maps an RFC 7807 error body onto mutation.error", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        {
          type: "/errors/validation",
          title: "Validation error",
          status: 422,
          detail: "system_prompt references variable 'topic' not present in arguments",
        },
        { status: 422 },
      ),
    );
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => usePlaygroundRun(TEMPLATE_ID), { wrapper });

    result.current.mutate({ arguments: {}, timeout_seconds: 30 });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect((result.current.error as unknown as { detail?: string }).detail).toBe(
      "system_prompt references variable 'topic' not present in arguments",
    );
  });

  it("usePlaygroundRun does not auto-retry a failed run", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({}, { status: 503 }));
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => usePlaygroundRun(TEMPLATE_ID), { wrapper });

    result.current.mutate({ arguments: {}, timeout_seconds: 30 });

    await waitFor(() => expect(result.current.isError).toBe(true));
    // Exactly one fetch call — a 503 must not trigger a silent, possibly
    // billed, second LLM call.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("usePlaygroundAssignedTools GETs the template's assigned tools", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        template_id: TEMPLATE_ID,
        assigned_tools: [{ tool_id: "t1", name: "echo" }],
      }),
    );
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => usePlaygroundAssignedTools(TEMPLATE_ID), {
      wrapper,
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(`/api/v1/agents/templates/${TEMPLATE_ID}/tools`);
    expect(result.current.data?.assigned_tools).toEqual([{ tool_id: "t1", name: "echo" }]);
  });
});
