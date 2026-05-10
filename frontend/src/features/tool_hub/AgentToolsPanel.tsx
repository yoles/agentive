/**
 * `AgentToolsPanel` — Story 2.5 AC6.
 *
 * Rendu en bas de la page `/config/agents/{templateId}` (Story 2.3 host
 * inchangé — anti-scope strict). Affiche les outils MCP groupés par
 * serveur avec checkbox d'assignment, et un bouton "Sauvegarder" qui
 * REPLACE la liste d'assignments via POST /agents/templates/{id}/tools.
 *
 * UX :
 * - Empty state si 0 serveur MCP enregistré → CTA "Configurer un serveur MCP"
 *   link vers `/config/tools`.
 * - Loading state via skeleton primitive.
 * - Pré-cochage des tools déjà assignés.
 * - Le bouton "Sauvegarder" devient disabled quand la sélection courante
 *   == la sélection serveur (no-op).
 */

import { Link } from "@tanstack/react-router";
import { useMemo, useState, useEffect, useRef } from "react";
import { toast } from "sonner";
import { Button } from "@/shared/components/ui/button";
import { Checkbox } from "@/shared/components/ui/checkbox";
import {
  useAgentTools,
  useReplaceAgentTools,
  useToolServers,
} from "./hooks";
import { getToolServer } from "./api";
import type { ToolInfo, ToolServer } from "./types";
import { useQueries } from "@tanstack/react-query";

type Props = {
  templateId: string;
};

export function AgentToolsPanel({ templateId }: Props) {
  const serversQuery = useToolServers();
  const assignedQuery = useAgentTools(templateId);
  const replaceMutation = useReplaceAgentTools(templateId);

  // Fetch the detail (with tools) of every server in parallel — TanStack
  // useQueries lets us keep the cache scoped per server.
  const servers = serversQuery.data ?? [];
  const detailQueries = useQueries({
    queries: servers.map((s) => ({
      queryKey: ["tool-server", s.server_id],
      queryFn: () => getToolServer(s.server_id),
      staleTime: 30_000,
    })),
  });

  const allDetailsLoaded = detailQueries.every((q) => q.isSuccess);
  const anyDetailLoading = detailQueries.some((q) => q.isLoading);

  // Local UI state — the `selectedToolIds` set is the staged selection.
  const [selectedToolIds, setSelectedToolIds] = useState<Set<string>>(new Set());

  // P-02 (CR 2026-05-10) — only seed the local selection ONCE from the server
  // (initial load OR after a successful save). Background refetches (window
  // focus, staleTime expiry, sibling mutation invalidation) MUST NOT clobber
  // the user's in-progress checkbox edits.
  const assignedIds = useMemo(
    () => new Set((assignedQuery.data?.assigned_tools ?? []).map((t) => t.tool_id)),
    [assignedQuery.data],
  );
  const seededRef = useRef(false);
  useEffect(() => {
    if (assignedQuery.isSuccess && !seededRef.current) {
      setSelectedToolIds(new Set(assignedIds));
      seededRef.current = true;
    }
  }, [assignedQuery.isSuccess, assignedIds]);

  if (serversQuery.isLoading || assignedQuery.isLoading) {
    return (
      <section className="mt-8 border-t pt-6">
        <h2 className="text-lg font-semibold">Outils MCP assignés à cet agent</h2>
        <p className="mt-2 text-sm text-muted-foreground">Chargement des outils…</p>
      </section>
    );
  }

  if (servers.length === 0) {
    return (
      <section className="mt-8 border-t pt-6" data-testid="agent-tools-panel-empty">
        <h2 className="text-lg font-semibold">Outils MCP assignés à cet agent</h2>
        <p className="mt-2 text-sm text-muted-foreground">
          Aucun serveur MCP enregistré. Branchez votre premier serveur pour donner
          des capacités à vos agents.
        </p>
        <Button asChild className="mt-3">
          <Link to="/config/tools">Ajouter votre premier serveur MCP</Link>
        </Button>
      </section>
    );
  }

  function toggleTool(toolId: string) {
    setSelectedToolIds((prev) => {
      const next = new Set(prev);
      if (next.has(toolId)) {
        next.delete(toolId);
      } else {
        next.add(toolId);
      }
      return next;
    });
  }

  // Diff vs assignedIds — disable Save when the selection equals the saved set.
  const hasChanges =
    selectedToolIds.size !== assignedIds.size ||
    [...selectedToolIds].some((id) => !assignedIds.has(id));

  async function handleSave() {
    try {
      await replaceMutation.mutateAsync({ tool_ids: [...selectedToolIds] });
      // After a successful save, allow the next assignedQuery refresh to
      // re-seed the local set (so external changes / multi-tab edits land).
      seededRef.current = false;
      toast.success("Outils mis à jour");
    } catch (err) {
      const apiError = err as { detail?: string; title?: string };
      toast.error(apiError.detail ?? apiError.title ?? "Échec de la mise à jour des outils");
    }
  }

  return (
    <section
      className="mt-8 border-t pt-6"
      aria-labelledby="agent-tools-panel-heading"
      data-testid="agent-tools-panel"
    >
      <header className="flex items-center justify-between">
        <h2 id="agent-tools-panel-heading" className="text-lg font-semibold">
          Outils MCP assignés à cet agent
        </h2>
        <Button asChild variant="outline" size="sm">
          <Link to="/config/tools">Configurer les serveurs MCP</Link>
        </Button>
      </header>

      <p className="mt-1 text-xs text-muted-foreground">
        Cochez les outils que cet agent peut appeler. L'exécution runtime arrive avec
        la Story 2.6 (sandbox).
      </p>

      {anyDetailLoading && (
        <p className="mt-3 text-sm text-muted-foreground">Chargement des serveurs…</p>
      )}

      {allDetailsLoaded && (
        <div className="mt-4 flex flex-col gap-4">
          {detailQueries.map((q, idx) => {
            const server = servers[idx];
            const detail = q.data;
            if (!detail) return null;
            return (
              <ServerToolsGroup
                key={server.server_id}
                server={server}
                tools={detail.tools}
                selectedToolIds={selectedToolIds}
                onToggle={toggleTool}
              />
            );
          })}
        </div>
      )}

      <div className="mt-4 flex items-center gap-3">
        <Button
          type="button"
          onClick={handleSave}
          disabled={!hasChanges || replaceMutation.isPending}
          data-testid="agent-tools-save"
        >
          {replaceMutation.isPending ? "Sauvegarde…" : "Sauvegarder les assignments"}
        </Button>
        {!hasChanges && (
          <span className="text-xs text-muted-foreground">Aucune modification.</span>
        )}
      </div>
    </section>
  );
}

function ServerToolsGroup({
  server,
  tools,
  selectedToolIds,
  onToggle,
}: {
  server: ToolServer;
  tools: ToolInfo[];
  selectedToolIds: Set<string>;
  onToggle: (toolId: string) => void;
}) {
  return (
    <div
      className="rounded-md border border-border bg-card p-4"
      data-testid={`tool-group-${server.server_id}`}
    >
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-semibold">{server.name}</h3>
        <span className="rounded bg-muted px-2 py-0.5 text-xs uppercase tracking-wide">
          {server.transport}
        </span>
      </div>
      {tools.length === 0 ? (
        <p className="text-xs text-muted-foreground">Ce serveur n'expose aucun outil.</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {tools.map((tool) => (
            <li key={tool.tool_id} className="flex items-start gap-3">
              <Checkbox
                id={`tool-checkbox-${tool.tool_id}`}
                checked={selectedToolIds.has(tool.tool_id)}
                onCheckedChange={() => onToggle(tool.tool_id)}
                data-testid={`tool-checkbox-${tool.tool_id}`}
              />
              <div className="flex flex-col">
                <label
                  htmlFor={`tool-checkbox-${tool.tool_id}`}
                  className="cursor-pointer text-sm font-medium"
                >
                  {tool.name}
                </label>
                {tool.description && (
                  <span className="text-xs text-muted-foreground">{tool.description}</span>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
