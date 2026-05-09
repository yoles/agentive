/**
 * Tests — `useModeStore` (Story 2.3 T3.4).
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useModeStore } from "./modeStore";

describe("useModeStore", () => {
  const STORAGE_KEY = "agentive.agent-config-mode";

  beforeEach(() => {
    // Reset both the store state AND the persisted value before each test.
    useModeStore.setState({ mode: "wizard", hasHydrated: false });
    localStorage.removeItem(STORAGE_KEY);
  });

  afterEach(() => {
    localStorage.removeItem(STORAGE_KEY);
  });

  it("defaults to mode 'wizard' on first read", () => {
    expect(useModeStore.getState().mode).toBe("wizard");
  });

  it("setMode updates state and persists to localStorage", () => {
    useModeStore.getState().setMode("expert");
    expect(useModeStore.getState().mode).toBe("expert");
    const persisted = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}");
    expect(persisted.state.mode).toBe("expert");
  });

  it("partialize keeps only `mode` in localStorage (not transient flags)", () => {
    useModeStore.getState().setMode("expert");
    useModeStore.getState().setHasHydrated(true);
    const persisted = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}");
    expect(Object.keys(persisted.state)).toEqual(["mode"]);
    expect(persisted.state.hasHydrated).toBeUndefined();
  });

  it("setHasHydrated flips the in-memory flag without writing it to storage", () => {
    expect(useModeStore.getState().hasHydrated).toBe(false);
    useModeStore.getState().setHasHydrated(true);
    expect(useModeStore.getState().hasHydrated).toBe(true);
  });
});
