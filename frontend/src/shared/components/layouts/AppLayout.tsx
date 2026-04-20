import { Outlet } from "@tanstack/react-router";
import { Sidebar } from "./Sidebar";

/**
 * AppLayout — sidebar fixe 240px + main content fluide (max-width 1440px).
 */
export function AppLayout() {
  return (
    <div className="flex h-screen bg-background text-foreground">
      <Sidebar />
      <main className="flex-1 overflow-auto">
        <div className="mx-auto h-full max-w-[1440px] px-6 py-8">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
