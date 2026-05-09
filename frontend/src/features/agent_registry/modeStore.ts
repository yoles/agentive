/**
 * `useModeStore` — agent config Wizard/Expert preference (Story 2.3 T3.1).
 *
 * Persisted Zustand store mirroring the Story 1.8 `sidebarStore.ts` pattern :
 * `persist` middleware with a localStorage key, `partialize` to limit the
 * persisted shape to the user-visible field, and a `hasHydrated` flag that
 * consumers can read to suppress a visual flicker on first paint (zustand v5
 * rehydrates through a microtask, so the initial render sees the default
 * `wizard` value before the persisted `expert` lands).
 *
 * Default `mode = "wizard"` so first-time users get the guided UX.
 */

import { create } from "zustand";
import { persist } from "zustand/middleware";

export type ConfigMode = "wizard" | "expert";

type ModeState = {
  mode: ConfigMode;
  hasHydrated: boolean;
  setMode: (value: ConfigMode) => void;
  setHasHydrated: (value: boolean) => void;
};

export const useModeStore = create<ModeState>()(
  persist(
    (set) => ({
      mode: "wizard",
      hasHydrated: false,
      setMode: (value) => set({ mode: value }),
      setHasHydrated: (value) => set({ hasHydrated: value }),
    }),
    {
      name: "agentive.agent-config-mode",
      partialize: (state) => ({ mode: state.mode }),
      onRehydrateStorage: () => (state) => {
        state?.setHasHydrated(true);
      },
    },
  ),
);
