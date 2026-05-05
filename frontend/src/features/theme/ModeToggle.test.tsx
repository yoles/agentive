/**
 * ModeToggle tests — verify theme switch + dynamic aria-label, including
 * the prod-aligned `enableSystem={true}` configuration and the missing-
 * provider degradation.
 *
 * next-themes reads/writes `<html class="dark|light">` and persists in
 * localStorage. We reset both between tests for determinism.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ThemeProvider } from "next-themes";
import { ModeToggle } from "./ModeToggle";

function renderWithTheme(options?: {
  defaultTheme?: "dark" | "light";
  enableSystem?: boolean;
}) {
  const { defaultTheme = "dark", enableSystem = false } = options ?? {};
  return render(
    <ThemeProvider
      attribute="class"
      defaultTheme={defaultTheme}
      enableSystem={enableSystem}
      disableTransitionOnChange
    >
      <ModeToggle />
    </ThemeProvider>,
  );
}

describe("ModeToggle", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.className = "";
  });

  afterEach(() => {
    cleanup();
    document.documentElement.className = "";
    localStorage.clear();
  });

  it("toggles from dark to light and updates the html class", () => {
    renderWithTheme({ defaultTheme: "dark" });

    // next-themes sets `<html class="dark">` on mount.
    expect(document.documentElement.classList.contains("dark")).toBe(true);

    const button = screen.getByRole("button", {
      name: /Activer le thème clair/i,
    });
    fireEvent.click(button);

    expect(document.documentElement.classList.contains("light")).toBe(true);
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("exposes a dynamic aria-label that describes the next action", () => {
    renderWithTheme({ defaultTheme: "dark" });
    const button = screen.getByRole("button");

    // Starting in dark mode → label invites switching to light.
    expect(button).toHaveAccessibleName("Activer le thème clair");

    fireEvent.click(button);

    // Now in light mode → label invites switching to dark.
    expect(button).toHaveAccessibleName("Activer le thème sombre");
  });

  it("works with prod-aligned ThemeProvider (enableSystem=true)", () => {
    // Mirrors `Providers` (`enableSystem` + `defaultTheme="dark"`). With no
    // explicit theme in localStorage, next-themes falls back to defaultTheme.
    renderWithTheme({ defaultTheme: "dark", enableSystem: true });

    expect(document.documentElement.classList.contains("dark")).toBe(true);
    const button = screen.getByRole("button", {
      name: /Activer le thème clair/i,
    });
    fireEvent.click(button);
    expect(document.documentElement.classList.contains("light")).toBe(true);
  });

  it("renders an inert placeholder when mounted outside a ThemeProvider", () => {
    // Without a Provider, useTheme returns no setter → toggling is a no-op.
    // The mounted-flag pattern still produces a stable, accessible button.
    render(<ModeToggle />);
    const button = screen.getByRole("button");
    expect(button).toBeInTheDocument();
    // The accessible name is non-empty (mounted placeholder OR resolved
    // label — both are valid; we only check that the button is reachable).
    expect(button.getAttribute("aria-label")).toBeTruthy();
  });
});
