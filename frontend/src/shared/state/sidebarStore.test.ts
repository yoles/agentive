/**
 * sidebarStore tests — initial state, toggle, setCollapsed, persistence,
 * and partialize/hydration metadata.
 *
 * Zustand `persist` middleware writes to localStorage. We reset both the
 * in-memory state and localStorage between tests to avoid bleed.
 */
import { beforeEach, describe, expect, it } from "vitest";
import { useSidebarStore } from "./sidebarStore";

const STORAGE_KEY = "agentive.sidebar";

describe("sidebarStore", () => {
  beforeEach(() => {
    localStorage.clear();
    useSidebarStore.setState({ collapsed: false, hasHydrated: false });
  });

  it("starts collapsed = false", () => {
    expect(useSidebarStore.getState().collapsed).toBe(false);
  });

  it("toggles collapsed state", () => {
    useSidebarStore.getState().toggle();
    expect(useSidebarStore.getState().collapsed).toBe(true);
    useSidebarStore.getState().toggle();
    expect(useSidebarStore.getState().collapsed).toBe(false);
  });

  it("setCollapsed forces a specific value", () => {
    useSidebarStore.getState().setCollapsed(true);
    expect(useSidebarStore.getState().collapsed).toBe(true);
    useSidebarStore.getState().setCollapsed(true);
    expect(useSidebarStore.getState().collapsed).toBe(true);
    useSidebarStore.getState().setCollapsed(false);
    expect(useSidebarStore.getState().collapsed).toBe(false);
  });

  it("persists `collapsed` to localStorage and excludes transient fields", () => {
    useSidebarStore.getState().setCollapsed(true);

    const raw = localStorage.getItem(STORAGE_KEY);
    expect(raw).not.toBeNull();
    const parsed = JSON.parse(raw as string) as {
      state: Record<string, unknown>;
    };
    expect(parsed.state.collapsed).toBe(true);
    // `hasHydrated` is a transient flag — `partialize` must keep it out of
    // localStorage so a corrupted reload doesn't accidentally claim hydrated.
    expect(parsed.state).not.toHaveProperty("hasHydrated");
    // Functions must not be persisted either.
    expect(parsed.state).not.toHaveProperty("toggle");
    expect(parsed.state).not.toHaveProperty("setCollapsed");
  });
});
