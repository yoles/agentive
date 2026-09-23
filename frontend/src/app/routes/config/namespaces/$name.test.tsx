/**
 * `/config/namespaces/$name` route tests — Story 3.6 AC5.
 *
 * Covers: chunk table rendering, the "tag"/content filter, and the purge
 * confirmation flow (select → confirm → DELETE per chunk).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { Providers } from "@/app/providers";
import { routeTree } from "@/app/routeTree.gen";
import { queryClient } from "@/shared/api/queryClient";

function jsonResponse(body: unknown, init: ResponseInit = { status: 200 }) {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    ...init,
    headers: {
      "Content-Type":
        init.status && init.status >= 400 ? "application/problem+json" : "application/json",
      ...(init.headers ?? {}),
    },
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

const NAMESPACE_FIXTURE = {
  namespace_id: "11111111-1111-1111-1111-111111111111",
  name: "dev-notes",
  type: "metier",
  department: "Dev",
  project: null,
  retention_policy: { default_ttl_seconds: 31_536_000, archive_after_seconds: null },
  retention_policy_valid: true,
  decay_policy: {},
  decay_policy_valid: true,
  embedding_backend: "local",
  chunk_count: 2,
  created_at: new Date().toISOString(),
};

const CHUNK_A = {
  chunk_id: "aaaaaaaa-1111-1111-1111-111111111111",
  namespace: "dev-notes",
  content: "first chunk content",
  created_at: new Date().toISOString(),
  expires_at: null,
  archived_at: null,
};

const CHUNK_B = {
  chunk_id: "bbbbbbbb-2222-2222-2222-222222222222",
  namespace: "dev-notes",
  content: "second chunk content",
  created_at: new Date().toISOString(),
  expires_at: null,
  archived_at: null,
};

describe("/config/namespaces/$name", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    // See `index.test.tsx` — `Providers` shares a module-level QueryClient
    // singleton across every test in this file.
    queryClient.clear();
  });

  function mockDefaultRoutes(chunks: unknown[] = [CHUNK_A, CHUNK_B]) {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/v1/memory/chunks")) {
        return Promise.resolve(jsonResponse(chunks));
      }
      if (url.includes("/api/v1/memory/namespaces")) {
        return Promise.resolve(jsonResponse([NAMESPACE_FIXTURE]));
      }
      return Promise.resolve(jsonResponse({}, { status: 404 }));
    });
  }

  it("renders the namespace header info and the chunk table", async () => {
    mockDefaultRoutes();

    renderRoute("/config/namespaces/dev-notes");

    await screen.findByRole("heading", { name: "dev-notes" });
    expect(await screen.findByTestId("namespace-embedding-backend")).toHaveTextContent("local");
    expect(await screen.findByText("first chunk content")).toBeInTheDocument();
    expect(screen.getByText("second chunk content")).toBeInTheDocument();
  });

  it("sends content_contains to the API when the tag filter is typed", async () => {
    mockDefaultRoutes([CHUNK_A]);

    renderRoute("/config/namespaces/dev-notes");
    await screen.findByRole("heading", { name: "dev-notes" });

    fireEvent.change(screen.getByTestId("chunk-filter-content"), {
      target: { value: "invoice" },
    });

    await waitFor(() => {
      const calls = fetchMock.mock.calls.map((args) => String(args[0]));
      expect(calls.some((u) => u.includes("content_contains=invoice"))).toBe(true);
    });
  });

  it("purges the selected chunks via DELETE and refreshes the table", async () => {
    mockDefaultRoutes();

    renderRoute("/config/namespaces/dev-notes");
    await screen.findByText("first chunk content");

    fireEvent.click(
      screen.getByRole("checkbox", { name: /Sélectionner le chunk aaaaaaaa/i }),
    );
    fireEvent.click(screen.getByTestId("purge-selected-button"));

    const confirmButton = await screen.findByTestId("purge-chunks-confirm");

    // One persistent, stateful mock rather than a queue of `...Once`
    // implementations: the purge triggers a query invalidation, so the
    // chunk table refetches GET /memory/chunks right after the DELETE —
    // both must be handled by whatever mock is active at that moment.
    const purged = new Set<string>();
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (init?.method === "DELETE") {
        expect(url).toContain(`/api/v1/memory/chunks/${CHUNK_A.chunk_id}`);
        purged.add(CHUNK_A.chunk_id);
        return Promise.resolve(jsonResponse(undefined, { status: 204 }));
      }
      if (url.includes("/api/v1/memory/chunks")) {
        const remaining = [CHUNK_A, CHUNK_B].filter((c) => !purged.has(c.chunk_id));
        return Promise.resolve(jsonResponse(remaining));
      }
      return Promise.resolve(jsonResponse([NAMESPACE_FIXTURE]));
    });

    fireEvent.click(confirmButton);

    await waitFor(() => expect(screen.queryByText("first chunk content")).not.toBeInTheDocument());
    expect(screen.getByText("second chunk content")).toBeInTheDocument();
  });

  it("reports an individual failure without blocking the rest of the batch", async () => {
    mockDefaultRoutes();

    renderRoute("/config/namespaces/dev-notes");
    await screen.findByText("first chunk content");

    fireEvent.click(screen.getByTestId("select-all-chunks"));
    fireEvent.click(screen.getByTestId("purge-selected-button"));
    const confirmButton = await screen.findByTestId("purge-chunks-confirm");

    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (init?.method === "DELETE" && url.includes(CHUNK_A.chunk_id)) {
        return Promise.resolve(jsonResponse({ type: "/errors/not-found" }, { status: 404 }));
      }
      if (init?.method === "DELETE") {
        return Promise.resolve(jsonResponse(undefined, { status: 204 }));
      }
      if (url.includes("/api/v1/memory/chunks")) {
        return Promise.resolve(jsonResponse([CHUNK_A]));
      }
      return Promise.resolve(jsonResponse([NAMESPACE_FIXTURE]));
    });

    fireEvent.click(confirmButton);

    // Both DELETE calls fire (Promise.allSettled, not Promise.all) even
    // though one of them 404s.
    await waitFor(() => {
      const deleteCalls = fetchMock.mock.calls.filter(
        (args) => (args[1] as RequestInit | undefined)?.method === "DELETE",
      );
      expect(deleteCalls.length).toBe(2);
    });
  });
});
