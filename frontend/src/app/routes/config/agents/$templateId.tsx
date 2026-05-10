/**
 * `/config/agents/$templateId` — agent template configuration page (Story 2.3 host).
 *
 * Story 2.3 réécrit l'ancienne page Story 2.2 (formulaire flat 12 fields)
 * en un host minimal qui :
 *   1. Charge le template via `useTemplate(id)` (UUID guard, staleTime 30s)
 *   2. Maintient un `formState` partagé entre Wizard et Expert (AC4 — switch
 *      préserve les modifications en cours)
 *   3. Route vers `<TemplateWizardForm>` ou `<TemplateExpertForm>` selon
 *      `useModeStore.mode` (persisté localStorage via Zustand)
 *
 * Le `handleSubmit` partagé applique le pattern Story 2.2 :
 *   - P-12 guard `if (updateMutation.isPending) return`
 *   - `buildPayload(formState)` → discriminated union OK / { field, message }
 *   - Sur succès : toast + invalidation queryKeys (déjà géré par useUpdateTemplate)
 *   - Sur erreur : toast (RFC 7807 detail) + `focusFirstInvalidField`
 */

import { createFileRoute, Link } from "@tanstack/react-router";
import { useRef, useState } from "react";
import { toast } from "sonner";
import {
  buildInitialForm,
  buildPayload,
  ConfigModeToggle,
  focusFirstInvalidField,
  type FormState,
  TemplateExpertForm,
  TemplateWizardForm,
  useModeStore,
  useTemplate,
  useUpdateTemplate,
  type WizardHandle,
} from "@/features/agent_registry";
import type { ApiError } from "@/shared/api/client";

export const Route = createFileRoute("/config/agents/$templateId")({
  component: AgentTemplateDetail,
});

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function AgentTemplateDetail() {
  const { templateId } = Route.useParams();
  const isValidUuid = UUID_RE.test(templateId);

  const templateQuery = useTemplate(isValidUuid ? templateId : null);
  const updateMutation = useUpdateTemplate(templateId);

  const mode = useModeStore((s) => s.mode);
  const hasHydrated = useModeStore((s) => s.hasHydrated);

  // P-05/P-06 (CR 2026-05-10) — imperative handle to navigate the Wizard
  // (back-to-step on save error, reset on success). Null when mode=expert
  // (Wizard is unmounted) — guard with optional chaining at call sites.
  const wizardRef = useRef<WizardHandle>(null);

  // FormState shared between Wizard and Expert — switching modes preserves
  // edits (AC4). "Derived state during render" pattern (Story 2.2 P-14)
  // hydrates from `templateQuery.data` without a `useEffect` (avoids
  // `react-hooks/set-state-in-effect`).
  const [hydratedFromId, setHydratedFromId] = useState<string | null>(null);
  const [formState, setFormState] = useState<FormState | null>(null);
  if (
    templateQuery.data &&
    (hydratedFromId !== templateQuery.data.template_id || formState === null)
  ) {
    setHydratedFromId(templateQuery.data.template_id);
    setFormState(buildInitialForm(templateQuery.data.config));
  }

  if (!isValidUuid) {
    return (
      <section className="flex flex-col gap-4">
        <h1 className="text-2xl font-semibold">Identifiant invalide</h1>
        <p className="text-sm text-muted-foreground">
          Le paramètre fourni n'est pas un UUID valide.
        </p>
        <Link to="/config" className="text-primary underline">
          Retour à la configuration
        </Link>
      </section>
    );
  }

  if (templateQuery.isLoading || formState === null) {
    return <p className="text-sm text-muted-foreground">Chargement du template…</p>;
  }
  if (templateQuery.isError) {
    return (
      <section className="flex flex-col gap-4">
        <h1 className="text-2xl font-semibold">Template introuvable</h1>
        <p className="text-sm text-muted-foreground">
          Le template demandé n'existe pas (ou a été supprimé).
        </p>
        <Link to="/config" className="text-primary underline">
          Retour à la configuration
        </Link>
      </section>
    );
  }

  const template = templateQuery.data;
  if (!template) {
    return <p className="text-sm text-muted-foreground">Chargement du template…</p>;
  }

  async function handleSubmit() {
    if (formState === null) return;
    // P-12 guard — bloque double-submit synchrone.
    if (updateMutation.isPending) return;

    const result = buildPayload(formState);
    if (!result.ok) {
      const fieldLabel: Record<typeof result.field, string> = {
        provider_chain: "Provider chain",
        input_contract: "Input contract",
        output_contract: "Output contract",
      };
      toast.error(`${fieldLabel[result.field]} : ${result.message}`);
      // P-05 (CR 2026-05-10) — navigate the Wizard to the failing field
      // BEFORE focusing, otherwise the input is not in the DOM (Wizard
      // shows one step at a time). Defer focus to next frame so the
      // navigation has rendered.
      if (mode === "wizard") {
        wizardRef.current?.goToStepForField(result.field);
      }
      const inputId = {
        provider_chain: "tpl-provider-chain",
        input_contract: "tpl-input-contract",
        output_contract: "tpl-output-contract",
      }[result.field];
      requestAnimationFrame(() => {
        const el = document.getElementById(inputId);
        if (el instanceof HTMLElement) el.focus();
      });
      return;
    }

    try {
      const response = await updateMutation.mutateAsync(result.payload);
      toast.success(`Template mis à jour (v${response.version})`);
      // P-06 (CR 2026-05-10) — reset FormState from the response config
      // (server is authoritative — the new prompt version, version bump,
      // etc.) and reset the Wizard to step 1 with no completed steps.
      setFormState(buildInitialForm(response.config));
      wizardRef.current?.reset();
    } catch (err) {
      const apiError = err as Partial<ApiError> & {
        errors?: Array<{ loc?: unknown }>;
      };
      const message =
        apiError.detail ??
        apiError.title ??
        "Mise à jour impossible — vérifiez les valeurs saisies.";
      toast.error(message);
      // P-05 (CR 2026-05-10) — navigate to the offending field's step BEFORE
      // focusFirstInvalidField, so the focus target exists in the DOM.
      if (mode === "wizard") {
        const firstError = apiError.errors?.[0];
        const loc = Array.isArray(firstError?.loc) ? firstError.loc : [];
        const firstField = typeof loc[1] === "string" ? loc[1] : null;
        if (firstField) wizardRef.current?.goToStepForField(firstField);
      }
      requestAnimationFrame(() => focusFirstInvalidField(apiError));
    }
  }

  return (
    <section
      aria-labelledby="agent-config-heading"
      className="container mx-auto flex max-w-3xl flex-col gap-6 py-6"
    >
      <header className="flex items-center justify-between gap-4">
        <div>
          <h1 id="agent-config-heading" className="text-3xl font-semibold tracking-tight">
            Configuration de l'agent
          </h1>
          <p className="mt-1 text-xs text-muted-foreground">
            {template.name} · archétype {template.archetype} · version actuelle{" "}
            {template.version}
          </p>
        </div>
        <ConfigModeToggle />
      </header>

      {hasHydrated && mode === "wizard" && (
        <TemplateWizardForm
          ref={wizardRef}
          template={template}
          formState={formState}
          onFormStateChange={setFormState}
          onSubmit={handleSubmit}
          isPending={updateMutation.isPending}
        />
      )}
      {hasHydrated && mode === "expert" && (
        <TemplateExpertForm
          template={template}
          formState={formState}
          onFormStateChange={setFormState}
          onSubmit={handleSubmit}
          isPending={updateMutation.isPending}
        />
      )}
      {!hasHydrated && (
        <p className="text-sm text-muted-foreground">Chargement de l'interface…</p>
      )}
    </section>
  );
}
