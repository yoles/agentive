/**
 * `/config/agents/$templateId` — agent template configuration page (Story 2.2 AC5).
 *
 * Mode "Expert" only — flat form, no accordion / no wizard step gates
 * (those land in Story 2.3 along with the wizard/expert toggle UX-DR16
 * and the Zod-mirrored validation). Story 2.2 ships a minimal multi-field
 * form so the operator can configure every backend-supported field.
 *
 * D4 fix (Story 2.1 tech-debt) — `useTemplate` guards `enabled` with the
 * UUID regex; if the route param is malformed (e.g. someone navigates to
 * `/config/agents/abc`), no fetch fires and the page renders an error state
 * with a redirect link.
 */

import { createFileRoute, Link } from "@tanstack/react-router";
import { useState } from "react";
import { toast } from "sonner";
import {
  type ContractDefinition,
  type ErrorPolicy,
  type LLMModel,
  type LLMParams,
  type ProviderId,
  type UpdateTemplateRequest,
  useTemplate,
  useUpdateTemplate,
} from "@/features/agent_registry";
import type { ApiError } from "@/shared/api/client";
import { Button } from "@/shared/components/ui/button";
import { Input } from "@/shared/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/components/ui/select";
import { Textarea } from "@/shared/components/ui/textarea";

export const Route = createFileRoute("/config/agents/$templateId")({
  component: AgentTemplateDetail,
});

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const LLM_MODELS: readonly LLMModel[] = [
  "claude-3-5-sonnet-20241022",
  "claude-3-5-haiku-20241022",
  "gpt-4o",
  "gpt-4o-mini",
] as const;

const ON_TIMEOUT_OPTIONS = ["retry_with_backoff", "fail_fast", "fallback_provider"] as const;
const BACKOFF_OPTIONS = ["exponential", "linear", "constant"] as const;

const DEFAULT_LLM_PARAMS: LLMParams = { temperature: 0.7, max_tokens: 4096 };
const DEFAULT_ERROR_POLICY: ErrorPolicy = {
  on_timeout: "retry_with_backoff",
  max_retries: 3,
  backoff_strategy: "exponential",
};
const DEFAULT_PROVIDER_CHAIN: ProviderId[] = ["anthropic"];
const EMPTY_CONTRACT: ContractDefinition = { core: {}, extras: {} };

type FormState = {
  system_prompt: string;
  llm_model: LLMModel;
  temperature: number;
  max_tokens: number;
  provider_chain_raw: string; // JSON string
  input_contract_raw: string;
  output_contract_raw: string;
  on_timeout: ErrorPolicy["on_timeout"];
  max_retries: number;
  backoff_strategy: ErrorPolicy["backoff_strategy"];
};

function pickString(record: Record<string, unknown>, key: string, fallback: string): string {
  const value = record[key];
  return typeof value === "string" ? value : fallback;
}

function pickNumber(record: Record<string, unknown>, key: string, fallback: number): number {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function buildInitialForm(config: Record<string, unknown>): FormState {
  const llmParams = (config.llm_params ?? DEFAULT_LLM_PARAMS) as LLMParams;
  const errorPolicy = (config.error_policy ?? DEFAULT_ERROR_POLICY) as ErrorPolicy;
  const providerChain = (config.provider_chain ?? DEFAULT_PROVIDER_CHAIN) as ProviderId[];
  const inputContract = (config.input_contract ?? EMPTY_CONTRACT) as ContractDefinition;
  const outputContract = (config.output_contract ?? EMPTY_CONTRACT) as ContractDefinition;

  // P-01/P-02 fix — NE PAS copier `prompt_base` (skeleton archétype) dans le
  // form state du `system_prompt`. Sans ça, la default UX (Save sans toucher
  // le textarea) persiste l'archétype skeleton comme prompt utilisateur ET
  // bump la version. Le textarea reste vide ; un placeholder côté JSX
  // affichera le `prompt_base` à titre indicatif.
  return {
    system_prompt: pickString(config, "system_prompt", ""),
    llm_model: (config.llm_model as LLMModel | undefined) ?? "claude-3-5-sonnet-20241022",
    temperature: pickNumber(llmParams as unknown as Record<string, unknown>, "temperature", 0.7),
    max_tokens: pickNumber(llmParams as unknown as Record<string, unknown>, "max_tokens", 4096),
    provider_chain_raw: JSON.stringify(providerChain),
    input_contract_raw: JSON.stringify(inputContract, null, 2),
    output_contract_raw: JSON.stringify(outputContract, null, 2),
    on_timeout: errorPolicy.on_timeout ?? "retry_with_backoff",
    max_retries: errorPolicy.max_retries ?? 3,
    backoff_strategy: errorPolicy.backoff_strategy ?? "exponential",
  };
}

type BuildPayloadResult =
  | { ok: true; payload: UpdateTemplateRequest }
  | { ok: false; field: "provider_chain" | "input_contract" | "output_contract"; message: string };

/** P-09/P-10 fix — parse + valide chaque champ JSON séparément avec un
 * message dédié au champ (au lieu d'un blob générique). Valide aussi la
 * SHAPE post-parse (Array.isArray + every typeof === "string" pour
 * provider_chain ; objet avec `core` + `extras` pour les contrats).
 */
function parseProviderChain(raw: string): ProviderId[] | string {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    return `JSON invalide : ${(e as Error).message}`;
  }
  if (
    !Array.isArray(parsed) ||
    parsed.length === 0 ||
    !parsed.every((x): x is string => typeof x === "string")
  ) {
    return "doit être un tableau JSON non vide de strings (ex : [\"anthropic\", \"openai\"])";
  }
  return parsed as ProviderId[];
}

function parseContract(raw: string): ContractDefinition | string {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    return `JSON invalide : ${(e as Error).message}`;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return "doit être un objet JSON avec les clés `core` et `extras`";
  }
  const obj = parsed as Record<string, unknown>;
  if (typeof obj.core !== "object" || obj.core === null || Array.isArray(obj.core)) {
    return "le champ `core` doit être un objet";
  }
  // `extras` est optionnel côté backend (default {}) — on accepte l'absence.
  return parsed as ContractDefinition;
}

function buildPayload(form: FormState): BuildPayloadResult {
  const providerChain = parseProviderChain(form.provider_chain_raw);
  if (typeof providerChain === "string") {
    return { ok: false, field: "provider_chain", message: providerChain };
  }
  const inputContract = parseContract(form.input_contract_raw);
  if (typeof inputContract === "string") {
    return { ok: false, field: "input_contract", message: inputContract };
  }
  const outputContract = parseContract(form.output_contract_raw);
  if (typeof outputContract === "string") {
    return { ok: false, field: "output_contract", message: outputContract };
  }

  const payload: UpdateTemplateRequest = {
    llm_model: form.llm_model,
    llm_params: {
      temperature: form.temperature,
      max_tokens: form.max_tokens,
    },
    provider_chain: providerChain,
    input_contract: inputContract,
    output_contract: outputContract,
    error_policy: {
      on_timeout: form.on_timeout,
      max_retries: form.max_retries,
      backoff_strategy: form.backoff_strategy,
    },
  };
  // P-01 fix — N'inclure `system_prompt` QUE s'il a un contenu non-blanc.
  // Backend `min_length=1` rejette `""` en 422 ; et le service skippe le
  // bump version si la valeur est identique à l'existante (P-01 backend).
  if (form.system_prompt.trim().length > 0) {
    payload.system_prompt = form.system_prompt;
  }
  return { ok: true, payload };
}

/** P-11 fix — extrait `errors[].loc[1]` du body RFC 7807 et focus l'input
 * correspondant. Mapping `loc[1]` → input id ; fallback sur le textarea
 * `system_prompt` si le champ ne matche aucun input connu.
 */
function focusFirstInvalidField(apiError: { errors?: Array<{ loc?: unknown }> }): void {
  const fieldToInputId: Record<string, string> = {
    system_prompt: "tpl-system-prompt",
    llm_model: "tpl-llm-model",
    llm_params: "tpl-temperature", // sub-field non distinct ici — temperature en premier
    provider_chain: "tpl-provider-chain",
    input_contract: "tpl-input-contract",
    output_contract: "tpl-output-contract",
    error_policy: "tpl-on-timeout",
  };
  const errors = apiError.errors ?? [];
  for (const err of errors) {
    const loc = Array.isArray(err.loc) ? err.loc : [];
    const field = typeof loc[1] === "string" ? loc[1] : null;
    if (field && fieldToInputId[field]) {
      const el = document.getElementById(fieldToInputId[field]);
      if (el instanceof HTMLElement) {
        el.focus();
        return;
      }
    }
  }
  // Fallback : focus le textarea du prompt (le champ le plus souvent édité).
  const fallback = document.getElementById("tpl-system-prompt");
  if (fallback instanceof HTMLTextAreaElement) {
    fallback.focus();
  }
}

export function AgentTemplateDetail() {
  const { templateId } = Route.useParams();
  const isValidUuid = UUID_RE.test(templateId);

  const templateQuery = useTemplate(isValidUuid ? templateId : null);
  const updateMutation = useUpdateTemplate(templateId);

  // "Derived state during render" pattern (https://react.dev/reference/react/useState
  // #storing-information-from-previous-renders) — React handles a setState call
  // mid-render by immediately re-rendering with the new state, no extra effect
  // needed. We track the template id we hydrated from so we re-build the form
  // if the route param changes (rare, but defensive).
  const [hydratedFromId, setHydratedFromId] = useState<string | null>(null);
  const [form, setForm] = useState<FormState | null>(null);

  if (
    templateQuery.data &&
    (hydratedFromId !== templateQuery.data.template_id || form === null)
  ) {
    setHydratedFromId(templateQuery.data.template_id);
    setForm(buildInitialForm(templateQuery.data.config));
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

  if (templateQuery.isLoading || form === null) {
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

  // P-14 fix — destructure narrow au lieu de `templateQuery.data!`.
  // Si `data` est unexpectedly null après les guards précédents, on
  // tombe sur un loading state plutôt qu'un crash runtime.
  const template = templateQuery.data;
  if (!template) {
    return <p className="text-sm text-muted-foreground">Chargement du template…</p>;
  }

  function patch(partial: Partial<FormState>) {
    setForm((prev) => (prev ? { ...prev, ...partial } : prev));
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (form === null) return;
    // P-12 fix — guard synchrone contre le double-submit (Enter rapide,
    // double-click). `disabled={isPending}` sur le bouton ne s'applique
    // qu'au render suivant, donc une fenêtre de race existe sans ce check.
    if (updateMutation.isPending) return;

    const result = buildPayload(form);
    if (!result.ok) {
      const fieldLabel: Record<typeof result.field, string> = {
        provider_chain: "Provider chain",
        input_contract: "Input contract",
        output_contract: "Output contract",
      };
      toast.error(`${fieldLabel[result.field]} : ${result.message}`);
      const inputId = {
        provider_chain: "tpl-provider-chain",
        input_contract: "tpl-input-contract",
        output_contract: "tpl-output-contract",
      }[result.field];
      const el = document.getElementById(inputId);
      if (el instanceof HTMLTextAreaElement) {
        el.focus();
      }
      return;
    }

    try {
      const response = await updateMutation.mutateAsync(result.payload);
      toast.success(`Template mis à jour (v${response.version})`);
    } catch (err) {
      const apiError = err as Partial<ApiError> & { errors?: Array<{ loc?: unknown }> };
      const message =
        apiError.detail ??
        apiError.title ??
        "Mise à jour impossible — vérifiez les valeurs saisies.";
      toast.error(message);
      // P-11 fix — focus le PREMIER champ invalide depuis errors[].loc[1]
      // (RFC 7807 detailed errors), au lieu d'un focus systématique sur
      // `system_prompt` (UX-trompeur quand l'erreur est ailleurs).
      focusFirstInvalidField(apiError);
    }
  }

  function handleCancel() {
    if (template) {
      setForm(buildInitialForm(template.config));
      toast.success("Modifications annulées");
    }
  }

  return (
    <section
      aria-labelledby="agent-config-heading"
      className="container mx-auto flex max-w-2xl flex-col gap-6 py-6"
    >
      <header>
        <h1 id="agent-config-heading" className="text-3xl font-semibold tracking-tight">
          Configuration de l'agent
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Édition mode <strong>expert</strong> — tous les champs sont visibles. Le mode wizard
          arrive avec la Story 2.3.
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          {template.name} · archétype {template.archetype} · version actuelle {template.version}
        </p>
      </header>

      <form onSubmit={handleSubmit} className="flex flex-col gap-5">
        {/* Name (read-only) */}
        <div className="flex flex-col gap-2">
          <label htmlFor="tpl-name" className="text-sm font-medium">
            Nom du template
          </label>
          <Input id="tpl-name" type="text" value={template.name} readOnly />
          <p className="text-xs text-muted-foreground">
            Renommer un template arrive avec la Story 2.4 (distinction template/instance).
          </p>
        </div>

        {/* System prompt — P-01/P-02 fix : placeholder montre `prompt_base`
            archetype, mais la valeur n'est PAS pré-remplie ; un champ vide
            au save n'envoie pas `system_prompt` au backend. */}
        <div className="flex flex-col gap-2">
          <label htmlFor="tpl-system-prompt" className="text-sm font-medium">
            System prompt
          </label>
          <Textarea
            id="tpl-system-prompt"
            rows={12}
            value={form.system_prompt}
            onChange={(e) => patch({ system_prompt: e.target.value })}
            placeholder={
              typeof template.config.prompt_base === "string"
                ? `Laisser vide pour utiliser le prompt de l'archétype :\n\n${template.config.prompt_base}`
                : "Saisir un system prompt personnalisé"
            }
            className="font-mono text-sm"
            maxLength={50_000}
          />
          <p className="text-xs text-muted-foreground">
            Modifier ce champ crée une nouvelle version (versioning prompts). Laisser vide
            pour conserver le prompt de l'archétype.
          </p>
        </div>

        {/* LLM model */}
        <div className="flex flex-col gap-2">
          <label htmlFor="tpl-llm-model" className="text-sm font-medium">
            Modèle LLM
          </label>
          <Select
            value={form.llm_model}
            onValueChange={(value) => patch({ llm_model: value as LLMModel })}
          >
            <SelectTrigger id="tpl-llm-model" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {LLM_MODELS.map((model) => (
                <SelectItem key={model} value={model}>
                  {model}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        {/* LLM params */}
        <div className="grid grid-cols-2 gap-4">
          <div className="flex flex-col gap-2">
            <label htmlFor="tpl-temperature" className="text-sm font-medium">
              Temperature
            </label>
            <Input
              id="tpl-temperature"
              type="number"
              step="0.1"
              min={0}
              max={2}
              value={form.temperature}
              onChange={(e) => {
                // P-04 fix — `valueAsNumber` retourne NaN pour "" / "abc" /
                // formats invalides ; `Number(e.target.value)` coerce "" en 0
                // silencieusement et "1,5" (locale fr) en NaN. On n'écrit
                // l'état QUE si la valeur parse en number fini.
                const v = e.target.valueAsNumber;
                if (Number.isFinite(v)) patch({ temperature: v });
              }}
            />
          </div>
          <div className="flex flex-col gap-2">
            <label htmlFor="tpl-max-tokens" className="text-sm font-medium">
              Max tokens
            </label>
            <Input
              id="tpl-max-tokens"
              type="number"
              min={1}
              max={200_000}
              value={form.max_tokens}
              onChange={(e) => {
                const v = e.target.valueAsNumber;
                if (Number.isFinite(v)) patch({ max_tokens: v });
              }}
            />
          </div>
        </div>

        {/* Provider chain (JSON raw) */}
        <div className="flex flex-col gap-2">
          <label htmlFor="tpl-provider-chain" className="text-sm font-medium">
            Provider chain (JSON)
          </label>
          <Textarea
            id="tpl-provider-chain"
            rows={2}
            value={form.provider_chain_raw}
            onChange={(e) => patch({ provider_chain_raw: e.target.value })}
            className="font-mono text-sm"
          />
          <p className="text-xs text-muted-foreground">
            Tableau JSON, ex&nbsp;: <code>["anthropic", "openai"]</code>. Le fallback runtime
            arrive avec la Story 4.6.
          </p>
        </div>

        {/* Input contract */}
        <div className="flex flex-col gap-2">
          <label htmlFor="tpl-input-contract" className="text-sm font-medium">
            Input contract (JSON)
          </label>
          <Textarea
            id="tpl-input-contract"
            rows={6}
            value={form.input_contract_raw}
            onChange={(e) => patch({ input_contract_raw: e.target.value })}
            className="font-mono text-xs"
          />
        </div>

        {/* Output contract */}
        <div className="flex flex-col gap-2">
          <label htmlFor="tpl-output-contract" className="text-sm font-medium">
            Output contract (JSON)
          </label>
          <Textarea
            id="tpl-output-contract"
            rows={6}
            value={form.output_contract_raw}
            onChange={(e) => patch({ output_contract_raw: e.target.value })}
            className="font-mono text-xs"
          />
        </div>

        {/* Error policy */}
        <fieldset className="flex flex-col gap-3 rounded-lg border border-border p-4">
          <legend className="px-1 text-sm font-medium">Politique d'erreur</legend>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <div className="flex flex-col gap-2">
              <label htmlFor="tpl-on-timeout" className="text-xs font-medium">
                On timeout
              </label>
              <Select
                value={form.on_timeout}
                onValueChange={(v) =>
                  patch({ on_timeout: v as ErrorPolicy["on_timeout"] })
                }
              >
                <SelectTrigger id="tpl-on-timeout" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ON_TIMEOUT_OPTIONS.map((opt) => (
                    <SelectItem key={opt} value={opt}>
                      {opt}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex flex-col gap-2">
              <label htmlFor="tpl-max-retries" className="text-xs font-medium">
                Max retries
              </label>
              <Input
                id="tpl-max-retries"
                type="number"
                min={0}
                max={10}
                value={form.max_retries}
                onChange={(e) => {
                  const v = e.target.valueAsNumber;
                  if (Number.isFinite(v)) patch({ max_retries: v });
                }}
              />
            </div>
            <div className="flex flex-col gap-2">
              <label htmlFor="tpl-backoff" className="text-xs font-medium">
                Backoff
              </label>
              <Select
                value={form.backoff_strategy}
                onValueChange={(v) =>
                  patch({ backoff_strategy: v as ErrorPolicy["backoff_strategy"] })
                }
              >
                <SelectTrigger id="tpl-backoff" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {BACKOFF_OPTIONS.map((opt) => (
                    <SelectItem key={opt} value={opt}>
                      {opt}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            Le dispatcher actif (qui APPLIQUE le retry au runtime) arrive avec la Story 4.6.
          </p>
        </fieldset>

        <div className="flex items-center gap-3">
          <Button type="submit" disabled={updateMutation.isPending}>
            {updateMutation.isPending ? "Sauvegarde…" : "Sauvegarder"}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={handleCancel}
            disabled={updateMutation.isPending}
          >
            Annuler
          </Button>
        </div>
      </form>
    </section>
  );
}
