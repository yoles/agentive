/**
 * `CreateNamespaceDialog` tests — Story 3.2 code review, P8.
 *
 * "Annuler" used to call the `onOpenChange` prop directly, bypassing the
 * `Dialog` wrapper's `reset()` — so dismissing via Escape/overlay cleared
 * the draft but "Annuler" left it behind. Both paths must reset the form.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { CreateNamespaceDialog } from "./CreateNamespaceDialog";

function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

describe("CreateNamespaceDialog", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    cleanup();
  });

  it("clears the draft when dismissed via 'Annuler'", () => {
    const Wrapper = makeWrapper();
    render(<CreateNamespaceDialog open={true} onOpenChange={() => {}} />, {
      wrapper: Wrapper,
    });

    fireEvent.change(screen.getByTestId("create-namespace-name"), {
      target: { value: "dev-notes" },
    });
    fireEvent.change(screen.getByTestId("create-namespace-department"), {
      target: { value: "Dev" },
    });

    fireEvent.click(screen.getByRole("button", { name: "Annuler" }));

    expect(screen.getByTestId("create-namespace-name")).toHaveValue("");
    expect(screen.getByTestId("create-namespace-department")).toHaveValue("");
  });

  it("clears the draft the same way when dismissed via Escape", () => {
    const Wrapper = makeWrapper();
    render(<CreateNamespaceDialog open={true} onOpenChange={() => {}} />, {
      wrapper: Wrapper,
    });

    fireEvent.change(screen.getByTestId("create-namespace-name"), {
      target: { value: "dev-notes" },
    });

    fireEvent.keyDown(screen.getByTestId("create-namespace-dialog"), {
      key: "Escape",
      code: "Escape",
    });

    expect(screen.getByTestId("create-namespace-name")).toHaveValue("");
  });
});
