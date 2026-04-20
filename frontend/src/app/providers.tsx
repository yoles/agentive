import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { ThemeProvider } from "@/features/theme";
import { queryClient } from "@/shared/api/queryClient";

/**
 * Composition des providers globaux : Theme + Query.
 * Le RouterProvider est mis en place dans `main.tsx`.
 */
export function Providers({ children }: { children: ReactNode }) {
  return (
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </ThemeProvider>
  );
}
