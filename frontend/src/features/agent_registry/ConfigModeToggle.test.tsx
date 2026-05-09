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
    localStorage.removeItem("agentive.agent-config-mode");
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

  it("hides active state until hydration completes (suppress flicker)", () => {
    useModeStore.setState({ mode: "wizard", hasHydrated: false });
    render(<ConfigModeToggle />);
    // Both options inactive when not hydrated.
    expect(
      screen.getByRole("radio", { name: /mode wizard/i }),
    ).toHaveAttribute("aria-checked", "false");
    expect(
      screen.getByRole("radio", { name: /mode expert/i }),
    ).toHaveAttribute("aria-checked", "false");
  });
});
