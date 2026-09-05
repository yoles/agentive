/**
 * `/config/agents/new` — create a new agent template from an archetype.
 *
 * Story 2.1 AC4 + AC5 :
 * - Layout `grid-cols-1 md:grid-cols-3` (selector 2 cols + preview/form 1 col).
 * - Submit → `useCreateTemplate().mutateAsync` → toast + redirect to
 *   `/config/agents/$templateId`.
 *
 * Mode "Wizard" / "Expert" Story 2.3 — for now, single Expert form (minimal).
 */

import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { toast } from "sonner";
import {
  ArchetypePreview,
  ArchetypeSelector,
  useArchetypes,
  useCreateTemplate,
} from "@/features/agent_registry";
import { Button } from "@/shared/components/ui/button";
import { Input } from "@/shared/components/ui/input";
import {
  isApiTokenConfigured,
  setApiToken,
  type ApiError,
} from "@/shared/api/client";

export const Route = createFileRoute("/config/agents/new")({
  component: NewAgentTemplatePage,
});

const MAX_NAME_LENGTH = 255;

export function NewAgentTemplatePage() {
  const navigate = useNavigate();
  const [archetypeId, setArchetypeId] = useState<string | null>(null);
  const [name, setName] = useState<string>("");
  const [tokenInput, setTokenInput] = useState("");
  const [authConfigured, setAuthConfigured] = useState(isApiTokenConfigured);

  const archetypesQuery = useArchetypes(authConfigured);
  const createMutation = useCreateTemplate();
  const queryError = archetypesQuery.error as Partial<ApiError> | null;
  const needsToken = !authConfigured || queryError?.status === 401;

  const trimmedName = name.trim();
  const submitDisabled =
    archetypeId === null ||
    trimmedName.length === 0 ||
    trimmedName.length > MAX_NAME_LENGTH ||
    createMutation.isPending;

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (archetypeId === null) return;

    try {
      const response = await createMutation.mutateAsync({
        archetype: archetypeId,
        name: trimmedName,
      });
      toast.success(`Template « ${response.name} » créé`);
      void navigate({
        to: "/config/agents/$templateId",
        params: { templateId: response.template_id },
      });
    } catch (err) {
      const apiError = err as Partial<ApiError>;
      // Story 2.1 P-11 — branch on status so the user gets a targeted
      // message and the right input is highlighted. 409 (duplicate name)
      // is the most common UX path; we focus the name input and keep the
      // form filled so the operator can rename without retyping.
      if (apiError.status === 409) {
        toast.error(
          `Un template « ${trimmedName} » existe déjà. Choisis un autre nom.`,
        );
        const nameInput = document.getElementById("template-name");
        if (nameInput instanceof HTMLInputElement) {
          nameInput.focus();
          nameInput.select();
        }
        return;
      }
      const message =
        apiError.detail ??
        apiError.title ??
        "Création impossible — vérifiez les informations saisies.";
      toast.error(message);
    }
  }

  function handleTokenSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const token = tokenInput.trim();
    if (token.length === 0) return;

    setApiToken(token);
    setTokenInput("");
    if (authConfigured) {
      void archetypesQuery.refetch();
    } else {
      setAuthConfigured(true);
    }
  }

  return (
    <section
      aria-labelledby="new-agent-heading"
      className="flex flex-col gap-6"
    >
      <header>
        <h1
          id="new-agent-heading"
          className="text-3xl font-semibold tracking-tight"
        >
          Nouveau template d'agent
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Choisissez un archétype universel et nommez votre template. Vous
          configurerez ensuite son prompt, son modèle LLM et ses contrats.
        </p>
      </header>

      {needsToken && (
        <form
          onSubmit={handleTokenSubmit}
          className="max-w-lg rounded-lg border border-border bg-card p-5"
        >
          <h2 className="text-base font-semibold">Connexion à l'API</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Saisissez le jeton Bearer du MVP. Il reste uniquement en mémoire
            dans cet onglet et sera oublié au rechargement.
          </p>
          <div className="mt-4 flex gap-2">
            <label htmlFor="api-token" className="sr-only">
              Jeton API
            </label>
            <Input
              id="api-token"
              type="password"
              value={tokenInput}
              onChange={(event) => setTokenInput(event.target.value)}
              placeholder="Jeton API"
              autoComplete="off"
              aria-invalid={queryError?.status === 401}
            />
            <Button type="submit" disabled={tokenInput.trim().length === 0}>
              Se connecter
            </Button>
          </div>
          {queryError?.status === 401 && (
            <p className="mt-2 text-sm text-destructive">
              Jeton refusé. Vérifiez sa valeur puis réessayez.
            </p>
          )}
        </form>
      )}

      {authConfigured && !needsToken && archetypesQuery.isLoading && (
        <p className="text-sm text-muted-foreground">Chargement des archétypes…</p>
      )}
      {archetypesQuery.isError && queryError?.status !== 401 && (
        <p className="text-sm text-destructive">
          Impossible de charger les archétypes. Réessayez.
        </p>
      )}

      {archetypesQuery.data && (
        <form
          onSubmit={handleSubmit}
          className="grid grid-cols-1 gap-6 md:grid-cols-3"
        >
          <div className="md:col-span-2">
            <ArchetypeSelector
              archetypes={archetypesQuery.data}
              value={archetypeId}
              onChange={setArchetypeId}
              disabled={createMutation.isPending}
            />
          </div>

          <aside className="flex flex-col gap-6">
            <div className="flex flex-col gap-2">
              <label
                htmlFor="template-name"
                className="text-sm font-medium leading-none"
              >
                Nom du template
              </label>
              <Input
                id="template-name"
                type="text"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="Ex. : Code Producer"
                maxLength={MAX_NAME_LENGTH}
                disabled={createMutation.isPending}
                aria-describedby="template-name-help"
              />
              <p id="template-name-help" className="text-xs text-muted-foreground">
                1 à {MAX_NAME_LENGTH} caractères. Doit être unique pour la
                version 1.
              </p>
            </div>

            <Button type="submit" disabled={submitDisabled}>
              {createMutation.isPending ? "Création…" : "Créer le template"}
            </Button>

            <div className="rounded-lg border border-border bg-card p-4">
              <ArchetypePreview archetypeId={archetypeId} />
            </div>
          </aside>
        </form>
      )}
    </section>
  );
}
