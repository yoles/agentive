/**
 * Tests — `ConfigModeToggle` (Story 2.3 T3.4).
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ConfigModeToggle } from "./ConfigModeToggle";
import { useModeStore } from "./modeStore";

describe("ConfigModeToggle", () => {
  beforeEach(() => {
    // Pretend hydration completed so the active state is reflected in the
    // visual `aria-checked`. Without this, both options would render in
    // their inactive variant (suppress flicker pattern).
    useModeStore.setState({ mode: "wizard", hasHydrated: true });
  });

  afterEach(() => {
    cleanup();
    // P-11 (CR 2026-05-10) — chemin officiel Zustand, plus robust qu'un
    // removeItem direct.
    useModeStore.persist.clearStorage();
  });

  it("renders both options with role=radio + role=radiogroup", () => {
    render(<ConfigModeToggle />);
    const group = screen.getByRole("radiogroup", { name: /mode de configuration/i });
    expect(group).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /mode wizard/i })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /mode expert/i })).toBeInTheDocument();
  });

  it("clicking expert calls setMode and flips aria-checked", () => {
    render(<ConfigModeToggle />);
    const expertOption = screen.getByRole("radio", { name: /mode expert/i });
    expect(expertOption).toHaveAttribute("aria-checked", "false");
    fireEvent.click(expertOption);
    expect(useModeStore.getState().mode).toBe("expert");
    expect(
      screen.getByRole("radio", { name: /mode expert/i }),
    ).toHaveAttribute("aria-checked", "true");
  });

  it("renders a skeleton (no radios) until hydration completes (P-24)", () => {
    // P-24 (CR 2026-05-10) — pre-hydration : the component renders a busy
    // skeleton ; aucun `role="radio"` n'est présent (sinon le radiogroup
    // serait invalide WAI-ARIA — au moins un radio doit être checked).
    useModeStore.setState({ mode: "wizard", hasHydrated: false });
    render(<ConfigModeToggle />);
    expect(screen.getByTestId("config-mode-toggle-skeleton")).toBeInTheDocument();
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
  });

  it("disabled prop blocks setMode (P-10 race-mid-save guard)", () => {
    useModeStore.setState({ mode: "wizard", hasHydrated: true });
    render(<ConfigModeToggle disabled />);
    fireEvent.click(screen.getByRole("radio", { name: /mode expert/i }));
    expect(useModeStore.getState().mode).toBe("wizard");
  });
});
