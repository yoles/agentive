/**
 * Component tests — AgentToolsPanel (Story 2.5 T9.6 / AC8).
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
import { AgentToolsPanel } from "./AgentToolsPanel";

const TEMPLATE_ID = "11111111-2222-3333-4444-555555555555";
const SERVER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
const TOOL_ID_A = "11111111-aaaa-aaaa-aaaa-111111111111";
const TOOL_ID_B = "22222222-bbbb-bbbb-bbbb-222222222222";

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
  const toolsRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/config/tools",
    component: () => <p>tools page</p>,
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([indexRoute, toolsRoute]),
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

describe("AgentToolsPanel", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("empty state when no tool servers registered", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/tools/servers")) {
        return Promise.resolve(jsonResponse([]));
      }
      if (url.endsWith(`/agents/templates/${TEMPLATE_ID}/tools`)) {
        return Promise.resolve(
          jsonResponse({ template_id: TEMPLATE_ID, assigned_tools: [] }),
        );
      }
      return Promise.resolve(jsonResponse({}, { status: 404 }));
    });

    render(
      <Providers>
        <AgentToolsPanel templateId={TEMPLATE_ID} />
      </Providers>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("agent-tools-panel-empty")).toBeInTheDocument();
    });
    expect(
      screen.getByRole("link", { name: /Ajouter votre premier serveur MCP/i }),
    ).toBeInTheDocument();
  });

  it("renders tools grouped by server with pre-checked assignments + saves diff", async () => {
    const serverDetail = {
      server_id: SERVER_ID,
      name: "smoke-mcp",
      transport: "stdio" as const,
      status: "active" as const,
      connection_config: { command: "python" },
      discovered_at: new Date().toISOString(),
      tools: [
        {
          tool_id: TOOL_ID_A,
          name: "echo",
          description: "Echo input",
          input_schema: {},
          output_schema: null,
        },
        {
          tool_id: TOOL_ID_B,
          name: "add",
          description: "Add two numbers",
          input_schema: {},
          output_schema: null,
        },
      ],
    };
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.endsWith("/tools/servers") && method === "GET") {
        return Promise.resolve(
          jsonResponse([
            {
              server_id: SERVER_ID,
              name: "smoke-mcp",
              transport: "stdio",
              status: "active",
              tools_count: 2,
              discovered_at: serverDetail.discovered_at,
            },
          ]),
        );
      }
      if (url.endsWith(`/tools/servers/${SERVER_ID}`)) {
        return Promise.resolve(jsonResponse(serverDetail));
      }
      if (
        url.endsWith(`/agents/templates/${TEMPLATE_ID}/tools`) &&
        method === "GET"
      ) {
        // Pre-assign TOOL_ID_A to the template.
        return Promise.resolve(
          jsonResponse({
            template_id: TEMPLATE_ID,
            assigned_tools: [
              {
                tool_id: TOOL_ID_A,
                name: "echo",
                description: "",
                server_id: SERVER_ID,
              },
            ],
          }),
        );
      }
      if (
        url.endsWith(`/agents/templates/${TEMPLATE_ID}/tools`) &&
        method === "POST"
      ) {
        const body = JSON.parse(init!.body as string);
        return Promise.resolve(
          jsonResponse({
            template_id: TEMPLATE_ID,
            assigned_tools: body.tool_ids.map((tid: string) => ({
              tool_id: tid,
              name: tid === TOOL_ID_A ? "echo" : "add",
              description: "",
              server_id: SERVER_ID,
            })),
          }),
        );
      }
      return Promise.resolve(jsonResponse({}, { status: 404 }));
    });

    render(
      <Providers>
        <AgentToolsPanel templateId={TEMPLATE_ID} />
      </Providers>,
    );

    // Wait for the tools group to render.
    await waitFor(() => {
      expect(screen.getByTestId(`tool-group-${SERVER_ID}`)).toBeInTheDocument();
    });

    // TOOL_ID_A pre-checked, TOOL_ID_B unchecked.
    const checkboxA = screen.getByTestId(`tool-checkbox-${TOOL_ID_A}`);
    const checkboxB = screen.getByTestId(`tool-checkbox-${TOOL_ID_B}`);
    expect(checkboxA).toHaveAttribute("data-state", "checked");
    expect(checkboxB).toHaveAttribute("data-state", "unchecked");

    // Save button initially disabled (no changes).
    const saveBtn = screen.getByTestId("agent-tools-save");
    expect(saveBtn).toBeDisabled();

    // Toggle TOOL_ID_B on → save becomes enabled.
    fireEvent.click(checkboxB);
    await waitFor(() => {
      expect(saveBtn).not.toBeDisabled();
    });

    // Click save → POST fires with both tool_ids.
    fireEvent.click(saveBtn);
    await waitFor(() => {
      const postCall = fetchMock.mock.calls.find(([, init]) => {
        const i = init as RequestInit | undefined;
        return (i?.method ?? "GET").toUpperCase() === "POST";
      });
      expect(postCall).toBeDefined();
      const body = JSON.parse((postCall![1] as RequestInit).body as string);
      expect(new Set(body.tool_ids)).toEqual(new Set([TOOL_ID_A, TOOL_ID_B]));
    });
  });

  it("P-07: renders healthy servers even when one server detail fetch fails", async () => {
    const SERVER_OK = "11111111-aaaa-bbbb-cccc-dddddddddddd";
    const SERVER_KO = "22222222-aaaa-bbbb-cccc-dddddddddddd";
    const TOOL_OK = "33333333-aaaa-bbbb-cccc-dddddddddddd";

    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/tools/servers")) {
        return Promise.resolve(
          jsonResponse([
            {
              server_id: SERVER_OK,
              name: "healthy-server",
              transport: "stdio",
              status: "active",
              tools_count: 1,
              discovered_at: new Date().toISOString(),
            },
            {
              server_id: SERVER_KO,
              name: "broken-server",
              transport: "stdio",
              status: "active",
              tools_count: 1,
              discovered_at: new Date().toISOString(),
            },
          ]),
        );
      }
      if (url.endsWith(`/tools/servers/${SERVER_OK}`)) {
        return Promise.resolve(
          jsonResponse({
            server_id: SERVER_OK,
            name: "healthy-server",
            transport: "stdio",
            status: "active",
            connection_config: { command: "python" },
            discovered_at: new Date().toISOString(),
            tools: [
              {
                tool_id: TOOL_OK,
                name: "ping",
                description: "Ping a host",
                input_schema: {},
                output_schema: null,
              },
            ],
          }),
        );
      }
      if (url.endsWith(`/tools/servers/${SERVER_KO}`)) {
        return Promise.resolve(jsonResponse({ detail: "boom" }, { status: 500 }));
      }
      if (url.endsWith(`/agents/templates/${TEMPLATE_ID}/tools`)) {
        return Promise.resolve(
          jsonResponse({ template_id: TEMPLATE_ID, assigned_tools: [] }),
        );
      }
      return Promise.resolve(jsonResponse({}, { status: 404 }));
    });

    render(
      <Providers>
        <AgentToolsPanel templateId={TEMPLATE_ID} />
      </Providers>,
    );

    // The healthy server's tool group must render despite the broken sibling.
    await waitFor(() => {
      expect(screen.getByTestId(`tool-group-${SERVER_OK}`)).toBeInTheDocument();
    });
    expect(screen.getByTestId(`tool-checkbox-${TOOL_OK}`)).toBeInTheDocument();

    // The broken server gets a degraded banner instead of hiding the panel.
    expect(
      screen.getByTestId(`tool-group-error-${SERVER_KO}`),
    ).toBeInTheDocument();
  });
});
