/**
 * `/config/agents/$templateId` — placeholder post-create redirect target.
 *
 * Story 2.1 ships a minimal version that displays the template_id and
 * directs the operator to Story 2.2 for full configuration UI.
 */

import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/config/agents/$templateId")({
  component: AgentTemplateDetail,
});

export function AgentTemplateDetail() {
  const { templateId } = Route.useParams();
  return (
    <section
      aria-labelledby="agent-template-heading"
      className="flex flex-col gap-4"
    >
      <header>
        <h1
          id="agent-template-heading"
          className="text-3xl font-semibold tracking-tight"
        >
          Template d'agent
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Identifiant&nbsp;:{" "}
          <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">
            {templateId}
          </code>
        </p>
      </header>

      <div className="rounded-lg border border-border bg-card p-6">
        <p className="text-sm text-muted-foreground">
          Configuration détaillée (system prompt, modèle LLM, contrats
          élastiques, error policy)&nbsp;: arrive avec la Story 2.2.
        </p>
      </div>
    </section>
  );
}
