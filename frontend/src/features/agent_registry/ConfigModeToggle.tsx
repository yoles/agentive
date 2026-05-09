/**
 * `ConfigModeToggle` — UX-DR16 (Story 2.3 T3.2).
 *
 * Switch entre les deux modes mutuellement exclusifs `wizard` ↔ `expert`
 * pour la page de configuration d'agent. Persistance via `useModeStore`
 * (Zustand + localStorage). Pattern miroir du `ModeToggle` theme (Story 1.8) :
 * `hasHydrated` flag pour suppress flicker au premier render avant que
 * la valeur persistée ne soit injectée.
 *
 * Accessibility :
 *  - `role="radiogroup"` sur le container, `role="radio"` + `aria-checked`
 *    sur chaque option (UX-DR16 ligne 957).
 *  - Tab puis Espace/Entrée pour switcher.
 *  - Focus visible (UX-DR37 ring violet 2px) hérité de la primitive Button.
 */

import { LayoutGrid, Wand2 } from "lucide-react";
import { Button } from "@/shared/components/ui/button";
import { cn } from "@/shared/lib/utils";
import { useModeStore, type ConfigMode } from "./modeStore";

const OPTIONS: ReadonlyArray<{
  mode: ConfigMode;
  label: string;
  Icon: typeof Wand2;
}> = [
  { mode: "wizard", label: "Wizard", Icon: Wand2 },
  { mode: "expert", label: "Expert", Icon: LayoutGrid },
];

export function ConfigModeToggle() {
  const mode = useModeStore((s) => s.mode);
  const setMode = useModeStore((s) => s.setMode);
  const hasHydrated = useModeStore((s) => s.hasHydrated);

  return (
    <div
      role="radiogroup"
      aria-label="Mode de configuration"
      className="inline-flex items-center gap-1 rounded-md bg-muted p-1"
      data-testid="config-mode-toggle"
    >
      {OPTIONS.map(({ mode: opt, label, Icon }) => {
        const isActive = hasHydrated && mode === opt;
        return (
          <Button
            key={opt}
            type="button"
            role="radio"
            variant={isActive ? "default" : "ghost"}
            size="sm"
            aria-checked={isActive}
            aria-label={`Mode ${label}`}
            onClick={() => setMode(opt)}
            className={cn(
              "h-8 gap-2 px-3",
              isActive ? "shadow-xs" : "text-muted-foreground hover:text-foreground",
            )}
            data-mode={opt}
          >
            <Icon className="size-4" aria-hidden="true" />
            <span>{label}</span>
          </Button>
        );
      })}
    </div>
  );
}
