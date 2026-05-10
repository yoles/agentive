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

const VALID_MODES = new Set<ConfigMode>(["wizard", "expert"]);

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
      // P-12 (CR 2026-05-10) — error branch + P-27 runtime guard sur la
      // valeur hydratée. Si localStorage est corrompu (manipulation manuelle
      // via DevTools, conflit cross-tab, etc.), on log et on retombe sur le
      // default `wizard` plutôt que de propager une valeur invalide qui
      // casserait le routing Wizard/Expert silencieusement.
      onRehydrateStorage: () => (state, error) => {
        if (error) {
          console.warn(
            "[useModeStore] hydration error — falling back to default 'wizard'",
            error,
          );
        }
        if (state && !VALID_MODES.has(state.mode)) {
          console.warn(
            `[useModeStore] invalid persisted mode '${state.mode}' — reset to 'wizard'`,
          );
          state.mode = "wizard";
        }
        state?.setHasHydrated(true);
      },
    },
  ),
);
