/**
 * Hook tests — Memory Manager namespaces (Story 3.2 T8.6).
 *
 * Verrouille les invariants des hooks isolated :
 * 1. URL + method + body shape pour chaque API wrapper.
 * 2. Invalidation chain (useCreateNamespace → ['memory-namespaces']).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useCreateNamespace, useNamespaces } from "./hooks";
import { listNamespaces } from "./api";

function jsonResponse(body: unknown, init: ResponseInit = { status: 200 }) {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: {
      "Content-Type":
        init.status && init.status >= 400 ? "application/problem+json" : "application/json",
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

describe("Memory Manager namespaces hooks", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("listNamespaces GETs /api/v1/memory/namespaces", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse([]));
    await listNamespaces();
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/v1/memory/namespaces");
    expect((init as RequestInit | undefined)?.method ?? "GET").toBe("GET");
  });

  it("useNamespaces surfaces the flat list from the API", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse([
        {
          namespace_id: "11111111-1111-1111-1111-111111111111",
          name: "dev-notes",
          type: "metier",
          department: "Dev",
          project: null,
          retention_policy: { default_ttl_seconds: 31_536_000, archive_after_seconds: null },
          embedding_backend: "cloud",
          chunk_count: 3,
          created_at: new Date().toISOString(),
        },
      ]),
    );
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useNamespaces(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.[0].name).toBe("dev-notes");
    expect(result.current.data?.[0].chunk_count).toBe(3);
  });

  it("useCreateNamespace POSTs body and invalidates ['memory-namespaces']", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        {
          namespace_id: "22222222-2222-2222-2222-222222222222",
          name: "dev-notes",
          type: "metier",
          department: "Dev",
          project: null,
          retention_policy: { default_ttl_seconds: 31_536_000, archive_after_seconds: null },
          embedding_backend: "cloud",
          created_at: new Date().toISOString(),
        },
        { status: 201 },
      ),
    );
    const { queryClient, wrapper } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useCreateNamespace(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ name: "dev-notes", type: "metier", department: "Dev" });
    });

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/v1/memory/namespaces");
    expect((init as RequestInit).method).toBe("POST");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      name: "dev-notes",
      type: "metier",
      department: "Dev",
    });

    await waitFor(() => {
      const calls = invalidateSpy.mock.calls.map(
        ([opts]) => (opts as { queryKey: unknown[] }).queryKey,
      );
      expect(calls).toContainEqual(["memory-namespaces"]);
    });
  });
});
