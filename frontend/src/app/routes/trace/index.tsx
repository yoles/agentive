import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/trace/")({
  component: TracePage,
});

export function TracePage() {
  return (
    <section aria-labelledby="trace-heading" className="flex flex-col gap-6">
      <header>
        <h1 id="trace-heading" className="text-3xl font-semibold tracking-tight">
          Trace Explorer
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Diagnostic des workflows — sources → données → raisonnement → décision.
          {" "}
          <span className="font-mono text-xs">[implémenté Epic 8]</span>
        </p>
      </header>

      <div className="rounded-lg border border-border bg-card p-6">
        <p className="text-sm text-muted-foreground">
          Les exécutions des agents s'affichent ici.
        </p>
      </div>
    </section>
  );
}
