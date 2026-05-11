/**
 * Component tests — PlaygroundPage (Story 2.7 T8.1).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createRouter,
  createMemoryHistory,
  RouterProvider,
  createRootRoute,
  createRoute,
  Outlet,
} from "@tanstack/react-router";
import type { ReactNode } from "react";
import { PlaygroundPage } from "./PlaygroundPage";

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

function makeRouter(children: ReactNode) {
  const rootRoute = createRootRoute({ component: () => <Outlet /> });
  const indexRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/",
    component: () => <>{children}</>,
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([indexRoute]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  return router;
}

function Providers({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const router = makeRouter(children);
  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}

describe("PlaygroundPage", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("empty state — no run yet", async () => {
    // useAgentTools fetches the assigned tools at mount.
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith(`/agents/templates/${TEMPLATE_ID}/tools`)) {
        return Promise.resolve(
          jsonResponse({ template_id: TEMPLATE_ID, assigned_tools: [] }),
        );
      }
      return Promise.resolve(jsonResponse({}, { status: 404 }));
    });
    render(
      <Providers>
        <PlaygroundPage templateId={TEMPLATE_ID} />
      </Providers>,
    );
    await waitFor(() => {
      expect(screen.getByTestId("playground-page")).toBeInTheDocument();
    });
    expect(screen.getByTestId("playground-output-empty")).toBeInTheDocument();
    expect(screen.getByTestId("playground-run-button")).toBeInTheDocument();
  });

  it("runs the agent and renders the output tabs", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      if (
        url.endsWith(`/agents/templates/${TEMPLATE_ID}/tools`) &&
        method === "GET"
      ) {
        return Promise.resolve(
          jsonResponse({ template_id: TEMPLATE_ID, assigned_tools: [] }),
        );
      }
      if (
        url.endsWith(`/playground/agents/${TEMPLATE_ID}/run`) &&
        method === "POST"
      ) {
        return Promise.resolve(
          jsonResponse({
            prompt_resolved: "you are useful",
            raw_output: '{"summary": "hi"}',
            parsed_output: { summary: "hi" },
            tokens: { input_tokens: 42, output_tokens: 84 },
            cost_estimate_usd: "0.001",
            model_used: "claude-sonnet-4-6",
            provider_used: "anthropic",
            tool_invocations: [],
            duration_ms_total: 123,
          }),
        );
      }
      return Promise.resolve(jsonResponse({}, { status: 404 }));
    });
    render(
      <Providers>
        <PlaygroundPage templateId={TEMPLATE_ID} />
      </Providers>,
    );
    const runBtn = await screen.findByTestId("playground-run-button");
    fireEvent.click(runBtn);
    await waitFor(() => {
      expect(screen.getByTestId("playground-output-result")).toBeInTheDocument();
    });
    // Default tab = raw.
    expect(screen.getByTestId("playground-tab-raw-content")).toHaveTextContent(
      '{"summary": "hi"}',
    );
  });

  it("shows JSON parse error when arguments textarea has invalid JSON", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith(`/agents/templates/${TEMPLATE_ID}/tools`)) {
        return Promise.resolve(
          jsonResponse({ template_id: TEMPLATE_ID, assigned_tools: [] }),
        );
      }
      return Promise.resolve(jsonResponse({}, { status: 404 }));
    });
    render(
      <Providers>
        <PlaygroundPage templateId={TEMPLATE_ID} />
      </Providers>,
    );
    const textarea = await screen.findByTestId("playground-arguments-textarea");
    fireEvent.change(textarea, { target: { value: "{invalid json" } });
    fireEvent.click(screen.getByTestId("playground-run-button"));
    await waitFor(() => {
      expect(screen.getByTestId("playground-json-error")).toBeInTheDocument();
    });
  });

  it("displays tokens & cost in the dedicated tab", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.endsWith(`/agents/templates/${TEMPLATE_ID}/tools`)) {
        return Promise.resolve(
          jsonResponse({ template_id: TEMPLATE_ID, assigned_tools: [] }),
        );
      }
      if (
        url.endsWith(`/playground/agents/${TEMPLATE_ID}/run`) &&
        method === "POST"
      ) {
        return Promise.resolve(
          jsonResponse({
            prompt_resolved: "x",
            raw_output: "y",
            parsed_output: null,
            tokens: { input_tokens: 100, output_tokens: 200 },
            cost_estimate_usd: "0.005",
            model_used: "claude-sonnet-4-6",
            provider_used: "anthropic",
            tool_invocations: [],
            duration_ms_total: 50,
          }),
        );
      }
      return Promise.resolve(jsonResponse({}, { status: 404 }));
    });
    render(
      <Providers>
        <PlaygroundPage templateId={TEMPLATE_ID} />
      </Providers>,
    );
    fireEvent.click(await screen.findByTestId("playground-run-button"));
    await waitFor(() => {
      expect(screen.getByTestId("playground-output-result")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("playground-tab-tokens"));
    const tokens = screen.getByTestId("playground-tab-tokens-content");
    expect(tokens).toHaveTextContent("100");
    expect(tokens).toHaveTextContent("200");
    expect(tokens).toHaveTextContent("0.005");
  });
});
