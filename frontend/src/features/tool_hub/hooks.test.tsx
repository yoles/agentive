/**
 * Hook tests — Tool Hub (Story 2.5 T9.6 / AC8).
 *
 * Verrouille les invariants des hooks isolated :
 * 1. URL + method + body shape pour chaque API wrapper.
 * 2. Invalidation chains (useReplaceAgentTools, useDeleteAgentTool,
 *    useCreateToolServer).
 * 3. UUID guard sur useAgentTools.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import {
  useAgentTools,
  useCreateToolServer,
  useDeleteAgentTool,
  useReplaceAgentTools,
  useToolServers,
} from "./hooks";
import { listToolServers } from "./api";

const TEMPLATE_ID = "11111111-2222-3333-4444-555555555555";
const SERVER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
const TOOL_ID = "33333333-3333-3333-3333-333333333333";

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

describe("Tool Hub api wrappers", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("listToolServers GETs /api/v1/tools/servers", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse([]));
    await listToolServers();
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/v1/tools/servers");
    expect((init as RequestInit | undefined)?.method ?? "GET").toBe("GET");
  });

  it("useCreateToolServer POSTs body and invalidates ['tool-servers']", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        {
          server_id: SERVER_ID,
          name: "smoke",
          transport: "stdio",
          status: "active",
          connection_config: { command: "x" },
          discovered_at: new Date().toISOString(),
          tools: [],
        },
        { status: 201 },
      ),
    );
    const { queryClient, wrapper } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useCreateToolServer(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({
        name: "smoke",
        transport: "stdio",
        connection_config: { command: "python" },
      });
    });

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/v1/tools/servers");
    expect((init as RequestInit).method).toBe("POST");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      name: "smoke",
      transport: "stdio",
      connection_config: { command: "python" },
    });

    await waitFor(() => {
      const calls = invalidateSpy.mock.calls.map(
        ([opts]) => (opts as { queryKey: unknown[] }).queryKey,
      );
      expect(calls).toContainEqual(["tool-servers"]);
    });
  });

  it("useReplaceAgentTools POSTs REPLACE body + invalidates per-template tools queryKey", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        template_id: TEMPLATE_ID,
        assigned_tools: [
          {
            tool_id: TOOL_ID,
            name: "tool",
            description: "",
            server_id: SERVER_ID,
          },
        ],
      }),
    );
    const { queryClient, wrapper } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useReplaceAgentTools(TEMPLATE_ID), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ tool_ids: [TOOL_ID] });
    });

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(
      `/api/v1/agents/templates/${TEMPLATE_ID}/tools`,
    );
    expect((init as RequestInit).method).toBe("POST");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      tool_ids: [TOOL_ID],
    });

    await waitFor(() => {
      const calls = invalidateSpy.mock.calls.map(
        ([opts]) => (opts as { queryKey: unknown[] }).queryKey,
      );
      expect(calls).toContainEqual(["agent-template", TEMPLATE_ID, "tools"]);
    });
  });

  it("useDeleteAgentTool DELETEs + invalidates per-template tools queryKey", async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    const { queryClient, wrapper } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useDeleteAgentTool(TEMPLATE_ID), { wrapper });

    await act(async () => {
      await result.current.mutateAsync(TOOL_ID);
    });

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(
      `/api/v1/agents/templates/${TEMPLATE_ID}/tools/${TOOL_ID}`,
    );
    expect((init as RequestInit).method).toBe("DELETE");

    await waitFor(() => {
      const calls = invalidateSpy.mock.calls.map(
        ([opts]) => (opts as { queryKey: unknown[] }).queryKey,
      );
      expect(calls).toContainEqual(["agent-template", TEMPLATE_ID, "tools"]);
    });
  });

  it("useAgentTools gates fetch on a valid UUID", async () => {
    const { wrapper } = makeWrapper();
    renderHook(() => useAgentTools("not-a-uuid"), { wrapper });
    renderHook(() => useAgentTools(""), { wrapper });
    renderHook(() => useAgentTools(null), { wrapper });
    expect(fetchMock).not.toHaveBeenCalled();

    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        template_id: TEMPLATE_ID,
        assigned_tools: [],
      }),
    );
    const { result } = renderHook(() => useAgentTools(TEMPLATE_ID), { wrapper });
    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(`/api/v1/agents/templates/${TEMPLATE_ID}/tools`);
  });

  it("useToolServers GETs the list", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse([]));
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useToolServers(), { wrapper });
    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/v1/tools/servers");
  });
});
