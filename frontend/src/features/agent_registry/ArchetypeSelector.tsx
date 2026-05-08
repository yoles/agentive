/**
 * `ArchetypeSelector` — UX-DR17 grid 4x2 of the 8 universal archetypes.
 *
 * Story 2.1 AC4 :
 * - Responsive grid : `grid-cols-2 md:grid-cols-4` (8 cards = 4×2 on desktop,
 *   2×4 on mobile).
 * - Selected card : `ring-2 ring-primary` border (UX-DR37 focus consistent).
 * - Keyboard accessible : Tab to navigate, Enter / Space to select.
 *
 * The variant prop is restricted to ``single`` for Story 2.1 (multi will land
 * Story 2.3 — Mode Wizard / Expert).
 */

import { Card } from "@/shared/components/ui/card";
import { cn } from "@/shared/lib/utils";
import { getArchetypeIcon } from "./icons";
import type { ArchetypeSummary } from "./types";

export type ArchetypeSelectorProps = {
  archetypes: ArchetypeSummary[];
  value: string | null;
  onChange: (archetypeId: string) => void;
  /** Disable interaction (e.g. while a parent form is submitting). */
  disabled?: boolean;
};

export function ArchetypeSelector({
  archetypes,
  value,
  onChange,
  disabled = false,
}: ArchetypeSelectorProps) {
  return (
    <div
      role="radiogroup"
      aria-label="Choisir un archétype"
      className="grid grid-cols-2 gap-4 md:grid-cols-4"
    >
      {archetypes.map((archetype) => {
        const Icon = getArchetypeIcon(archetype.icon_name);
        const selected = value === archetype.id;
        return (
          <Card
            key={archetype.id}
            role="radio"
            aria-checked={selected}
            aria-disabled={disabled}
            tabIndex={disabled ? -1 : 0}
            data-testid={`archetype-card-${archetype.id}`}
            onClick={() => {
              if (!disabled) onChange(archetype.id);
            }}
            onKeyDown={(event) => {
              if (disabled) return;
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onChange(archetype.id);
              }
            }}
            className={cn(
              "flex cursor-pointer flex-col gap-2 p-4 transition-shadow",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
              selected && "ring-2 ring-primary",
              disabled && "cursor-not-allowed opacity-50",
            )}
          >
            <Icon
              aria-hidden="true"
              className={cn("size-10", selected ? "text-primary" : "text-muted-foreground")}
            />
            <div className="flex flex-col gap-1">
              <span className="font-semibold leading-tight">{archetype.display_name}</span>
              <span className="line-clamp-2 text-xs text-muted-foreground">
                {archetype.description}
              </span>
            </div>
          </Card>
        );
      })}
    </div>
  );
}
