/**
 * `NamespacesPage` tests — Story 3.2 code review, IG2.
 *
 * A corrupted `retention_policy` used to render exactly like a legitimate
 * unlimited `client` namespace. `retention_policy_valid: false` must
 * surface as a distinct, visible signal instead.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { NamespacesPage } from "./index";

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

describe("NamespacesPage", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    cleanup();
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
          embedding_backend: "cloud",
          chunk_count: 0,
          created_at: new Date().toISOString(),
        },
      ]),
    );

    render(<NamespacesPage />, { wrapper: makeWrapper() });

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
          embedding_backend: "cloud",
          chunk_count: 0,
          created_at: new Date().toISOString(),
        },
      ]),
    );

    render(<NamespacesPage />, { wrapper: makeWrapper() });

    await waitFor(() => expect(screen.getByText("legit-client")).toBeInTheDocument());
    expect(screen.getByText("illimité")).toBeInTheDocument();
    expect(screen.queryByText("donnée invalide")).not.toBeInTheDocument();
  });
});
