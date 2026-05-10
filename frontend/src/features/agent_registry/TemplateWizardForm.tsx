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
import { forwardRef, useImperativeHandle, useState } from "react";
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
  /**
   * P-21 (CR 2026-05-10) — functional updater pour éviter le race où deux
   * `patch()` synchrones (autocomplete, IME composition, paste multi-champ)
   * snapshot le `formState` du même render et le second écrase le premier.
   * Pour un reset complet (Annuler / post-PUT succès), passer
   * `(_prev) => buildInitialForm(template.config)`.
   */
  onFormStateChange: (updater: (prev: FormState) => FormState) => void;
  onSubmit: () => Promise<void>;
  isPending: boolean;
};

type StepError = { message: string; field?: string } | null;

export type WizardHandle = {
  /**
   * Navigate to the wizard step that contains the given backend field.
   * Used by the parent host when an API error (RFC 7807 or buildPayload
   * pre-flight) targets a field that is NOT visible on the current step.
   * P-05 (CR 2026-05-10).
   */
  goToStepForField: (field: string) => void;
  /**
   * Reset wizard state to step 1 with no completed steps and no error.
   * Used by the parent host after a successful PUT (P-06 CR 2026-05-10).
   */
  reset: () => void;
};

// Map backend field names → wizard step. Used by both `goToStepForField`
// (parent → child) and `focusStepField` (child → DOM after a gate failure).
const FIELD_TO_STEP: Record<string, number> = {
  system_prompt: 2,
  input_contract: 3,
  output_contract: 3,
  llm_model: 4,
  llm_params: 4,
  temperature: 4,
  max_tokens: 4,
  provider_chain: 4,
  error_policy: 5,
  on_timeout: 5,
  max_retries: 5,
  backoff_strategy: 5,
};

const FIELD_TO_INPUT_ID: Record<string, string> = {
  system_prompt: "tpl-system-prompt",
  input_contract: "tpl-input-contract",
  output_contract: "tpl-output-contract",
  llm_model: "tpl-llm-model",
  llm_params: "tpl-temperature",
  temperature: "tpl-temperature",
  max_tokens: "tpl-max-tokens",
  provider_chain: "tpl-provider-chain",
  error_policy: "tpl-on-timeout",
  on_timeout: "tpl-on-timeout",
  max_retries: "tpl-max-retries",
  backoff_strategy: "tpl-backoff",
};

function focusStepField(field: string | undefined) {
  if (!field) return;
  const id = FIELD_TO_INPUT_ID[field];
  if (!id) return;
  // Defer to next frame so the DOM has settled if a navigation just happened.
  requestAnimationFrame(() => {
    const el = document.getElementById(id);
    if (el instanceof HTMLElement) el.focus();
  });
}

export const TemplateWizardForm = forwardRef<WizardHandle, Props>(function TemplateWizardForm(
  { template, formState, onFormStateChange, onSubmit, isPending },
  ref,
) {
  const [currentStep, setCurrentStep] = useState<number>(1);
  const [completedSteps, setCompletedSteps] = useState<Set<number>>(new Set());
  const [stepError, setStepError] = useState<StepError>(null);

  useImperativeHandle(
    ref,
    () => ({
      goToStepForField(field: string) {
        const target = FIELD_TO_STEP[field];
        if (target) {
          setCurrentStep(target);
          setStepError(null);
        }
      },
      reset() {
        setCurrentStep(1);
        setCompletedSteps(new Set());
        setStepError(null);
      },
    }),
    [],
  );

  function patch(partial: Partial<FormState>) {
    onFormStateChange((prev) => ({ ...prev, ...partial }));
  }

  function validateStep(step: number): StepError {
    // P-29 (CR 2026-05-10) — defensive : if STEPS is extended without
    // updating this dispatcher, a step outside [1..5] would index past
    // the schema array → safeParse on undefined → runtime crash. Better
    // to fail loud (return null = pass = no gate) than crash silently.
    if (step < 1 || step > 5) return null;
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
      // P-03 (CR 2026-05-10) — focus the first invalid field so the user
      // does not have to scroll/hunt. AC2 explicitly requires this behavior.
      focusStepField(err.field);
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
    onFormStateChange(() => buildInitialForm(template.config));
    setCurrentStep(1);
    setCompletedSteps(new Set());
    setStepError(null);
  }

  async function handleSave() {
    // P-02 (CR 2026-05-10) — re-validate ALL editable steps (2..5) before
    // triggering the mutation. Guards against bypass scenarios where the
    // user goes back to an earlier step, breaks a value, then jumps forward
    // via handleStepClick (which trusts completedSteps but does NOT
    // re-validate). On failure, navigate to the failing step + focus.
    for (let s = 2; s <= 5; s++) {
      const err = validateStep(s);
      if (err !== null) {
        setStepError(err);
        setCurrentStep(s);
        focusStepField(err.field);
        return;
      }
    }
    // P-23 (CR 2026-05-10) — clear stepError BEFORE calling onSubmit so the
    // banner does not linger after a failed-then-fixed save. If onSubmit
    // throws, the parent host handles the error toast + focus ; we just
    // re-surface a generic banner so the user knows something went wrong
    // even if the toast is dismissed.
    setStepError(null);
    try {
      await onSubmit();
    } catch {
      setStepError({
        message: "La sauvegarde a échoué — voir la notification d'erreur.",
      });
    }
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
            error={stepError?.field === "system_prompt" ? stepError.message : null}
          />
        )}
        {currentStep === 3 && (
          <Step3Contracts
            inputRaw={formState.input_contract_raw}
            outputRaw={formState.output_contract_raw}
            onInputChange={(v) => patch({ input_contract_raw: v })}
            onOutputChange={(v) => patch({ output_contract_raw: v })}
            inputError={
              stepError?.field === "input_contract" ? stepError.message : null
            }
            outputError={
              stepError?.field === "output_contract" ? stepError.message : null
            }
          />
        )}
        {currentStep === 4 && (
          <Step4LLM formState={formState} patch={patch} stepError={stepError} />
        )}
        {currentStep === 5 && (
          <Step5ErrorPolicyAndValidation
            formState={formState}
            patch={patch}
            stepError={stepError}
          />
        )}

        {/* P-17 (CR 2026-05-10) — global banner kept for messages whose
            `field` does not map to a known input id (defensive fallback) ;
            per-step components render an inline `<FormMessage>` under the
            relevant field when `stepError.field` matches. */}
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
});

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
              // P-35 (CR 2026-05-10) — `aria-disabled` au lieu de `disabled`
              // pour garder le bouton focusable au clavier (sinon Tab passe
              // par-dessus les étapes futures sans signal a11y), avec un
              // onClick guard + title explicite pour le screen reader.
              onClick={() => {
                if (reachable) onStepClick(step.id);
              }}
              aria-disabled={!reachable || undefined}
              aria-current={isActive ? "step" : undefined}
              title={
                !reachable
                  ? "Complétez les étapes précédentes pour débloquer"
                  : undefined
              }
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

function FormMessage({ children }: { children: React.ReactNode }) {
  // P-17 (CR 2026-05-10) — inline error message under a field. Pattern
  // shadcn `<FormMessage>` (non encore ajouté au repo) reproduit en `<p>`
  // text-destructive (cohérent Story 2.1 archetype creation form).
  // Pas de `role="alert"` ici : le banner global (live region) annonce
  // déjà le message au screen reader. Le per-field reste un visual cue.
  return <p className="text-xs text-destructive">{children}</p>;
}

function Step2Prompt({
  template,
  value,
  onChange,
  error,
}: {
  template: TemplateDetail;
  value: string;
  onChange: (v: string) => void;
  error: string | null;
}) {
  const placeholderBase =
    typeof template.config.prompt_base === "string"
      ? `Laisser vide pour utiliser le prompt de l'archétype :\n\n${template.config.prompt_base}`
      : "Saisir un system prompt personnalisé";
  return (
    <div className="flex flex-col gap-3">
      <h2 className="text-lg font-semibold">System prompt</h2>
      <label htmlFor="tpl-system-prompt" className={LABEL_CLASS}>
        Prompt
      </label>
      <Textarea
        id="tpl-system-prompt"
        data-testid="wizard-tpl-system-prompt"
        rows={12}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholderBase}
        className="font-mono text-sm"
        maxLength={50_000}
        aria-invalid={error !== null || undefined}
        aria-describedby={error !== null ? "tpl-system-prompt-error" : undefined}
      />
      {error && <span id="tpl-system-prompt-error"><FormMessage>{error}</FormMessage></span>}
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
  inputError,
  outputError,
}: {
  inputRaw: string;
  outputRaw: string;
  onInputChange: (v: string) => void;
  onOutputChange: (v: string) => void;
  inputError: string | null;
  outputError: string | null;
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
          aria-invalid={inputError !== null || undefined}
        />
        {inputError && <FormMessage>{inputError}</FormMessage>}
      </div>
      <div className="flex flex-col gap-2">
        <label htmlFor="tpl-output-contract" className={LABEL_CLASS}>Output contract (JSON)</label>
        <Textarea
          id="tpl-output-contract"
          rows={6}
          value={outputRaw}
          onChange={(e) => onOutputChange(e.target.value)}
          className="font-mono text-xs"
          aria-invalid={outputError !== null || undefined}
        />
        {outputError && <FormMessage>{outputError}</FormMessage>}
      </div>
    </div>
  );
}

function Step4LLM({
  formState,
  patch,
  stepError,
}: {
  formState: FormState;
  patch: (partial: Partial<FormState>) => void;
  stepError: StepError;
}) {
  // P-20 (CR 2026-05-10) — `value` displays the empty string when state is
  // NaN so the input visually mirrors the underlying state. Sans ce guard,
  // l'utilisateur efface le champ → state garde l'ancienne valeur, mismatch UI.
  const tempDisplay = Number.isFinite(formState.temperature)
    ? formState.temperature
    : "";
  const maxTokDisplay = Number.isFinite(formState.max_tokens)
    ? formState.max_tokens
    : "";
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
        {stepError?.field === "llm_model" && <FormMessage>{stepError.message}</FormMessage>}
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
            value={tempDisplay}
            onChange={(e) => {
              const v = e.target.valueAsNumber;
              if (Number.isFinite(v)) patch({ temperature: v });
            }}
            aria-invalid={stepError?.field === "temperature" || undefined}
          />
          {stepError?.field === "temperature" && (
            <FormMessage>{stepError.message}</FormMessage>
          )}
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor="tpl-max-tokens" className={LABEL_CLASS}>Max tokens</label>
          <Input
            id="tpl-max-tokens"
            type="number"
            min={1}
            max={200_000}
            value={maxTokDisplay}
            onChange={(e) => {
              const v = e.target.valueAsNumber;
              // P-33 (CR 2026-05-10) — client bounds guard (Pydantic int
              // >=1 <=200_000). Évite le round-trip 422 backend.
              if (Number.isFinite(v) && v >= 1 && v <= 200_000) {
                patch({ max_tokens: v });
              }
            }}
            aria-invalid={stepError?.field === "max_tokens" || undefined}
          />
          {stepError?.field === "max_tokens" && (
            <FormMessage>{stepError.message}</FormMessage>
          )}
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
          aria-invalid={stepError?.field === "provider_chain" || undefined}
        />
        {stepError?.field === "provider_chain" && (
          <FormMessage>{stepError.message}</FormMessage>
        )}
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
  stepError,
}: {
  formState: FormState;
  patch: (partial: Partial<FormState>) => void;
  stepError: StepError;
}) {
  // P-20 (CR 2026-05-10) — display empty for non-finite max_retries.
  const retriesDisplay = Number.isFinite(formState.max_retries)
    ? formState.max_retries
    : "";
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
          {stepError?.field === "on_timeout" && <FormMessage>{stepError.message}</FormMessage>}
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor="tpl-max-retries" className={LABEL_CLASS}>Max retries</label>
          <Input
            id="tpl-max-retries"
            type="number"
            min={0}
            max={10}
            step={1}
            value={retriesDisplay}
            onChange={(e) => {
              const v = e.target.valueAsNumber;
              // P-28 (CR 2026-05-10) — type=number accepte 0.5 ; on rejette
              // les non-entiers côté client (Pydantic int validation rejet
              // sinon avec 422 silencieux).
              if (Number.isInteger(v)) patch({ max_retries: v });
            }}
            aria-invalid={stepError?.field === "max_retries" || undefined}
          />
          {stepError?.field === "max_retries" && <FormMessage>{stepError.message}</FormMessage>}
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
          {stepError?.field === "backoff_strategy" && <FormMessage>{stepError.message}</FormMessage>}
        </div>
      </div>
      <p className="text-xs text-muted-foreground">
        Le dispatcher actif (qui APPLIQUE le retry au runtime) arrive avec la Story 4.6.
      </p>
      <hr className="border-border" />
      {/* P-13 (CR 2026-05-10) — récap COMPLET (12 champs) en lecture seule
          avant Sauvegarder. Décision intégrée #10 spec : "récap de tous les
          champs en lecture seule + bouton Sauvegarder". */}
      <h3 className="text-sm font-semibold">Récapitulatif avant sauvegarde</h3>
      <dl className="grid grid-cols-1 gap-2 text-xs md:grid-cols-2">
        <Field label="Modèle LLM">{formState.llm_model}</Field>
        <Field label="Temperature">{formState.temperature}</Field>
        <Field label="Max tokens">{formState.max_tokens}</Field>
        <Field label="System prompt (taille)">
          {/* P-36 (CR 2026-05-10) — defensive ?? '' au cas où un FormState
              corrompu se glisse (TS garantit string mais runtime peut
              recevoir undefined depuis hydration partielle). */}
          {(formState.system_prompt ?? "").length === 0
            ? "vide (utilise l'archétype)"
            : `${(formState.system_prompt ?? "").length} caractères`}
        </Field>
        <Field label="Provider chain">{formState.provider_chain_raw}</Field>
        <Field label="On timeout">{formState.on_timeout}</Field>
        <Field label="Max retries">{formState.max_retries}</Field>
        <Field label="Backoff">{formState.backoff_strategy}</Field>
        <Field label="Input contract (taille)">
          {formState.input_contract_raw.length} caractères
        </Field>
        <Field label="Output contract (taille)">
          {formState.output_contract_raw.length} caractères
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
