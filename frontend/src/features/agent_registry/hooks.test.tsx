/**
 * Hook tests — `useUpdateTemplate` (Story 2.2 P-08 fix from code-review 2026-05-09).
 *
 * AC7 lettre demande ≥2 tests sur le hook isolated. Le test page
 * (`$templateId.test.tsx`) exerce le hook indirectement via la mutation ;
 * ces tests-ci verrouillent les invariants du hook seul :
 *
 * 1. `onSuccess` invalide à la fois `["agent-template", id]` et
 *    `["agent-templates"]` (pour que la liste future et le détail
 *    courant refetchent).
 * 2. `onError` propage l'`ApiError` brute au caller (pas de masquage),
 *    pour que la page puisse afficher un toast contextuel.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useInstance, useInstantiateTemplate, useUpdateTemplate } from "./hooks";
import { getInstance } from "./api";

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

describe("useUpdateTemplate", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("invalidates both ['agent-template', id] and ['agent-templates'] on success", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        template_id: TEMPLATE_ID,
        name: "X",
        archetype: "producteur",
        version: 2,
        config: {},
        updated_at: new Date().toISOString(),
      }),
    );
    const { queryClient, wrapper } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    const { result } = renderHook(() => useUpdateTemplate(TEMPLATE_ID), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ system_prompt: "v2" });
    });

    await waitFor(() => {
      const calls = invalidateSpy.mock.calls.map(([opts]) => (opts as { queryKey: unknown[] }).queryKey);
      expect(calls).toContainEqual(["agent-template", TEMPLATE_ID]);
      expect(calls).toContainEqual(["agent-templates"]);
    });
  });

  it("propagates RFC 7807 ApiError on failure (caller can show targeted toast)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        {
          type: "/errors/validation",
          title: "Validation failed",
          status: 422,
          detail: "system_prompt must not be blank",
          correlation_id: "01999999-9999-7999-8999-999999999999",
        },
        { status: 422 },
      ),
    );
    const { wrapper } = makeWrapper();

    const { result } = renderHook(() => useUpdateTemplate(TEMPLATE_ID), { wrapper });

    await expect(
      result.current.mutateAsync({ system_prompt: "" } as unknown as Parameters<
        typeof result.current.mutateAsync
      >[0]),
    ).rejects.toMatchObject({
      type: "/errors/validation",
      title: "Validation failed",
      status: 422,
      detail: "system_prompt must not be blank",
    });
  });
});

describe("useInstantiateTemplate (Story 2.4 T8.5)", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("POSTs to /agents/templates/{id}/instances with the body", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        instance_id: "22222222-2222-2222-2222-222222222222",
        template_id: TEMPLATE_ID,
        template_version: 1,
        workflow_run_id: null,
        snapshot: {
          template_id: TEMPLATE_ID,
          template_version: 1,
          name: "x",
          archetype: "producteur",
          config: {},
        },
        created_at: new Date().toISOString(),
      }),
    );
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useInstantiateTemplate(TEMPLATE_ID), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({});
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(`/api/v1/agents/templates/${TEMPLATE_ID}/instances`);
    expect((init as RequestInit).method).toBe("POST");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({});
  });

  it("getInstance(instanceId) GETs /api/v1/agents/instances/{id} (P-03 CR — AC7 spec test 2)", async () => {
    const INSTANCE_ID = "44444444-4444-4444-4444-444444444444";
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        instance_id: INSTANCE_ID,
        template_id: TEMPLATE_ID,
        template_version: 2,
        workflow_run_id: null,
        snapshot: {
          template_id: TEMPLATE_ID,
          template_version: 2,
          name: "x",
          archetype: "producteur",
          config: {},
        },
        created_at: new Date().toISOString(),
      }),
    );

    const result = await getInstance(INSTANCE_ID);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(`/api/v1/agents/instances/${INSTANCE_ID}`);
    // GET = no method override (default), or "GET". Either way no body.
    expect((init as RequestInit | undefined)?.method ?? "GET").toBe("GET");
    expect((init as RequestInit | undefined)?.body).toBeUndefined();
    expect(result.instance_id).toBe(INSTANCE_ID);
    expect(result.template_version).toBe(2);
  });

  it("useInstance gates fetch on a valid UUID (P-03 CR — guard against malformed param)", async () => {
    const { wrapper } = makeWrapper();
    // Render with non-UUID — `enabled` should be false, no fetch fired.
    renderHook(() => useInstance("not-a-uuid"), { wrapper });
    // Render with empty string — same.
    renderHook(() => useInstance(""), { wrapper });
    // Render with null — same.
    renderHook(() => useInstance(null), { wrapper });
    expect(fetchMock).not.toHaveBeenCalled();

    // Now render with a valid UUID — fetch fires.
    const VALID_ID = "55555555-5555-5555-5555-555555555555";
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        instance_id: VALID_ID,
        template_id: TEMPLATE_ID,
        template_version: 1,
        workflow_run_id: null,
        snapshot: {},
        created_at: new Date().toISOString(),
      }),
    );
    const { result } = renderHook(() => useInstance(VALID_ID), { wrapper });
    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(`/api/v1/agents/instances/${VALID_ID}`);
  });

  it("invalidates ['agent-template', templateId, 'instances'] on success", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        instance_id: "55555555-5555-5555-5555-555555555555",
        template_id: TEMPLATE_ID,
        template_version: 1,
        workflow_run_id: null,
        snapshot: {},
        created_at: new Date().toISOString(),
      }),
    );
    const { queryClient, wrapper } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    const { result } = renderHook(() => useInstantiateTemplate(TEMPLATE_ID), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({});
    });

    await waitFor(() => {
      const calls = invalidateSpy.mock.calls.map(
        ([opts]) => (opts as { queryKey: unknown[] }).queryKey,
      );
      expect(calls).toContainEqual(["agent-template", TEMPLATE_ID, "instances"]);
    });
  });
});
