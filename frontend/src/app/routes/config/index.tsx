import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/config/")({
  component: ConfigPage,
});

export function ConfigPage() {
  return (
    <section aria-labelledby="config-heading" className="flex flex-col gap-6">
      <header>
        <h1 id="config-heading" className="text-3xl font-semibold tracking-tight">
          Configuration
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Agents, namespaces mémoire, outils, API keys, budget caps.
          {" "}
          <span className="font-mono text-xs">[implémenté Epics 2, 3, 9]</span>
        </p>
      </header>

      <div className="rounded-lg border border-border bg-card p-6">
        <p className="text-sm text-muted-foreground">
          Configuration à venir.
        </p>
      </div>
    </section>
  );
}
