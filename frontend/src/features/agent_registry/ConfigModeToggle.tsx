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
import { useRef, type KeyboardEvent } from "react";
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

export type ConfigModeToggleProps = {
  /** P-10 (CR 2026-05-10) — empêche le switch de mode pendant qu'une mutation
   * est en flight (sinon race : le toast/focus fire après le switch et cible
   * un input qui n'est plus dans le DOM). */
  disabled?: boolean;
};

export function ConfigModeToggle({ disabled = false }: ConfigModeToggleProps = {}) {
  const mode = useModeStore((s) => s.mode);
  const setMode = useModeStore((s) => s.setMode);
  const hasHydrated = useModeStore((s) => s.hasHydrated);
  const buttonRefs = useRef<Array<HTMLButtonElement | null>>([]);

  // P-24 (CR 2026-05-10) — skeleton avant hydratation pour éviter un
  // radiogroup invalide WAI-ARIA (au moins un radio doit être checked).
  // Sans ce guard, premier render = 2 radios `aria-checked=false` qu'un
  // lecteur d'écran annonce "no item selected".
  if (!hasHydrated) {
    return (
      <div
        aria-label="Mode de configuration (chargement)"
        aria-busy="true"
        className="inline-flex items-center gap-1 rounded-md bg-muted p-1"
        data-testid="config-mode-toggle-skeleton"
      >
        <div className="h-8 w-20 animate-pulse rounded bg-muted-foreground/10" />
        <div className="h-8 w-20 animate-pulse rounded bg-muted-foreground/10" />
      </div>
    );
  }

  // P-25 (CR 2026-05-10) — WAI-ARIA radiogroup keyboard pattern : roving
  // tabindex (un seul stop dans le Tab order) + ←/→/Home/End pour naviguer
  // entre les radios. Cohérent avec aria-keyshortcuts annoncé sur le group.
  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const currentIndex = OPTIONS.findIndex((o) => o.mode === mode);
    if (currentIndex < 0) return;
    let nextIndex: number | null = null;
    switch (event.key) {
      case "ArrowRight":
      case "ArrowDown":
        nextIndex = (currentIndex + 1) % OPTIONS.length;
        break;
      case "ArrowLeft":
      case "ArrowUp":
        nextIndex = (currentIndex - 1 + OPTIONS.length) % OPTIONS.length;
        break;
      case "Home":
        nextIndex = 0;
        break;
      case "End":
        nextIndex = OPTIONS.length - 1;
        break;
    }
    if (nextIndex !== null) {
      event.preventDefault();
      const next = OPTIONS[nextIndex];
      setMode(next.mode);
      buttonRefs.current[nextIndex]?.focus();
    }
  }

  return (
    <div
      role="radiogroup"
      tabIndex={-1}
      aria-label="Mode de configuration"
      aria-keyshortcuts="ArrowLeft ArrowRight Home End"
      onKeyDown={handleKeyDown}
      className="inline-flex items-center gap-1 rounded-md bg-muted p-1"
      data-testid="config-mode-toggle"
    >
      {OPTIONS.map(({ mode: opt, label, Icon }, index) => {
        const isActive = mode === opt;
        return (
          <Button
            key={opt}
            ref={(el) => {
              buttonRefs.current[index] = el;
            }}
            type="button"
            role="radio"
            variant={isActive ? "default" : "ghost"}
            size="sm"
            aria-checked={isActive}
            aria-label={`Mode ${label}`}
            // P-25 — roving tabindex : seul l'item actif est tabbable.
            tabIndex={isActive ? 0 : -1}
            disabled={disabled}
            onClick={() => {
              if (!disabled && mode !== opt) setMode(opt);
            }}
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
