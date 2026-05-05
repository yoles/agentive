/**
 * useGlobalShortcuts integration tests — verify Cmd/Ctrl+1..4 navigation,
 * input-focus opt-out, modifier filtering, repeat/composition guards, and
 * unmatched shortcuts.
 *
 * Pattern: mount the hook inside a real Router with MemoryHistory so we can
 * assert on `router.history.location.pathname` after firing keyboard events.
 *
 * Note on `code` vs `key`: the hook keys off `event.code` (`Digit1`..`Digit4`)
 * to remain layout-independent (AZERTY users press Shift to type digits).
 * Tests therefore set `code: "Digit2"` instead of `key: "2"`.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { useGlobalShortcuts } from "./useGlobalShortcuts";

function buildTestRouter(initialPath = "/dashboard") {
  function Layout() {
    useGlobalShortcuts();
    return (
      <div data-testid="layout">
        <input data-testid="text-input" />
        <Outlet />
      </div>
    );
  }
  const root = createRootRoute({ component: Layout });
  const dashboard = createRoute({
    getParentRoute: () => root,
    path: "/dashboard",
    component: () => <div>dashboard</div>,
  });
  const chat = createRoute({
    getParentRoute: () => root,
    path: "/chat",
    component: () => <div>chat</div>,
  });
  const trace = createRoute({
    getParentRoute: () => root,
    path: "/trace",
    component: () => <div>trace</div>,
  });
  const config = createRoute({
    getParentRoute: () => root,
    path: "/config",
    component: () => <div>config</div>,
  });
  return createRouter({
    routeTree: root.addChildren([dashboard, chat, trace, config]),
    history: createMemoryHistory({ initialEntries: [initialPath] }),
  });
}

describe("useGlobalShortcuts", () => {
  afterEach(() => {
    cleanup();
  });

  it("navigates to /chat when Cmd+2 is pressed", async () => {
    const router = buildTestRouter("/dashboard");
    render(<RouterProvider router={router} />);
    await screen.findByTestId("layout");

    fireEvent.keyDown(window, { code: "Digit2", metaKey: true });

    await waitFor(() => {
      expect(router.history.location.pathname).toBe("/chat");
    });
  });

  it("navigates to /trace via Ctrl+3 (Windows/Linux path)", async () => {
    const router = buildTestRouter("/dashboard");
    render(<RouterProvider router={router} />);
    await screen.findByTestId("layout");

    fireEvent.keyDown(window, { code: "Digit3", ctrlKey: true });

    await waitFor(() => {
      expect(router.history.location.pathname).toBe("/trace");
    });
  });

  it("ignores Cmd+5 (no mapped shortcut)", async () => {
    const router = buildTestRouter("/dashboard");
    render(<RouterProvider router={router} />);
    await screen.findByTestId("layout");

    fireEvent.keyDown(window, { code: "Digit5", metaKey: true });

    expect(router.history.location.pathname).toBe("/dashboard");
  });

  it("does not navigate when an input is focused", async () => {
    const router = buildTestRouter("/dashboard");
    render(<RouterProvider router={router} />);
    const input = await screen.findByTestId("text-input");

    input.focus();
    expect(document.activeElement).toBe(input);

    fireEvent.keyDown(window, { code: "Digit2", metaKey: true });

    expect(router.history.location.pathname).toBe("/dashboard");
  });

  it("ignores Cmd+Shift+2 (reserved for OS/browser)", async () => {
    const router = buildTestRouter("/dashboard");
    render(<RouterProvider router={router} />);
    await screen.findByTestId("layout");

    fireEvent.keyDown(window, {
      code: "Digit2",
      metaKey: true,
      shiftKey: true,
    });

    expect(router.history.location.pathname).toBe("/dashboard");
  });

  it("ignores Cmd+Alt+2 (reserved — AltGr emits ctrl+alt on Win/Linux)", async () => {
    const router = buildTestRouter("/dashboard");
    render(<RouterProvider router={router} />);
    await screen.findByTestId("layout");

    fireEvent.keyDown(window, {
      code: "Digit2",
      ctrlKey: true,
      altKey: true,
    });

    expect(router.history.location.pathname).toBe("/dashboard");
  });

  it("ignores auto-repeat (key held down)", async () => {
    const router = buildTestRouter("/dashboard");
    render(<RouterProvider router={router} />);
    await screen.findByTestId("layout");

    fireEvent.keyDown(window, {
      code: "Digit2",
      metaKey: true,
      repeat: true,
    });

    expect(router.history.location.pathname).toBe("/dashboard");
  });

  it("ignores key events during IME composition", async () => {
    const router = buildTestRouter("/dashboard");
    render(<RouterProvider router={router} />);
    await screen.findByTestId("layout");

    fireEvent.keyDown(window, {
      code: "Digit2",
      metaKey: true,
      isComposing: true,
    });

    expect(router.history.location.pathname).toBe("/dashboard");
  });
});
