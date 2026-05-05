import type { ReactNode } from "react";
import { Outlet } from "@tanstack/react-router";
import { useGlobalShortcuts } from "@/shared/hooks/useGlobalShortcuts";
import { Sidebar } from "./Sidebar";

/**
 * AppLayout — sidebar fixe 240px + main content fluide (max-width 1440px).
 *
 * Reste une primitive shared agnostique des features : le caller (route
 * `__root.tsx`, type `app`) injecte les éléments feature-aware via les slots.
 *
 * `useGlobalShortcuts` mounts the Cmd/Ctrl+1..4 listener once for the whole
 * app — this is the single source of truth for global keyboard navigation.
 */
export function AppLayout({
  sidebarFooterSlot,
  sidebarFooterSlotTooltip,
}: {
  sidebarFooterSlot?: ReactNode;
  sidebarFooterSlotTooltip?: ReactNode;
} = {}) {
  useGlobalShortcuts();

  return (
    <div className="flex h-screen bg-background text-foreground">
      <Sidebar
        footerSlot={sidebarFooterSlot}
        footerSlotTooltip={sidebarFooterSlotTooltip}
      />
      <main className="flex-1 overflow-auto">
        <div className="mx-auto h-full max-w-[1440px] px-6 py-8">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
