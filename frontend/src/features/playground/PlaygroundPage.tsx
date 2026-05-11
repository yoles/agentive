/**
 * PlaygroundPage — Story 2.7 T7.5.
 *
 * Split view (Tailwind grid cols-2 Sprint 1, D77 resize handle défer
 * Sprint 2+) : form à gauche, output à droite.
 */

import { AgentTesterForm } from "./AgentTesterForm";
import { OutputInspector } from "./OutputInspector";
import { usePlaygroundRun } from "./hooks";

type Props = {
  templateId: string;
};

export function PlaygroundPage({ templateId }: Props) {
  const mutation = usePlaygroundRun(templateId);

  return (
    <section
      className="mx-auto flex max-w-7xl flex-col gap-6 px-6 py-8"
      data-testid="playground-page"
    >
      <header>
        <h1 className="text-2xl font-bold">Playground</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Testez un agent en isolation : aucun event publié sur le bus principal,
          aucune écriture mémoire, aucun agent-instance créé.
        </p>
      </header>
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <div className="rounded-md border border-border bg-card p-6">
          <h2 className="mb-4 text-lg font-semibold">Entrée</h2>
          <AgentTesterForm
            templateId={templateId}
            isRunning={mutation.isPending}
            onSubmit={(req) => mutation.mutate(req)}
          />
        </div>
        <div className="rounded-md border border-border bg-card p-6">
          <h2 className="mb-4 text-lg font-semibold">Sortie</h2>
          <OutputInspector
            result={mutation.data}
            isPending={mutation.isPending}
            error={mutation.error ?? undefined}
          />
        </div>
      </div>
    </section>
  );
}
