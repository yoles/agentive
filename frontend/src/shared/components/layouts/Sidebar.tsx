/**
 * Sidebar principale — UX-DR19 layout.
 *
 * Modes :
 *  - étendu (240px, défaut) — labels + raccourci `<kbd>` au hover ou focus-within
 *  - collapsé (56px) — icônes seulement, tooltip Radix au hover (UX-DR35)
 *
 * Persistance via `useSidebarStore` (Zustand persist). Raccourcis Cmd+1..4
 * câblés globalement par `useGlobalShortcuts` (mounted in `AppLayout`).
 *
 * `footerSlot` reçoit un ReactNode rendu dans le footer (ex: `ModeToggle`).
 * `footerSlotTooltip`, optionnel, est utilisé comme contenu de tooltip
 * Radix wrappant le slot quand la sidebar est collapsée (UX-DR35).
 *
 * Pattern de composition : Sidebar reste type `shared`, le caller `AppLayout`
 * (type `app`) peut importer depuis `features/*` librement.
 */

import type { ReactNode } from "react";
import { Link, useRouterState } from "@tanstack/react-router";
import {
  GitBranch,
  LayoutDashboard,
  MessageSquare,
  PanelLeftClose,
  PanelLeftOpen,
  Settings,
} from "lucide-react";
import { cn } from "@/shared/lib/utils";
import { useSidebarStore } from "@/shared/state/sidebarStore";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/shared/components/ui/tooltip";

type SpaceItem = {
  to: "/dashboard" | "/chat" | "/trace" | "/config";
  label: string;
  shortcut: string;
  Icon: typeof LayoutDashboard;
};

const spaces: SpaceItem[] = [
  { to: "/dashboard", label: "Dashboard", shortcut: "⌘1", Icon: LayoutDashboard },
  { to: "/chat", label: "Chat", shortcut: "⌘2", Icon: MessageSquare },
  { to: "/trace", label: "Trace", shortcut: "⌘3", Icon: GitBranch },
  { to: "/config", label: "Config", shortcut: "⌘4", Icon: Settings },
];

type SidebarProps = {
  footerSlot?: ReactNode;
  footerSlotTooltip?: ReactNode;
};

export function Sidebar({ footerSlot, footerSlotTooltip }: SidebarProps = {}) {
  const { location } = useRouterState();
  const collapsed = useSidebarStore((state) => state.collapsed);
  const toggle = useSidebarStore((state) => state.toggle);
  const hasHydrated = useSidebarStore((state) => state.hasHydrated);

  return (
    <TooltipProvider delayDuration={600}>
      <aside
        aria-label="Navigation principale"
        className={cn(
          "flex shrink-0 flex-col border-r border-border bg-card",
          // Suppress the width transition until the persisted state has
          // hydrated, otherwise users with `collapsed: true` see a 240→56px
          // snap on every reload.
          hasHydrated && "transition-[width] duration-200 motion-reduce:transition-none",
          collapsed ? "w-14" : "w-60",
        )}
      >
        <div
          className={cn(
            "flex items-center border-b border-border",
            collapsed ? "justify-center px-0 py-3" : "justify-between px-4 py-4",
          )}
        >
          {!collapsed && (
            <span className="text-lg font-semibold tracking-tight">
              Agentive
            </span>
          )}
          <button
            type="button"
            onClick={toggle}
            aria-label={
              collapsed ? "Étendre la barre latérale" : "Réduire la barre latérale"
            }
            aria-pressed={collapsed}
            aria-controls="sidebar-nav"
            className="inline-flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            {collapsed ? (
              <PanelLeftOpen className="size-4" aria-hidden="true" />
            ) : (
              <PanelLeftClose className="size-4" aria-hidden="true" />
            )}
          </button>
        </div>

        <nav
          id="sidebar-nav"
          aria-label="Espaces"
          className={cn(
            "flex flex-col gap-1",
            collapsed ? "items-center px-1 py-2" : "px-3 py-2",
          )}
        >
          {spaces.map(({ to, label, shortcut, Icon }) => {
            // Match `/chat` and `/chat/...` (route segment) but NOT
            // `/chat-history`.
            const isActive =
              location.pathname === to ||
              location.pathname.startsWith(`${to}/`);
            const link = (
              <Link
                key={to}
                to={to}
                className={cn(
                  "group flex items-center rounded-md text-sm font-medium transition-colors",
                  collapsed
                    ? "size-10 justify-center"
                    : "justify-between px-3 py-2",
                  isActive
                    ? "bg-muted text-foreground"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground",
                )}
                aria-current={isActive ? "page" : undefined}
                // The link's accessible *name* is always the space label; the
                // tooltip (in collapsed mode) supplies the shortcut as a
                // *description*, wired via Radix `aria-describedby` to avoid
                // double-announce on screen readers.
                aria-label={label}
              >
                <span className="flex items-center gap-2">
                  <Icon className="size-4" aria-hidden="true" />
                  {!collapsed && label}
                </span>
                {!collapsed && (
                  <kbd className="hidden rounded border border-border bg-background px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground group-hover:inline-block group-focus-within:inline-block">
                    {shortcut}
                  </kbd>
                )}
              </Link>
            );

            // In collapsed mode, wrap each link in a Radix Tooltip showing
            // the shortcut. In expanded mode, no tooltip — the visible kbd
            // (visible on hover or keyboard focus) replaces it.
            return collapsed ? (
              <Tooltip key={to}>
                <TooltipTrigger asChild>{link}</TooltipTrigger>
                <TooltipContent side="right">{shortcut}</TooltipContent>
              </Tooltip>
            ) : (
              link
            );
          })}
        </nav>

        <div
          className={cn(
            "mt-auto flex items-center text-xs text-muted-foreground",
            collapsed ? "flex-col gap-2 px-1 py-3" : "justify-between px-4 py-3",
          )}
        >
          {!collapsed && <span>Sprint 0 · v0.1.0</span>}
          {footerSlot && collapsed && footerSlotTooltip ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <span>{footerSlot}</span>
              </TooltipTrigger>
              <TooltipContent side="right">{footerSlotTooltip}</TooltipContent>
            </Tooltip>
          ) : (
            footerSlot
          )}
        </div>
      </aside>
    </TooltipProvider>
  );
}
