/**
 * `/config/namespaces/$name` — namespace detail + chunk purge (Story 3.6 AC5).
 *
 * First child route under `/config/namespaces` (the list page navigates to
 * no detail today). Addressed by `name`, not a UUID — consistent with the
 * backend, which addresses namespaces by name everywhere
 * (`PATCH /memory/namespaces/{namespace_name}`, `GET /memory/chunks?namespace=`).
 *
 * Reuses `useNamespaces()` for the header info (no dedicated
 * `GET /memory/namespaces/{name}` endpoint exists) and the new
 * `useNamespaceChunks` hook for the chunk table.
 */

import { createFileRoute, Link } from "@tanstack/react-router";
import { useState } from "react";
import { Button } from "@/shared/components/ui/button";
import { Input } from "@/shared/components/ui/input";
import {
  departmentLabel,
  formatRetention,
  PurgeChunksDialog,
  useNamespaceChunks,
  useNamespaces,
} from "@/features/memory_manager";

export const Route = createFileRoute("/config/namespaces/$name")({
  component: NamespaceDetailPage,
});

/** `<input type="date">` yields a bare `YYYY-MM-DD` — the backend's
 * `created_after`/`created_before` are full `datetime`s. Midnight local
 * time is close enough for an admin filter (not a precision-sensitive
 * boundary), and matches what a human typing a date would expect. */
function dateInputToIso(value: string): string | undefined {
  if (!value) return undefined;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? undefined : parsed.toISOString();
}

export function NamespaceDetailPage() {
  const { name } = Route.useParams();
  const namespacesQuery = useNamespaces();
  const namespace = namespacesQuery.data?.find((ns) => ns.name === name) ?? null;

  const [includeArchived, setIncludeArchived] = useState(false);
  const [contentContains, setContentContains] = useState("");
  const [createdAfter, setCreatedAfter] = useState("");
  const [createdBefore, setCreatedBefore] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [purgeDialogOpen, setPurgeDialogOpen] = useState(false);

  const chunksQuery = useNamespaceChunks(name, {
    includeArchived,
    contentContains: contentContains.trim() || undefined,
    createdAfter: dateInputToIso(createdAfter),
    createdBefore: dateInputToIso(createdBefore),
  });
  const chunks = chunksQuery.data ?? [];

  function toggleSelected(chunkId: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(chunkId)) next.delete(chunkId);
      else next.add(chunkId);
      return next;
    });
  }

  function toggleSelectAll() {
    setSelected((prev) => (prev.size === chunks.length ? new Set() : new Set(chunks.map((c) => c.chunk_id))));
  }

  return (
    <section
      aria-labelledby="namespace-detail-heading"
      className="container mx-auto flex max-w-4xl flex-col gap-6 py-6"
    >
      <Link to="/config/namespaces" className="text-sm text-primary underline">
        ← Retour aux namespaces
      </Link>

      <header>
        <h1 id="namespace-detail-heading" className="text-3xl font-semibold tracking-tight">
          {name}
        </h1>
        {namespace && (
          <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1 text-sm text-muted-foreground sm:grid-cols-4">
            <div>
              <dt className="font-medium text-foreground">Type</dt>
              <dd>{namespace.type}</dd>
            </div>
            <div>
              <dt className="font-medium text-foreground">Département</dt>
              <dd>{departmentLabel(namespace.department)}</dd>
            </div>
            <div>
              <dt className="font-medium text-foreground">Backend d'embedding</dt>
              <dd data-testid="namespace-embedding-backend">{namespace.embedding_backend}</dd>
            </div>
            <div>
              <dt className="font-medium text-foreground">Rétention</dt>
              <dd>
                {namespace.retention_policy_valid
                  ? formatRetention(namespace.retention_policy.default_ttl_seconds)
                  : "donnée invalide"}
              </dd>
            </div>
          </dl>
        )}
      </header>

      <div className="flex flex-wrap items-end gap-4 rounded-lg border border-border p-4">
        <div className="flex flex-col gap-1">
          <label htmlFor="chunk-filter-content" className="text-xs font-medium">
            Tag / contenu
          </label>
          <Input
            id="chunk-filter-content"
            value={contentContains}
            onChange={(e) => setContentContains(e.target.value)}
            placeholder="facture, contrat…"
            className="w-48"
            data-testid="chunk-filter-content"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="chunk-filter-after" className="text-xs font-medium">
            Créé après
          </label>
          <Input
            id="chunk-filter-after"
            type="date"
            value={createdAfter}
            onChange={(e) => setCreatedAfter(e.target.value)}
            data-testid="chunk-filter-created-after"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="chunk-filter-before" className="text-xs font-medium">
            Créé avant
          </label>
          <Input
            id="chunk-filter-before"
            type="date"
            value={createdBefore}
            onChange={(e) => setCreatedBefore(e.target.value)}
            data-testid="chunk-filter-created-before"
          />
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={includeArchived}
            onChange={(e) => setIncludeArchived(e.target.checked)}
            data-testid="chunk-filter-include-archived"
          />
          Inclure les chunks archivés
        </label>
      </div>

      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {chunks.length} chunk{chunks.length > 1 ? "s" : ""}
          {selected.size > 0 ? ` · ${selected.size} sélectionné(s)` : ""}
        </p>
        <Button
          type="button"
          variant="destructive"
          disabled={selected.size === 0}
          onClick={() => setPurgeDialogOpen(true)}
          data-testid="purge-selected-button"
        >
          Purger les chunks sélectionnés
        </Button>
      </div>

      {chunksQuery.isLoading && <p className="text-sm text-muted-foreground">Chargement…</p>}
      {chunksQuery.isError && (
        <p className="text-sm text-destructive" role="alert">
          Échec du chargement des chunks.
        </p>
      )}

      {chunksQuery.data && chunks.length === 0 && (
        <p className="text-sm text-muted-foreground" data-testid="chunks-empty">
          Aucun chunk ne correspond à ces filtres.
        </p>
      )}

      {chunks.length > 0 && (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th className="w-8 py-2">
                <input
                  type="checkbox"
                  checked={selected.size === chunks.length}
                  onChange={toggleSelectAll}
                  aria-label="Tout sélectionner"
                  data-testid="select-all-chunks"
                />
              </th>
              <th className="py-2">Contenu</th>
              <th className="py-2">Créé le</th>
              <th className="py-2">Expire le</th>
              <th className="py-2">Archivé</th>
            </tr>
          </thead>
          <tbody>
            {chunks.map((chunk) => (
              <tr
                key={chunk.chunk_id}
                className="border-b last:border-b-0"
                data-testid={`chunk-row-${chunk.chunk_id}`}
              >
                <td className="py-3">
                  <input
                    type="checkbox"
                    checked={selected.has(chunk.chunk_id)}
                    onChange={() => toggleSelected(chunk.chunk_id)}
                    aria-label={`Sélectionner le chunk ${chunk.chunk_id}`}
                  />
                </td>
                <td className="max-w-md truncate py-3" title={chunk.content}>
                  {chunk.content}
                </td>
                <td className="py-3 text-muted-foreground">
                  {new Date(chunk.created_at).toLocaleString()}
                </td>
                <td className="py-3 text-muted-foreground">
                  {chunk.expires_at ? new Date(chunk.expires_at).toLocaleString() : "—"}
                </td>
                <td className="py-3 text-muted-foreground">{chunk.archived_at ? "Oui" : "Non"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <PurgeChunksDialog
        open={purgeDialogOpen}
        onOpenChange={setPurgeDialogOpen}
        namespace={name}
        chunkIds={[...selected]}
        onSettled={() => setSelected(new Set())}
      />
    </section>
  );
}
