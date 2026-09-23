/**
 * `NamespacesPage` tests — Story 3.2 code review, IG2; Story 3.6 T12.6.
 *
 * A corrupted `retention_policy` used to render exactly like a legitimate
 * unlimited `client` namespace. `retention_policy_valid: false` must
 * surface as a distinct, visible signal instead.
 *
 * Rendered through a real `RouterProvider` (not the page component in
 * isolation) since Story 3.6 T12.6 turns each namespace name into a
 * `<Link>` to its detail route — `useLinkProps` throws outside a router
 * context.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { Providers } from "@/app/providers";
import { routeTree } from "@/app/routeTree.gen";
import { queryClient } from "@/shared/api/queryClient";

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function renderRoute(path: string) {
  const router = createRouter({
    routeTree,
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  return render(
    <Providers>
      <RouterProvider router={router} />
    </Providers>,
  );
}

describe("NamespacesPage", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    cleanup();
    // `Providers` wraps a MODULE-LEVEL singleton `QueryClient`
    // (`@/shared/api/queryClient`), shared across every test in this file —
    // without clearing it, the second test's ["memory-namespaces"] query
    // would serve the first test's cached (and by-then-stale in intent,
    // not by `staleTime`) fixture instead of refetching.
    queryClient.clear();
  });

  it("flags a namespace with an invalid retention_policy instead of showing 'illimité'", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      jsonResponse([
        {
          namespace_id: "11111111-1111-1111-1111-111111111111",
          name: "legacy-corrupt",
          type: "client",
          department: null,
          project: null,
          retention_policy: { default_ttl_seconds: null, archive_after_seconds: null },
          retention_policy_valid: false,
          decay_policy: {},
          decay_policy_valid: true,
          embedding_backend: "cloud",
          chunk_count: 0,
          created_at: new Date().toISOString(),
        },
      ]),
    );

    renderRoute("/config/namespaces");

    await waitFor(() => expect(screen.getByText("legacy-corrupt")).toBeInTheDocument());
    expect(screen.getByText("donnée invalide")).toBeInTheDocument();
    expect(screen.queryByText("illimité")).not.toBeInTheDocument();
  });

  it("renders a valid retention_policy normally", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      jsonResponse([
        {
          namespace_id: "22222222-2222-2222-2222-222222222222",
          name: "legit-client",
          type: "client",
          department: null,
          project: null,
          retention_policy: { default_ttl_seconds: null, archive_after_seconds: null },
          retention_policy_valid: true,
          decay_policy: {},
          decay_policy_valid: true,
          embedding_backend: "cloud",
          chunk_count: 0,
          created_at: new Date().toISOString(),
        },
      ]),
    );

    renderRoute("/config/namespaces");

    await waitFor(() => expect(screen.getByText("legit-client")).toBeInTheDocument());
    expect(screen.getByText("illimité")).toBeInTheDocument();
    expect(screen.queryByText("donnée invalide")).not.toBeInTheDocument();
  });

  it("navigates to the namespace detail route when its name is clicked", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/v1/memory/namespaces") && !url.includes("/api/v1/memory/chunks")) {
        return Promise.resolve(
          jsonResponse([
            {
              namespace_id: "33333333-3333-3333-3333-333333333333",
              name: "click-me",
              type: "metier",
              department: null,
              project: null,
              retention_policy: { default_ttl_seconds: null, archive_after_seconds: null },
              retention_policy_valid: true,
              decay_policy: {},
              decay_policy_valid: true,
              embedding_backend: "cloud",
              chunk_count: 0,
              created_at: new Date().toISOString(),
            },
          ]),
        );
      }
      return Promise.resolve(jsonResponse([]));
    });

    renderRoute("/config/namespaces");

    const link = await screen.findByTestId("namespace-link-33333333-3333-3333-3333-333333333333");
    link.click();

    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "click-me" })).toBeInTheDocument(),
    );
  });
});
