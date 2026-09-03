/**
 * Component tests — AgentTesterForm (Story 2.7 T8.2, P-04 fix-batch 2026-08-31).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { AgentTesterForm } from "./AgentTesterForm";

const TEMPLATE_ID = "11111111-2222-3333-4444-555555555555";

function jsonResponse(body: unknown, init: ResponseInit = { status: 200 }) {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
  });
}

function Providers({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

describe("AgentTesterForm", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders the arguments textarea, timeout input and run button", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ template_id: TEMPLATE_ID, assigned_tools: [] }),
    );
    render(
      <Providers>
        <AgentTesterForm templateId={TEMPLATE_ID} isRunning={false} onSubmit={vi.fn()} />
      </Providers>,
    );
    expect(await screen.findByTestId("playground-arguments-textarea")).toBeInTheDocument();
    expect(screen.getByTestId("playground-timeout-input")).toBeInTheDocument();
    expect(screen.getByTestId("playground-run-button")).toBeInTheDocument();
  });

  it("renders one checkbox per assigned tool, all checked by default", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        template_id: TEMPLATE_ID,
        assigned_tools: [
          { tool_id: "tool-a", name: "echo" },
          { tool_id: "tool-b", name: "add" },
        ],
      }),
    );
    render(
      <Providers>
        <AgentTesterForm templateId={TEMPLATE_ID} isRunning={false} onSubmit={vi.fn()} />
      </Providers>,
    );
    const checkboxA = await screen.findByTestId("playground-tool-tool-a");
    const checkboxB = await screen.findByTestId("playground-tool-tool-b");
    expect(checkboxA).toHaveAttribute("data-state", "checked");
    expect(checkboxB).toHaveAttribute("data-state", "checked");

    fireEvent.click(checkboxB);
    await waitFor(() => {
      expect(checkboxB).toHaveAttribute("data-state", "unchecked");
    });
    // Toggling one tool off must not affect the other.
    expect(checkboxA).toHaveAttribute("data-state", "checked");
  });

  it("submits parsed arguments, enabled_tool_ids and timeout_seconds", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        template_id: TEMPLATE_ID,
        assigned_tools: [{ tool_id: "tool-a", name: "echo" }],
      }),
    );
    const onSubmit = vi.fn();
    render(
      <Providers>
        <AgentTesterForm templateId={TEMPLATE_ID} isRunning={false} onSubmit={onSubmit} />
      </Providers>,
    );
    const textarea = await screen.findByTestId("playground-arguments-textarea");
    fireEvent.change(textarea, { target: { value: '{"topic": "tests"}' } });
    fireEvent.click(screen.getByTestId("playground-run-button"));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit).toHaveBeenCalledWith({
      arguments: { topic: "tests" },
      enabled_tool_ids: null,
      timeout_seconds: 30,
    });
  });

  it("does not call onSubmit while a run is already pending (P-38 double-submit guard)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ template_id: TEMPLATE_ID, assigned_tools: [] }),
    );
    const onSubmit = vi.fn();
    render(
      <Providers>
        <AgentTesterForm templateId={TEMPLATE_ID} isRunning={true} onSubmit={onSubmit} />
      </Providers>,
    );
    const button = await screen.findByTestId("playground-run-button");
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
