/**
 * Tests — `useModeStore` (Story 2.3 T3.4).
 *
 * P-11 + P-15 (CR 2026-05-10) — cleanup via `useModeStore.persist.clearStorage()`
 * (chemin officiel Zustand qui invalide aussi le cache rehydrate-once interne)
 * + ajout d'un test round-trip rehydration pour garder la persistance
 * fonctionnelle face aux refactors futurs.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useModeStore } from "./modeStore";

describe("useModeStore", () => {
  const STORAGE_KEY = "agentive.agent-config-mode";

  beforeEach(() => {
    // Reset both the store state AND the persisted value before each test.
    useModeStore.setState({ mode: "wizard", hasHydrated: false });
    useModeStore.persist.clearStorage();
  });

  afterEach(() => {
    useModeStore.persist.clearStorage();
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

  it("P-15 — round-trip : write 'expert' → rehydrate → state reflects 'expert'", async () => {
    // Write the persisted shape directly so we exercise the rehydration
    // path (and not just the in-memory setMode call).
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ state: { mode: "expert" }, version: 0 }),
    );
    // Re-trigger Zustand's hydration path. `rehydrate()` reads from storage
    // and runs the `onRehydrateStorage` callback (which sets hasHydrated).
    await useModeStore.persist.rehydrate();
    expect(useModeStore.getState().mode).toBe("expert");
    expect(useModeStore.getState().hasHydrated).toBe(true);
  });

  it("P-27 — corrupted persisted mode falls back to default 'wizard'", async () => {
    // Manually plant an invalid mode value (DevTools manipulation, cross-tab
    // race, schema drift, …). The `onRehydrateStorage` guard should reset.
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ state: { mode: "hacker" }, version: 0 }),
    );
    await useModeStore.persist.rehydrate();
    expect(useModeStore.getState().mode).toBe("wizard");
  });
});
