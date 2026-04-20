/**
 * Sidebar principale — 240px fixe, 4 espaces MVP (Dashboard / Chat / Trace / Config).
 * Raccourcis clavier : Cmd+1 .. Cmd+4 (implémentés en Story 6.5 / 1.8).
 */

import { Link, useRouterState } from "@tanstack/react-router";
import {
  LayoutDashboard,
  MessageSquare,
  GitBranch,
  Settings,
} from "lucide-react";
import { cn } from "@/shared/lib/utils";

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

export function Sidebar() {
  const { location } = useRouterState();

  return (
    <aside
      aria-label="Navigation principale"
      className="flex w-60 shrink-0 flex-col border-r border-border bg-card"
    >
      <div className="px-6 py-5 text-lg font-semibold tracking-tight">
        Agentive
      </div>
      <nav className="flex flex-col gap-1 px-3" aria-label="Espaces">
        {spaces.map(({ to, label, shortcut, Icon }) => {
          // Match `/chat` and `/chat/...` (route segment) but NOT `/chat-history`.
          const isActive =
            location.pathname === to ||
            location.pathname.startsWith(`${to}/`);
          return (
            <Link
              key={to}
              to={to}
              className={cn(
                "group flex items-center justify-between rounded-md px-3 py-2 text-sm font-medium transition-colors",
                isActive
                  ? "bg-muted text-foreground"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground",
              )}
              aria-current={isActive ? "page" : undefined}
            >
              <span className="flex items-center gap-2">
                <Icon className="size-4" aria-hidden="true" />
                {label}
              </span>
              <kbd className="hidden rounded border border-border bg-background px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground group-hover:inline-block">
                {shortcut}
              </kbd>
            </Link>
          );
        })}
      </nav>
      <div className="mt-auto px-6 py-4 text-xs text-muted-foreground">
        Sprint 0 · v0.1.0
      </div>
    </aside>
  );
}
