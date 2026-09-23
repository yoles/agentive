/**
 * Template form helpers — Story 2.3 T1 extract.
 *
 * Pure logic (no React, no DOM). Consumed by `TemplateWizardForm` and
 * `TemplateExpertForm` to guarantee both modes produce a byte-identical
 * payload for the PUT mutation. Originally inline in `$templateId.tsx`
 * (Story 2.2) ; promoted to a module here so the two presentation modes
 * can share the parsing/merging logic.
 */

import type {
  ContractDefinition,
  ErrorPolicy,
  LLMModel,
  LLMParams,
  ProviderId,
  UpdateTemplateRequest,
} from "./types";

export const LLM_MODELS: readonly LLMModel[] = [
  "claude-3-5-sonnet-20241022",
  "claude-3-5-haiku-20241022",
  "gpt-4o",
  "gpt-4o-mini",
] as const;

export const ON_TIMEOUT_OPTIONS = [
  "retry_with_backoff",
  "fail_fast",
  "fallback_provider",
] as const;
export const BACKOFF_OPTIONS = ["exponential", "linear", "constant"] as const;

export const DEFAULT_LLM_PARAMS: LLMParams = { temperature: 0.7, max_tokens: 4096 };
export const DEFAULT_ERROR_POLICY: ErrorPolicy = {
  on_timeout: "retry_with_backoff",
  max_retries: 3,
  backoff_strategy: "exponential",
};
export const DEFAULT_PROVIDER_CHAIN: ProviderId[] = ["anthropic"];
export const EMPTY_CONTRACT: ContractDefinition = { core: {}, extras: {} };

export type FormState = {
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

export type BuildPayloadResult =
  | { ok: true; payload: UpdateTemplateRequest }
  | {
      ok: false;
      field: "provider_chain" | "input_contract" | "output_contract";
      message: string;
    };

function pickString(record: Record<string, unknown>, key: string, fallback: string): string {
  const value = record[key];
  return typeof value === "string" ? value : fallback;
}

function pickNumber(record: Record<string, unknown>, key: string, fallback: number): number {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

// P-32 (CR 2026-05-10) — defensive shape guards : un template legacy peut
// stocker `input_contract = null` (en plus de `undefined`) ou même un type
// inattendu. On retombe sur EMPTY_CONTRACT plutôt que de propager `null`
// dans `JSON.stringify(null) = "null"` qui crashe `parseContract`.
function pickObject<T>(value: unknown, fallback: T): T {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as T)
    : fallback;
}
function pickArray<T>(value: unknown, fallback: T[]): T[] {
  return Array.isArray(value) ? (value as T[]) : fallback;
}

export function buildInitialForm(config: Record<string, unknown>): FormState {
  const llmParams = pickObject<LLMParams>(config.llm_params, DEFAULT_LLM_PARAMS);
  const errorPolicy = pickObject<ErrorPolicy>(config.error_policy, DEFAULT_ERROR_POLICY);
  const providerChain = pickArray<ProviderId>(config.provider_chain, DEFAULT_PROVIDER_CHAIN);
  const inputContract = pickObject<ContractDefinition>(config.input_contract, EMPTY_CONTRACT);
  const outputContract = pickObject<ContractDefinition>(config.output_contract, EMPTY_CONTRACT);

  // P-01/P-02 Story 2.2 — system_prompt is NOT pre-filled from the
  // archetype prompt_base ; the placeholder displays the archetype
  // baseline as a hint instead. An empty system_prompt at submit time
  // means "keep the archetype default" (the backend skips bump if absent).
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

const ALLOWED_PROVIDERS = new Set<string>(["anthropic", "openai"]);

/** P-09/P-10 Story 2.2 + P-09 Story 2.3 (CR 2026-05-10) — parse + validate
 * each JSON field separately with a field-specific error message. Validates
 * SHAPE post-parse AND value-domain (whitelist providers, dedup, length 1..4)
 * pour mirroir Pydantic 1:1 et éviter le round-trip 422 backend.
 */
export function parseProviderChain(raw: string): ProviderId[] | string {
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
    return 'doit être un tableau JSON non vide de strings (ex : ["anthropic", "openai"])';
  }
  if (parsed.length > 4) {
    return "max 4 providers dans la chaîne";
  }
  const unknown = parsed.find((p) => !ALLOWED_PROVIDERS.has(p));
  if (unknown !== undefined) {
    return `provider inconnu : "${unknown}" (autorisés : anthropic, openai)`;
  }
  if (new Set(parsed).size !== parsed.length) {
    return "doublons interdits dans provider_chain";
  }
  return parsed as ProviderId[];
}

export function parseContract(raw: string): ContractDefinition | string {
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
  // `extras` is optional backend-side (default {}). Accept its absence here too.
  return parsed as ContractDefinition;
}

export function buildPayload(form: FormState): BuildPayloadResult {
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
  // P-01 Story 2.2 — Only include `system_prompt` if non-blank. Backend
  // `min_length=1` rejects `""` with 422 ; the service skips bump if the
  // value matches the existing config["system_prompt"] (P-01 backend).
  if (form.system_prompt.trim().length > 0) {
    payload.system_prompt = form.system_prompt;
  }
  return { ok: true, payload };
}
