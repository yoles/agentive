/**
 * Zod schemas — frontend mirror of backend Pydantic v2 schemas
 * (`backend/src/agentive_backend/features/m2_agent_registry/schemas.py`).
 *
 * Story 2.3 D-F closure : these schemas drive client-side validation
 * (Wizard step gates + Expert form `react-hook-form` + `zodResolver`).
 * The backend remains the **authority** ; any Zod/Pydantic mismatch is a
 * Zod bug to fix.
 *
 * Sub-schemas `WizardStep{N}Schema` are picks of the full request schema
 * used as gates for each Wizard step before "Suivant".
 */

import { z } from "zod";

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Atomic schemas — mirror backend Literal / Field constraints
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const LLMModelSchema = z.enum([
  "claude-3-5-sonnet-20241022",
  "claude-3-5-haiku-20241022",
  "gpt-4o",
  "gpt-4o-mini",
]);

export const ProviderIdSchema = z.enum(["anthropic", "openai"]);

export const LLMParamsSchema = z.object({
  temperature: z.number().min(0).max(2),
  max_tokens: z.number().int().min(1).max(200_000),
});

// B1 amend Story 2.2 — `core` permissive (default {} accepted), only
// validates dict shape. Runtime value validation defers to Story 4.x.
export const ContractDefinitionSchema = z.object({
  core: z.record(z.string(), z.unknown()).optional().default({}),
  extras: z.record(z.string(), z.unknown()).optional().default({}),
});

export const ErrorPolicySchema = z.object({
  on_timeout: z
    .enum(["retry_with_backoff", "fail_fast", "fallback_provider"])
    .default("retry_with_backoff"),
  max_retries: z.number().int().min(0).max(10).default(3),
  backoff_strategy: z.enum(["exponential", "linear", "constant"]).default("exponential"),
});

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Top-level UpdateTemplateRequest mirror — strict + non-empty refine
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const UpdateTemplateRequestSchema = z
  .object({
    system_prompt: z.string().min(1).max(50_000).optional(),
    input_contract: ContractDefinitionSchema.optional(),
    output_contract: ContractDefinitionSchema.optional(),
    llm_model: LLMModelSchema.optional(),
    llm_params: LLMParamsSchema.optional(),
    provider_chain: z
      .array(ProviderIdSchema)
      .min(1)
      .max(4)
      .refine((arr) => new Set(arr).size === arr.length, {
        message: "duplicate provider in chain",
      })
      .optional(),
    error_policy: ErrorPolicySchema.optional(),
  })
  .strict()
  .refine(
    (payload) =>
      Object.values(payload).some((v) => v !== undefined && v !== null),
    { message: "at least one field must be provided" },
  );

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Wizard step sub-schemas (gates for "Suivant" buttons)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Step 1 — Identité (read-only Sprint 1 ; gate trivially passes). */
export const WizardStep1Schema = z.object({}).strict();

/** Step 2 — Prompt. `system_prompt` peut être vide (= conserver l'archétype baseline). */
export const WizardStep2Schema = z.object({
  system_prompt: z.string().max(50_000).optional(),
});

/** Step 3 — Contrats. */
export const WizardStep3Schema = z.object({
  input_contract: ContractDefinitionSchema,
  output_contract: ContractDefinitionSchema,
});

/** Step 4 — LLM (modèle + params + provider chain). */
export const WizardStep4Schema = z.object({
  llm_model: LLMModelSchema,
  llm_params: LLMParamsSchema,
  provider_chain: z
    .array(ProviderIdSchema)
    .min(1)
    .max(4)
    .refine((arr) => new Set(arr).size === arr.length, {
      message: "duplicate provider in chain",
    }),
});

/** Step 5 — Politique d'erreur + récap final. */
export const WizardStep5Schema = z.object({
  error_policy: ErrorPolicySchema,
});

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Type exports (z.infer for downstream consumers if needed)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type UpdateTemplateRequestZod = z.infer<typeof UpdateTemplateRequestSchema>;
export type ErrorPolicyZod = z.infer<typeof ErrorPolicySchema>;
export type LLMParamsZod = z.infer<typeof LLMParamsSchema>;
export type ContractDefinitionZod = z.infer<typeof ContractDefinitionSchema>;
