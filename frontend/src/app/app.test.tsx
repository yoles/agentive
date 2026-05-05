/**
 * Smoke tests — RouterProvider + AppLayout mount, Sidebar exposes the 4 MVP
 * spaces, the collapse toggle hides labels and shrinks width, and the
 * ModeToggle is present. Also covers the AC7 requirement that at least one
 * shadcn primitive imports & renders cleanly.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import {
  createMemoryHistory,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { Providers } from "@/app/providers";
import { routeTree } from "@/app/routeTree.gen";
import { Card } from "@/shared/components/ui/card";
import { useSidebarStore } from "@/shared/state/sidebarStore";

function renderApp() {
  const router = createRouter({
    routeTree,
    history: createMemoryHistory({ initialEntries: ["/dashboard"] }),
  });
  return render(
    <Providers>
      <RouterProvider router={router} />
    </Providers>,
  );
}

describe("App smoke", () => {
  beforeEach(() => {
    // Force theme=dark for deterministic ModeToggle assertions regardless of
    // the CI runner's `prefers-color-scheme`. `Providers` uses `enableSystem`
    // so without this, jsdom's matchMedia default could leak through.
    localStorage.setItem("theme", "dark");
  });

  afterEach(() => {
    cleanup();
    useSidebarStore.setState({ collapsed: false, hasHydrated: false });
    localStorage.clear();
    document.documentElement.className = "";
  });

  it("renders the sidebar with 4 navigation items + ModeToggle", async () => {
    renderApp();

    // `findByText` waits for the async router render to settle.
    expect(await screen.findByText("Agentive")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Dashboard/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Chat/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Trace/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Config/i })).toBeInTheDocument();
    // ModeToggle starts in dark mode → label invites switching to light.
    expect(
      screen.getByRole("button", { name: /Activer le thème clair/i }),
    ).toBeInTheDocument();
  });

  it("collapses the sidebar — labels disappear, width shrinks, store flips", async () => {
    renderApp();
    await screen.findByText("Agentive");

    const aside = screen.getByRole("complementary", {
      name: /Navigation principale/i,
    });
    expect(aside).toHaveClass("w-60");
    expect(aside).not.toHaveClass("w-14");

    const collapseButton = screen.getByRole("button", {
      name: /Réduire la barre latérale/i,
    });
    fireEvent.click(collapseButton);

    // After collapse: brand label and footer text are gone.
    expect(screen.queryByText("Agentive")).not.toBeInTheDocument();
    expect(screen.queryByText("Sprint 0 · v0.1.0")).not.toBeInTheDocument();
    expect(useSidebarStore.getState().collapsed).toBe(true);
    // The aside reflects the AC6 width swap.
    expect(aside).toHaveClass("w-14");
    expect(aside).not.toHaveClass("w-60");

    // Toggle is now the "expand" affordance.
    expect(
      screen.getByRole("button", { name: /Étendre la barre latérale/i }),
    ).toBeInTheDocument();
  });

  it("imports and renders a shadcn primitive (Card) without crashing", () => {
    // AC7 smoke check — guards against bundler/path/alias regressions on the
    // generated primitives.
    render(<Card data-testid="smoke-card">test</Card>);
    expect(screen.getByTestId("smoke-card")).toBeInTheDocument();
  });
});
