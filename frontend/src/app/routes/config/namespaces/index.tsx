import { createFileRoute, Link } from "@tanstack/react-router";
import { Plus } from "lucide-react";
import { useState } from "react";
import { Button } from "@/shared/components/ui/button";
import {
  CreateNamespaceDialog,
  departmentLabel,
  formatRetention,
  groupByDepartmentThenType,
  useNamespaces,
} from "@/features/memory_manager";

export const Route = createFileRoute("/config/namespaces/")({
  component: NamespacesPage,
});

export function NamespacesPage() {
  const namespacesQuery = useNamespaces();
  const [createDialogOpen, setCreateDialogOpen] = useState(false);

  const groups = namespacesQuery.data ? groupByDepartmentThenType(namespacesQuery.data) : [];

  return (
    <section
      aria-labelledby="namespaces-heading"
      className="container mx-auto flex max-w-3xl flex-col gap-6 py-6"
    >
      <header className="flex items-center justify-between">
        <div>
          <h1 id="namespaces-heading" className="text-3xl font-semibold tracking-tight">
            Namespaces mémoire
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Isolation de la mémoire par département/projet, 4 types avec rétention
            distincte (client, métier, opérationnelle, contextuelle).
          </p>
        </div>
        <Button onClick={() => setCreateDialogOpen(true)} data-testid="create-namespace-button">
          <Plus className="size-4" aria-hidden="true" />
          Nouveau namespace
        </Button>
      </header>

      {namespacesQuery.isLoading && (
        <p className="text-sm text-muted-foreground">Chargement…</p>
      )}

      {namespacesQuery.isError && (
        <p className="text-sm text-destructive" role="alert">
          Échec du chargement des namespaces.
        </p>
      )}

      {namespacesQuery.data && namespacesQuery.data.length === 0 && (
        <div
          className="rounded-lg border border-dashed border-border p-8 text-center"
          data-testid="namespaces-empty"
        >
          <p className="text-sm text-muted-foreground">
            Aucun namespace. Créez-en un pour organiser la mémoire des agents.
          </p>
          <Button onClick={() => setCreateDialogOpen(true)} className="mt-4">
            <Plus className="size-4" aria-hidden="true" />
            Nouveau namespace
          </Button>
        </div>
      )}

      {groups.map((group) => (
        <div key={group.department ?? "__no_department__"} className="flex flex-col gap-3">
          <h2
            className="text-lg font-semibold"
            data-testid={`department-${group.department ?? "__no_department__"}`}
          >
            {departmentLabel(group.department)}
          </h2>
          {group.types.map(({ type, namespaces }) => (
            <div key={type} className="flex flex-col gap-2">
              <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                {type}
              </h3>
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="border-b text-left text-xs uppercase tracking-wide text-muted-foreground">
                    <th className="py-2">Nom</th>
                    <th className="py-2">Projet</th>
                    <th className="py-2">Rétention</th>
                    <th className="py-2">Chunks</th>
                  </tr>
                </thead>
                <tbody>
                  {namespaces.map((ns) => (
                    <tr
                      key={ns.namespace_id}
                      className="border-b last:border-b-0"
                      data-testid={`namespace-row-${ns.namespace_id}`}
                    >
                      <td className="py-3 font-medium">
                        <Link
                          to="/config/namespaces/$name"
                          params={{ name: ns.name }}
                          className="text-primary underline-offset-2 hover:underline"
                          data-testid={`namespace-link-${ns.namespace_id}`}
                        >
                          {ns.name}
                        </Link>
                      </td>
                      <td className="py-3 text-muted-foreground">{ns.project ?? "—"}</td>
                      <td className="py-3">
                        {ns.retention_policy_valid ? (
                          formatRetention(ns.retention_policy.default_ttl_seconds)
                        ) : (
                          <span
                            className="font-medium text-destructive"
                            title="La donnée de rétention en base est corrompue et ne peut pas être interprétée. Contactez un développeur."
                            data-testid={`retention-invalid-${ns.namespace_id}`}
                          >
                            donnée invalide
                          </span>
                        )}
                      </td>
                      <td className="py-3">{ns.chunk_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      ))}

      <CreateNamespaceDialog open={createDialogOpen} onOpenChange={setCreateDialogOpen} />
    </section>
  );
}
