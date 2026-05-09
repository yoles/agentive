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
import { useUpdateTemplate } from "./hooks";

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
