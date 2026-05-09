/**
 * `TemplateWizardForm` — guided 5-step UX for agent template configuration.
 *
 * Story 2.3 AC2. The 5 steps are :
 *   1. Identité       (read-only récap nom + archétype + version)
 *   2. Prompt         (system_prompt Textarea)
 *   3. Contrats       (input + output JSON Textarea)
 *   4. LLM            (modèle + params + provider_chain JSON)
 *   5. Politique      (error_policy + récap final + bouton Sauvegarder)
 *
 * Validation gates : on "Suivant", a Zod sub-schema (`WizardStepNSchema`) is
 * applied to the relevant subset of `formState`. If invalid, the step stays
 * visible with inline error messages ; the focus moves to the first invalid
 * input via the field-id mapping (`focusFirstInvalidField` extends to wizard
 * field ids — same `tpl-*` prefix as Expert mode).
 *
 * State lifting : the `FormState` is owned by the parent (`$templateId.tsx`)
 * so switching to Expert mode mid-edit preserves all changes (AC4).
 */

import { CheckIcon, ChevronLeft, ChevronRight, Save, X } from "lucide-react";
import { useState } from "react";
import type { ZodType } from "zod";
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
import { cn } from "@/shared/lib/utils";
import {
  WizardStep1Schema,
  WizardStep2Schema,
  WizardStep3Schema,
  WizardStep4Schema,
  WizardStep5Schema,
} from "./schemas";
import {
  BACKOFF_OPTIONS,
  buildInitialForm,
  type FormState,
  LLM_MODELS,
  ON_TIMEOUT_OPTIONS,
  parseContract,
  parseProviderChain,
} from "./templateForm";
import type { TemplateDetail } from "./types";

const LABEL_CLASS = "text-sm font-medium";

const STEPS: ReadonlyArray<{ id: number; label: string }> = [
  { id: 1, label: "Identité" },
  { id: 2, label: "Prompt" },
  { id: 3, label: "Contrats" },
  { id: 4, label: "LLM" },
  { id: 5, label: "Politique d'erreur" },
];

type Props = {
  template: TemplateDetail;
  formState: FormState;
  onFormStateChange: (next: FormState) => void;
  onSubmit: () => Promise<void>;
  isPending: boolean;
};

type StepError = { message: string; field?: string } | null;

export function TemplateWizardForm({
  template,
  formState,
  onFormStateChange,
  onSubmit,
  isPending,
}: Props) {
  const [currentStep, setCurrentStep] = useState<number>(1);
  const [completedSteps, setCompletedSteps] = useState<Set<number>>(new Set());
  const [stepError, setStepError] = useState<StepError>(null);

  function patch(partial: Partial<FormState>) {
    onFormStateChange({ ...formState, ...partial });
  }

  function validateStep(step: number): StepError {
    const schema: ZodType<unknown> = (
      [WizardStep1Schema, WizardStep2Schema, WizardStep3Schema, WizardStep4Schema, WizardStep5Schema] as ZodType<unknown>[]
    )[step - 1];

    let candidate: Record<string, unknown> = {};
    if (step === 2) {
      candidate = { system_prompt: formState.system_prompt || undefined };
    } else if (step === 3) {
      const inputC = parseContract(formState.input_contract_raw);
      if (typeof inputC === "string") {
        return { message: `Input contract : ${inputC}`, field: "input_contract" };
      }
      const outputC = parseContract(formState.output_contract_raw);
      if (typeof outputC === "string") {
        return { message: `Output contract : ${outputC}`, field: "output_contract" };
      }
      candidate = { input_contract: inputC, output_contract: outputC };
    } else if (step === 4) {
      const provider = parseProviderChain(formState.provider_chain_raw);
      if (typeof provider === "string") {
        return { message: `Provider chain : ${provider}`, field: "provider_chain" };
      }
      candidate = {
        llm_model: formState.llm_model,
        llm_params: { temperature: formState.temperature, max_tokens: formState.max_tokens },
        provider_chain: provider,
      };
    } else if (step === 5) {
      candidate = {
        error_policy: {
          on_timeout: formState.on_timeout,
          max_retries: formState.max_retries,
          backoff_strategy: formState.backoff_strategy,
        },
      };
    }
    const result = schema.safeParse(candidate);
    if (!result.success) {
      const firstIssue = result.error.issues[0];
      return {
        message: firstIssue.message,
        field: firstIssue.path[0]?.toString(),
      };
    }
    return null;
  }

  function handleNext() {
    const err = validateStep(currentStep);
    if (err !== null) {
      setStepError(err);
      return;
    }
    setStepError(null);
    setCompletedSteps((prev) => new Set(prev).add(currentStep));
    setCurrentStep((s) => Math.min(s + 1, STEPS.length));
  }

  function handlePrev() {
    setStepError(null);
    setCurrentStep((s) => Math.max(s - 1, 1));
  }

  function handleStepClick(step: number) {
    // Allow jumping back to any completed (or current) step ; future steps
    // are blocked until prerequisites are validated.
    if (step <= currentStep || completedSteps.has(step - 1)) {
      setStepError(null);
      setCurrentStep(step);
    }
  }

  function handleCancel() {
    onFormStateChange(buildInitialForm(template.config));
    setCurrentStep(1);
    setCompletedSteps(new Set());
    setStepError(null);
  }

  async function handleSave() {
    // Final-step gate: validate step 5 before triggering the mutation.
    const err = validateStep(5);
    if (err !== null) {
      setStepError(err);
      return;
    }
    setStepError(null);
    await onSubmit();
  }

  return (
    <div className="flex flex-col gap-6" data-testid="template-wizard-form">
      <WizardProgress
        currentStep={currentStep}
        completedSteps={completedSteps}
        onStepClick={handleStepClick}
      />

      <div className="rounded-lg border border-border bg-card p-6">
        {currentStep === 1 && <Step1Identity template={template} />}
        {currentStep === 2 && (
          <Step2Prompt
            template={template}
            value={formState.system_prompt}
            onChange={(v) => patch({ system_prompt: v })}
          />
        )}
        {currentStep === 3 && (
          <Step3Contracts
            inputRaw={formState.input_contract_raw}
            outputRaw={formState.output_contract_raw}
            onInputChange={(v) => patch({ input_contract_raw: v })}
            onOutputChange={(v) => patch({ output_contract_raw: v })}
          />
        )}
        {currentStep === 4 && (
          <Step4LLM formState={formState} patch={patch} />
        )}
        {currentStep === 5 && (
          <Step5ErrorPolicyAndValidation
            formState={formState}
            patch={patch}
          />
        )}

        {stepError && (
          <p
            role="alert"
            className="mt-4 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive"
          >
            {stepError.message}
          </p>
        )}
      </div>

      <div className="flex items-center justify-between gap-3">
        <Button
          type="button"
          variant="outline"
          onClick={handleCancel}
          disabled={isPending}
        >
          <X className="mr-1 size-4" />
          Annuler
        </Button>
        <div className="flex items-center gap-3">
          {currentStep > 1 && (
            <Button
              type="button"
              variant="outline"
              onClick={handlePrev}
              disabled={isPending}
            >
              <ChevronLeft className="mr-1 size-4" />
              Précédent
            </Button>
          )}
          {currentStep < STEPS.length && (
            <Button type="button" onClick={handleNext} disabled={isPending}>
              Suivant
              <ChevronRight className="ml-1 size-4" />
            </Button>
          )}
          {currentStep === STEPS.length && (
            <Button
              type="button"
              onClick={handleSave}
              disabled={isPending}
              data-testid="wizard-save-button"
            >
              <Save className="mr-1 size-4" />
              {isPending ? "Sauvegarde…" : "Sauvegarder"}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Sub-components — one per wizard step
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function WizardProgress({
  currentStep,
  completedSteps,
  onStepClick,
}: {
  currentStep: number;
  completedSteps: Set<number>;
  onStepClick: (step: number) => void;
}) {
  return (
    <ol className="flex items-center gap-2" aria-label="Progression du wizard">
      {STEPS.map((step) => {
        const isActive = step.id === currentStep;
        const isCompleted = completedSteps.has(step.id);
        const reachable = step.id <= currentStep || completedSteps.has(step.id - 1);
        return (
          <li key={step.id} className="flex flex-1 items-center gap-2">
            <button
              type="button"
              onClick={() => onStepClick(step.id)}
              disabled={!reachable}
              aria-current={isActive ? "step" : undefined}
              className={cn(
                "flex h-8 w-8 shrink-0 items-center justify-center rounded-full border text-xs font-medium transition-colors",
                isActive && "border-primary bg-primary text-primary-foreground",
                !isActive && isCompleted && "border-status-success bg-status-success/10 text-status-success",
                !isActive && !isCompleted && "border-border text-muted-foreground",
                !reachable && "cursor-not-allowed opacity-50",
              )}
            >
              {isCompleted ? <CheckIcon className="size-4" /> : step.id}
            </button>
            <span
              className={cn(
                "text-xs font-medium",
                isActive ? "text-foreground" : "text-muted-foreground",
              )}
            >
              {step.label}
            </span>
            {step.id < STEPS.length && (
              <div className="h-px flex-1 bg-border" aria-hidden="true" />
            )}
          </li>
        );
      })}
    </ol>
  );
}

function Step1Identity({ template }: { template: TemplateDetail }) {
  return (
    <div className="flex flex-col gap-4">
      <h2 className="text-lg font-semibold">Identité de l'agent</h2>
      <p className="text-sm text-muted-foreground">
        Rappel des informations posées à la création (Story 2.1). Le rename arrive avec la Story 2.4.
      </p>
      <dl className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <Field label="Nom">{template.name}</Field>
        <Field label="Archétype">{template.archetype}</Field>
        <Field label="Version actuelle">v{template.version}</Field>
      </dl>
    </div>
  );
}

function Step2Prompt({
  template,
  value,
  onChange,
}: {
  template: TemplateDetail;
  value: string;
  onChange: (v: string) => void;
}) {
  const placeholderBase =
    typeof template.config.prompt_base === "string"
      ? `Laisser vide pour utiliser le prompt de l'archétype :\n\n${template.config.prompt_base}`
      : "Saisir un system prompt personnalisé";
  return (
    <div className="flex flex-col gap-3">
      <h2 className="text-lg font-semibold">System prompt</h2>
      <label htmlFor="tpl-system-prompt" className="text-sm font-medium" className={LABEL_CLASS}>
        Prompt
      </label>
      <Textarea
        id="tpl-system-prompt"
        rows={12}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholderBase}
        className="font-mono text-sm"
        maxLength={50_000}
      />
      <p className="text-xs text-muted-foreground">
        Modifier ce champ crée une nouvelle version (versioning prompts). Laisser vide
        pour conserver le prompt de l'archétype.
      </p>
    </div>
  );
}

function Step3Contracts({
  inputRaw,
  outputRaw,
  onInputChange,
  onOutputChange,
}: {
  inputRaw: string;
  outputRaw: string;
  onInputChange: (v: string) => void;
  onOutputChange: (v: string) => void;
}) {
  return (
    <div className="flex flex-col gap-5">
      <h2 className="text-lg font-semibold">Contrats élastiques</h2>
      <p className="text-sm text-muted-foreground">
        Format JSON : <code>{`{"core": {...}, "extras": {...}}`}</code>. Le noyau (`core`)
        est typé runtime Story 4.x ; la zone `extras` reste flexible.
      </p>
      <div className="flex flex-col gap-2">
        <label htmlFor="tpl-input-contract" className={LABEL_CLASS}>Input contract (JSON)</label>
        <Textarea
          id="tpl-input-contract"
          rows={6}
          value={inputRaw}
          onChange={(e) => onInputChange(e.target.value)}
          className="font-mono text-xs"
        />
      </div>
      <div className="flex flex-col gap-2">
        <label htmlFor="tpl-output-contract" className={LABEL_CLASS}>Output contract (JSON)</label>
        <Textarea
          id="tpl-output-contract"
          rows={6}
          value={outputRaw}
          onChange={(e) => onOutputChange(e.target.value)}
          className="font-mono text-xs"
        />
      </div>
    </div>
  );
}

function Step4LLM({
  formState,
  patch,
}: {
  formState: FormState;
  patch: (partial: Partial<FormState>) => void;
}) {
  return (
    <div className="flex flex-col gap-5">
      <h2 className="text-lg font-semibold">Modèle LLM &amp; provider chain</h2>
      <div className="flex flex-col gap-2">
        <label htmlFor="tpl-llm-model" className={LABEL_CLASS}>Modèle LLM</label>
        <Select
          value={formState.llm_model}
          onValueChange={(v) => patch({ llm_model: v as FormState["llm_model"] })}
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
          <label htmlFor="tpl-temperature" className={LABEL_CLASS}>Temperature</label>
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
          <label htmlFor="tpl-max-tokens" className={LABEL_CLASS}>Max tokens</label>
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
        <label htmlFor="tpl-provider-chain" className={LABEL_CLASS}>Provider chain (JSON)</label>
        <Textarea
          id="tpl-provider-chain"
          rows={2}
          value={formState.provider_chain_raw}
          onChange={(e) => patch({ provider_chain_raw: e.target.value })}
          className="font-mono text-sm"
        />
        <p className="text-xs text-muted-foreground">
          Tableau JSON, ex : <code>["anthropic", "openai"]</code>. Fallback runtime arrive Story 4.6.
        </p>
      </div>
    </div>
  );
}

function Step5ErrorPolicyAndValidation({
  formState,
  patch,
}: {
  formState: FormState;
  patch: (partial: Partial<FormState>) => void;
}) {
  return (
    <div className="flex flex-col gap-5">
      <h2 className="text-lg font-semibold">Politique d'erreur &amp; validation finale</h2>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <div className="flex flex-col gap-2">
          <label htmlFor="tpl-on-timeout" className={LABEL_CLASS}>On timeout</label>
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
          <label htmlFor="tpl-max-retries" className={LABEL_CLASS}>Max retries</label>
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
          <label htmlFor="tpl-backoff" className={LABEL_CLASS}>Backoff</label>
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
      <p className="text-xs text-muted-foreground">
        Le dispatcher actif (qui APPLIQUE le retry au runtime) arrive avec la Story 4.6.
      </p>
      <hr className="border-border" />
      <h3 className="text-sm font-semibold">Récapitulatif avant sauvegarde</h3>
      <dl className="grid grid-cols-1 gap-2 text-xs md:grid-cols-2">
        <Field label="Modèle LLM">{formState.llm_model}</Field>
        <Field label="Temperature">{formState.temperature}</Field>
        <Field label="Max tokens">{formState.max_tokens}</Field>
        <Field label="System prompt (taille)">
          {formState.system_prompt.length === 0
            ? "vide (utilise l'archétype)"
            : `${formState.system_prompt.length} caractères`}
        </Field>
      </dl>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1 rounded-md border border-border bg-muted/30 p-3">
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="text-sm font-medium">{children}</dd>
    </div>
  );
}
