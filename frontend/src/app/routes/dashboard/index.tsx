import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/dashboard/")({
  component: DashboardPage,
});

function DashboardPage() {
  return (
    <section aria-labelledby="dashboard-heading" className="flex flex-col gap-6">
      <header>
        <h1 id="dashboard-heading" className="text-3xl font-semibold tracking-tight">
          Dashboard
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Cockpit de pilotage — métriques, alertes, résumé Sprint Reporter.
          {" "}
          <span className="font-mono text-xs">[implémenté Epic 7]</span>
        </p>
      </header>

      <div className="rounded-lg border border-border bg-card p-6">
        <p className="text-sm text-muted-foreground">
          Tes métriques apparaîtront dès la première tâche.
        </p>
      </div>
    </section>
  );
}
