/**
 * ModeToggle — UX-DR16. Switches between dark and light themes.
 *
 * `next-themes` already wires `attribute="class"` on `<html>` (see
 * `ThemeProvider.tsx`). We use the `mounted` flag pattern recommended by
 * next-themes to avoid an icon/label flicker on first paint when
 * `theme`/`resolvedTheme` are still undefined.
 *
 * Behaviour:
 *  - Before mount → inert placeholder (Moon icon, generic label).
 *  - After mount → icon and label reflect the resolved theme; clicking
 *    swaps between dark and light. Note: clicking explicitly opts the
 *    user out of `system` inheritance — that is intentional, the toggle
 *    is for users who want a specific mode.
 */

import { Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import { useEffect, useState } from "react";
import { Button } from "@/shared/components/ui/button";

export function ModeToggle() {
  const { resolvedTheme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);

  // Canonical next-themes pattern: flip `mounted` after first commit so that
  // the initial render (where `resolvedTheme` is undefined) shows an inert
  // placeholder instead of guessing dark/light. There is no external system
  // to derive `mounted` from — the signal is "first commit happened".
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- documented next-themes pattern (see https://github.com/pacocoursey/next-themes#avoid-hydration-mismatch)
    setMounted(true);
  }, []);

  if (!mounted) {
    return (
      <Button
        variant="ghost"
        size="icon"
        aria-label="Basculer le thème"
        disabled
      >
        <Moon className="size-4" aria-hidden="true" />
      </Button>
    );
  }

  const isDark = resolvedTheme === "dark";
  const label = isDark ? "Activer le thème clair" : "Activer le thème sombre";

  return (
    <Button
      variant="ghost"
      size="icon"
      aria-label={label}
      onClick={() => setTheme(isDark ? "light" : "dark")}
    >
      {isDark ? (
        <Sun className="size-4" aria-hidden="true" />
      ) : (
        <Moon className="size-4" aria-hidden="true" />
      )}
    </Button>
  );
}
