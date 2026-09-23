import { createFileRoute } from "@tanstack/react-router";
import { Plus } from "lucide-react";
import { useState } from "react";
import { Button } from "@/shared/components/ui/button";
import { AddToolServerDialog, useToolServers } from "@/features/tool_hub";

export const Route = createFileRoute("/config/tools/")({
  component: ToolServersPage,
});

export function ToolServersPage() {
  const serversQuery = useToolServers();
  const [addDialogOpen, setAddDialogOpen] = useState(false);

  return (
    <section
      aria-labelledby="tool-servers-heading"
      className="container mx-auto flex max-w-3xl flex-col gap-6 py-6"
    >
      <header className="flex items-center justify-between">
        <div>
          <h1
            id="tool-servers-heading"
            className="text-3xl font-semibold tracking-tight"
          >
            Serveurs MCP
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Branchez des serveurs MCP (stdio ou SSE) pour découvrir les outils
            qu'ils exposent. Vous pourrez ensuite les assigner à vos agents.
          </p>
        </div>
        <Button onClick={() => setAddDialogOpen(true)} data-testid="add-server-button">
          <Plus className="size-4" aria-hidden="true" />
          Ajouter un serveur
        </Button>
      </header>

      {serversQuery.isLoading && (
        <p className="text-sm text-muted-foreground">Chargement…</p>
      )}

      {serversQuery.isError && (
        <p className="text-sm text-destructive" role="alert">
          Échec du chargement des serveurs.
        </p>
      )}

      {serversQuery.data && serversQuery.data.length === 0 && (
        <div
          className="rounded-lg border border-dashed border-border p-8 text-center"
          data-testid="tool-servers-empty"
        >
          <p className="text-sm text-muted-foreground">
            Aucun serveur MCP enregistré. Branchez votre premier serveur pour donner
            des capacités à vos agents.
          </p>
          <Button onClick={() => setAddDialogOpen(true)} className="mt-4">
            <Plus className="size-4" aria-hidden="true" />
            Ajouter un serveur
          </Button>
        </div>
      )}

      {serversQuery.data && serversQuery.data.length > 0 && (
        <table className="w-full border-collapse text-sm" data-testid="tool-servers-table">
          <thead>
            <tr className="border-b text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th className="py-2">Nom</th>
              <th className="py-2">Transport</th>
              <th className="py-2">Status</th>
              <th className="py-2">Outils</th>
              <th className="py-2">Découvert</th>
            </tr>
          </thead>
          <tbody>
            {serversQuery.data.map((server) => (
              <tr
                key={server.server_id}
                className="border-b last:border-b-0"
                data-testid={`server-row-${server.server_id}`}
              >
                <td className="py-3 font-medium">{server.name}</td>
                <td className="py-3">
                  <span className="rounded bg-muted px-2 py-0.5 text-xs uppercase tracking-wide">
                    {server.transport}
                  </span>
                </td>
                <td className="py-3">
                  <span
                    className={
                      server.status === "active"
                        ? "text-status-success"
                        : "text-muted-foreground"
                    }
                  >
                    {server.status}
                  </span>
                </td>
                <td className="py-3">{server.tools_count}</td>
                <td className="py-3 text-xs text-muted-foreground">
                  {new Date(server.discovered_at).toLocaleString("fr-FR")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <AddToolServerDialog open={addDialogOpen} onOpenChange={setAddDialogOpen} />
    </section>
  );
}
