/**
 * sidebarStore — UX-DR19 collapse persistence.
 *
 * Zustand store with `persist` middleware (localStorage key
 * `agentive.sidebar`). Default `collapsed: false` so first-time users see the
 * full 240px sidebar with labels. The state survives page reloads.
 *
 * `hasHydrated` exposes whether the persisted state has been merged into the
 * store. Consumers can use it to suppress visual transitions on the very
 * first paint to avoid a 240→56px flash when the user had `collapsed: true`
 * persisted (zustand v5 rehydrates through a microtask, so the initial render
 * sees `collapsed: false` before the persisted value lands).
 *
 * `partialize` keeps localStorage limited to the user-visible field
 * (`collapsed`); transient flags like `hasHydrated` are reset on every load.
 */

import { create } from "zustand";
import { persist } from "zustand/middleware";

type SidebarState = {
  collapsed: boolean;
  hasHydrated: boolean;
  toggle: () => void;
  setCollapsed: (value: boolean) => void;
  setHasHydrated: (value: boolean) => void;
};

export const useSidebarStore = create<SidebarState>()(
  persist(
    (set) => ({
      collapsed: false,
      hasHydrated: false,
      toggle: () => set((state) => ({ collapsed: !state.collapsed })),
      setCollapsed: (value) => set({ collapsed: value }),
      setHasHydrated: (value) => set({ hasHydrated: value }),
    }),
    {
      name: "agentive.sidebar",
      partialize: (state) => ({ collapsed: state.collapsed }),
      onRehydrateStorage: () => (state) => {
        state?.setHasHydrated(true);
      },
    },
  ),
);
