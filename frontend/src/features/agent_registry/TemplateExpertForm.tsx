/**
 * `TemplateExpertForm` — Story 2.3 AC3.
 *
 * Mode "Expert" — tous les 12 champs visibles, regroupés en 5 accordéons
 * empilés (Identité / Prompt / Contrats / LLM / Politique d'erreur). Tous
 * les accordéons sont OUVERTS au montage (`type="multiple"` +
 * `defaultValue=[...]`) — l'utilisateur peut replier librement, l'état
 * n'est pas persisté Sprint 1.
 *
 * Validation : pas de Zod side dans Sprint 1 — les inputs sont contrôlés
 * (pattern Story 2.2) et la validation backend reste finale (RFC 7807 →
 * toast + focus via `focusFirstInvalidField`). La couche Zod (T2) est
 * disponible pour les Wizard step gates ; côté Expert, le user soumet
 * tout en bloc et on ne le bloque pas par étape.
 */

import { Save, X } from "lucide-react";
import { Button } from "@/shared/components/ui/button";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/shared/components/ui/accordion";
import { Input } from "@/shared/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/components/ui/select";
import { Textarea } from "@/shared/components/ui/textarea";
import {
  BACKOFF_OPTIONS,
  buildInitialForm,
  type FormState,
  LLM_MODELS,
  ON_TIMEOUT_OPTIONS,
} from "./templateForm";
import type { TemplateDetail } from "./types";

const ALL_SECTIONS = ["identity", "prompt", "contracts", "llm", "error_policy"];

const LABEL_CLASS = "text-sm font-medium";

type Props = {
  template: TemplateDetail;
  formState: FormState;
  onFormStateChange: (next: FormState) => void;
  onSubmit: () => Promise<void>;
  isPending: boolean;
};

export function TemplateExpertForm({
  template,
  formState,
  onFormStateChange,
  onSubmit,
  isPending,
}: Props) {
  function patch(partial: Partial<FormState>) {
    onFormStateChange({ ...formState, ...partial });
  }

  function handleCancel() {
    onFormStateChange(buildInitialForm(template.config));
  }

  async function handleSave(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isPending) return;
    await onSubmit();
  }

  const promptPlaceholder =
    typeof template.config.prompt_base === "string"
      ? `Laisser vide pour utiliser le prompt de l'archétype :\n\n${template.config.prompt_base}`
      : "Saisir un system prompt personnalisé";

  return (
    <form
      onSubmit={handleSave}
      className="flex flex-col gap-5"
      data-testid="template-expert-form"
    >
      <Accordion type="multiple" defaultValue={ALL_SECTIONS} className="w-full">
        {/* ─── Identité ─── */}
        <AccordionItem value="identity">
          <AccordionTrigger>Identité</AccordionTrigger>
          <AccordionContent>
            <div className="flex flex-col gap-2 pt-2">
              <label htmlFor="tpl-name" className={LABEL_CLASS}>
                Nom du template
              </label>
              <Input id="tpl-name" type="text" value={template.name} readOnly />
              <p className="text-xs text-muted-foreground">
                Renommer un template arrive avec la Story 2.4 (distinction template/instance).
              </p>
              <p className="text-xs text-muted-foreground">
                Archétype : <strong>{template.archetype}</strong> · version actuelle{" "}
                <strong>v{template.version}</strong>
              </p>
            </div>
          </AccordionContent>
        </AccordionItem>

        {/* ─── Prompt ─── */}
        <AccordionItem value="prompt">
          <AccordionTrigger>System prompt</AccordionTrigger>
          <AccordionContent>
            <div className="flex flex-col gap-2 pt-2">
              <label htmlFor="tpl-system-prompt" className={LABEL_CLASS}>
                Prompt
              </label>
              <Textarea
                id="tpl-system-prompt"
                rows={12}
                value={formState.system_prompt}
                onChange={(e) => patch({ system_prompt: e.target.value })}
                placeholder={promptPlaceholder}
                className="font-mono text-sm"
                maxLength={50_000}
              />
              <p className="text-xs text-muted-foreground">
                Modifier ce champ crée une nouvelle version (versioning prompts). Laisser vide
                pour conserver le prompt de l'archétype.
              </p>
            </div>
          </AccordionContent>
        </AccordionItem>

        {/* ─── Contrats ─── */}
        <AccordionItem value="contracts">
          <AccordionTrigger>Contrats élastiques</AccordionTrigger>
          <AccordionContent>
            <div className="flex flex-col gap-4 pt-2">
              <div className="flex flex-col gap-2">
                <label htmlFor="tpl-input-contract" className={LABEL_CLASS}>
                  Input contract (JSON)
                </label>
                <Textarea
                  id="tpl-input-contract"
                  rows={6}
                  value={formState.input_contract_raw}
                  onChange={(e) => patch({ input_contract_raw: e.target.value })}
                  className="font-mono text-xs"
                />
              </div>
              <div className="flex flex-col gap-2">
                <label htmlFor="tpl-output-contract" className={LABEL_CLASS}>
                  Output contract (JSON)
                </label>
                <Textarea
                  id="tpl-output-contract"
                  rows={6}
                  value={formState.output_contract_raw}
                  onChange={(e) => patch({ output_contract_raw: e.target.value })}
                  className="font-mono text-xs"
                />
              </div>
            </div>
          </AccordionContent>
        </AccordionItem>

        {/* ─── LLM ─── */}
        <AccordionItem value="llm">
          <AccordionTrigger>Modèle LLM &amp; provider chain</AccordionTrigger>
          <AccordionContent>
            <div className="flex flex-col gap-4 pt-2">
              <div className="flex flex-col gap-2">
                <label htmlFor="tpl-llm-model" className={LABEL_CLASS}>
                  Modèle LLM
                </label>
                <Select
                  value={formState.llm_model}
                  onValueChange={(v) =>
                    patch({ llm_model: v as FormState["llm_model"] })
                  }
                >
                  <SelectTrigger id="tpl-llm-model" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {LLM_MODELS.map((m) => (
                      <SelectItem key={m} value={m}>
                        {m}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="flex flex-col gap-2">
                  <label htmlFor="tpl-temperature" className={LABEL_CLASS}>
                    Temperature
                  </label>
                  <Input
                    id="tpl-temperature"
                    type="number"
                    step="0.1"
                    min={0}
                    max={2}
                    value={formState.temperature}
                    onChange={(e) => {
                      const v = e.target.valueAsNumber;
                      if (Number.isFinite(v)) patch({ temperature: v });
                    }}
                  />
                </div>
                <div className="flex flex-col gap-2">
                  <label htmlFor="tpl-max-tokens" className={LABEL_CLASS}>
                    Max tokens
                  </label>
                  <Input
                    id="tpl-max-tokens"
                    type="number"
                    min={1}
                    max={200_000}
                    value={formState.max_tokens}
                    onChange={(e) => {
                      const v = e.target.valueAsNumber;
                      if (Number.isFinite(v)) patch({ max_tokens: v });
                    }}
                  />
                </div>
              </div>
              <div className="flex flex-col gap-2">
                <label htmlFor="tpl-provider-chain" className={LABEL_CLASS}>
                  Provider chain (JSON)
                </label>
                <Textarea
                  id="tpl-provider-chain"
                  rows={2}
                  value={formState.provider_chain_raw}
                  onChange={(e) => patch({ provider_chain_raw: e.target.value })}
                  className="font-mono text-sm"
                />
                <p className="text-xs text-muted-foreground">
                  Tableau JSON, ex&nbsp;: <code>["anthropic", "openai"]</code>. Le fallback runtime
                  arrive avec la Story 4.6.
                </p>
              </div>
            </div>
          </AccordionContent>
        </AccordionItem>

        {/* ─── Politique d'erreur ─── */}
        <AccordionItem value="error_policy">
          <AccordionTrigger>Politique d'erreur</AccordionTrigger>
          <AccordionContent>
            <div className="grid grid-cols-1 gap-3 pt-2 md:grid-cols-3">
              <div className="flex flex-col gap-2">
                <label htmlFor="tpl-on-timeout" className="text-xs font-medium">
                  On timeout
                </label>
                <Select
                  value={formState.on_timeout}
                  onValueChange={(v) =>
                    patch({ on_timeout: v as FormState["on_timeout"] })
                  }
                >
                  <SelectTrigger id="tpl-on-timeout" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {ON_TIMEOUT_OPTIONS.map((o) => (
                      <SelectItem key={o} value={o}>
                        {o}
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
                  value={formState.max_retries}
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
                  value={formState.backoff_strategy}
                  onValueChange={(v) =>
                    patch({ backoff_strategy: v as FormState["backoff_strategy"] })
                  }
                >
                  <SelectTrigger id="tpl-backoff" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {BACKOFF_OPTIONS.map((o) => (
                      <SelectItem key={o} value={o}>
                        {o}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>
            <p className="mt-3 text-xs text-muted-foreground">
              Le dispatcher actif (qui APPLIQUE le retry au runtime) arrive avec la Story 4.6.
            </p>
          </AccordionContent>
        </AccordionItem>
      </Accordion>

      <div className="flex items-center gap-3">
        <Button type="submit" disabled={isPending} data-testid="expert-save-button">
          <Save className="mr-1 size-4" />
          {isPending ? "Sauvegarde…" : "Sauvegarder"}
        </Button>
        <Button
          type="button"
          variant="outline"
          onClick={handleCancel}
          disabled={isPending}
        >
          <X className="mr-1 size-4" />
          Annuler
        </Button>
      </div>
    </form>
  );
}
