import { Link, createFileRoute } from "@tanstack/react-router";
import { Plus } from "lucide-react";
import { Button } from "@/shared/components/ui/button";

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
          Agents, namespaces mémoire, outils, API keys, budget caps.{" "}
          <span className="font-mono text-xs">[implémenté Epics 2, 3, 9]</span>
        </p>
      </header>

      <div className="rounded-lg border border-border bg-card p-6">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold">Agents</h2>
            <p className="text-sm text-muted-foreground">
              Templates d'agents construits depuis les 8 archétypes universels.{" "}
              <span className="font-mono text-xs">[Story 2.1+]</span>
            </p>
          </div>
          <Button asChild>
            <Link to="/config/agents/new">
              <Plus className="size-4" aria-hidden="true" />
              Nouveau
            </Link>
          </Button>
        </div>
      </div>

      <div className="rounded-lg border border-border bg-card p-6">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold">Outils MCP</h2>
            <p className="text-sm text-muted-foreground">
              Serveurs MCP enregistrés et outils découverts. Assignation aux agents.{" "}
              <span className="font-mono text-xs">[Story 2.5]</span>
            </p>
          </div>
          <Button asChild variant="outline">
            <Link to="/config/tools">Gérer</Link>
          </Button>
        </div>
      </div>

      <div className="rounded-lg border border-border bg-card p-6">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold">Playground</h2>
            <p className="text-sm text-muted-foreground">
              Testez un agent en isolation (no events, no memory writes).{" "}
              <span className="font-mono text-xs">[Story 2.7]</span>
            </p>
          </div>
          <Button asChild variant="outline">
            <Link to="/config/playground">Ouvrir</Link>
          </Button>
        </div>
      </div>
    </section>
  );
}
