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
import { useCreateNamespace, useNamespaceChunks, useNamespaces, usePurgeChunk } from "./hooks";
import { listChunks, listNamespaces } from "./api";

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
          retention_policy_valid: true,
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
    expect(result.current.data?.[0].retention_policy_valid).toBe(true);
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

  it("listChunks GETs /api/v1/memory/chunks with query params (Story 3.6 AC5)", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse([]));
    await listChunks({
      namespace: "dev-notes",
      includeArchived: true,
      contentContains: "invoice",
      createdAfter: "2026-01-01T00:00:00.000Z",
      limit: 10,
      offset: 5,
    });
    const [url] = fetchMock.mock.calls[0];
    const parsed = new URL(String(url), "http://test");
    expect(parsed.pathname).toBe("/api/v1/memory/chunks");
    expect(parsed.searchParams.get("namespace")).toBe("dev-notes");
    expect(parsed.searchParams.get("include_archived")).toBe("true");
    expect(parsed.searchParams.get("content_contains")).toBe("invoice");
    expect(parsed.searchParams.get("created_after")).toBe("2026-01-01T00:00:00.000Z");
    expect(parsed.searchParams.get("limit")).toBe("10");
    expect(parsed.searchParams.get("offset")).toBe("5");
  });

  it("useNamespaceChunks surfaces the chunk list for a namespace", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse([
        {
          chunk_id: "44444444-4444-4444-4444-444444444444",
          namespace: "dev-notes",
          content: "hello",
          created_at: new Date().toISOString(),
          expires_at: null,
          archived_at: null,
        },
      ]),
    );
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useNamespaceChunks("dev-notes"), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.[0].content).toBe("hello");
  });

  it("usePurgeChunk DELETEs the chunk and invalidates both query families (Story 3.6 AC1)", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(undefined, { status: 204 }));
    const { queryClient, wrapper } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => usePurgeChunk("dev-notes"), { wrapper });

    await act(async () => {
      await result.current.mutateAsync("55555555-5555-5555-5555-555555555555");
    });

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(
      "/api/v1/memory/chunks/55555555-5555-5555-5555-555555555555",
    );
    expect((init as RequestInit).method).toBe("DELETE");

    await waitFor(() => {
      const calls = invalidateSpy.mock.calls.map(
        ([opts]) => (opts as { queryKey: unknown[] }).queryKey,
      );
      expect(calls).toContainEqual(["memory-chunks", "dev-notes"]);
      expect(calls).toContainEqual(["memory-namespaces"]);
    });
  });
});
