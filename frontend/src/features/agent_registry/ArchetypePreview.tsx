/**
 * `ArchetypePreview` — UX-DR17 preview pane shown when an archetype is selected.
 *
 * Story 2.1 AC4 :
 * - First 500 chars of `prompt_base` rendered in `<pre>` with `whitespace-pre-wrap`.
 * - `input_contract` / `output_contract` rendered as `<dl>` key/value pairs.
 *
 * Loads `getArchetypeDetail` lazily on selection (TanStack Query).
 */

import { useArchetypeDetail } from "./hooks";
import type { ContractSkeleton } from "./types";

const PROMPT_PREVIEW_LENGTH = 500;

function FieldList({
  fields,
  emptyLabel,
}: {
  fields: Record<string, unknown>;
  emptyLabel: string;
}) {
  const entries = Object.entries(fields);
  if (entries.length === 0) {
    return <p className="text-xs italic text-muted-foreground">{emptyLabel}</p>;
  }
  return (
    <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-xs">
      {entries.map(([key, type]) => (
        <div key={key} className="contents">
          <dt className="font-mono text-foreground">{key}</dt>
          <dd className="font-mono text-muted-foreground">{String(type)}</dd>
        </div>
      ))}
    </dl>
  );
}

function ContractList({ label, contract }: { label: string; contract: ContractSkeleton }) {
  // Story 2.1 P-10 — `?? {}` defends against API drift sending null/undefined
  //                   for `core` or `extras` (e.g. server returns a partial
  //                   payload). Story 2.1 P-13 — render `extras` alongside
  //                   `core` so the AC4 wording "input_contract +
  //                   output_contract rendus en <dl>" is satisfied for
  //                   archetypes that ever ship non-empty `extras`.
  const core = contract.core ?? {};
  const extras = contract.extras ?? {};
  return (
    <div className="flex flex-col gap-3">
      <div>
        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {label} — core
        </h4>
        <FieldList fields={core} emptyLabel="Aucun champ noyau." />
      </div>
      <div>
        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {label} — extras
        </h4>
        <FieldList fields={extras} emptyLabel="Zone flexible vide." />
      </div>
    </div>
  );
}

export function ArchetypePreview({ archetypeId }: { archetypeId: string | null }) {
  const { data, isLoading, isError } = useArchetypeDetail(archetypeId);

  if (archetypeId === null) {
    return (
      <p className="text-sm text-muted-foreground">
        Sélectionnez un archétype pour voir son aperçu.
      </p>
    );
  }

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Chargement de l'aperçu…</p>;
  }

  if (isError || data == null) {
    return (
      <p className="text-sm text-destructive">
        Impossible de charger les détails de l'archétype.
      </p>
    );
  }

  const promptPreview =
    data.prompt_base.length > PROMPT_PREVIEW_LENGTH
      ? `${data.prompt_base.slice(0, PROMPT_PREVIEW_LENGTH)}…`
      : data.prompt_base;

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h3 className="text-sm font-semibold">{data.display_name}</h3>
        <p className="mt-1 text-xs text-muted-foreground">{data.description}</p>
      </div>

      <div>
        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Prompt de base
        </h4>
        <pre className="mt-2 whitespace-pre-wrap rounded-md border border-border bg-muted/40 p-3 font-mono text-xs">
          {promptPreview}
        </pre>
      </div>

      <ContractList label="Contrat d'entrée" contract={data.input_contract} />
      <ContractList label="Contrat de sortie" contract={data.output_contract} />
    </div>
  );
}
