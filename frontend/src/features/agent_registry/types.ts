/**
 * Domain types for the Agent Registry — Story 2.1.
 *
 * Mirror the backend Pydantic schemas in
 * `backend/src/agentive_backend/features/m2_agent_registry/schemas.py`.
 * When backend OpenAPI typegen lands (Story 1.1 stub `make gen-api-types`),
 * we'll regenerate these.
 */

export type ContractSkeleton = {
  core: Record<string, unknown>;
  extras: Record<string, unknown>;
};

export type ArchetypeSummary = {
  id: string;
  display_name: string;
  icon_name: string;
  description: string;
  default_role: string;
};

export type ArchetypeDetail = ArchetypeSummary & {
  prompt_base: string;
  input_contract: ContractSkeleton;
  output_contract: ContractSkeleton;
};

export type CreateTemplateRequest = {
  archetype: string;
  name: string;
};

export type CreateTemplateResponse = {
  template_id: string;
  name: string;
  archetype: string;
  version: number;
  created_at: string; // ISO 8601
};

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Story 2.2 — Template detail + update
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Whitelist Sprint 1 — sera enrichie via OpenAPI typegen quand `make gen-api-types` existera. */
export type LLMModel =
  | "claude-3-5-sonnet-20241022"
  | "claude-3-5-haiku-20241022"
  | "gpt-4o"
  | "gpt-4o-mini";

export type ProviderId = "anthropic" | "openai";

export type LLMParams = {
  temperature: number;
  max_tokens: number;
};

export type ContractDefinition = {
  core: Record<string, unknown>;
  extras: Record<string, unknown>;
};

export type ErrorPolicy = {
  on_timeout: "retry_with_backoff" | "fail_fast" | "fallback_provider";
  max_retries: number;
  backoff_strategy: "exponential" | "linear" | "constant";
};

export type TemplateDetail = {
  template_id: string;
  name: string;
  archetype: string;
  version: number;
  config: Record<string, unknown>;
  created_at: string;
};

export type UpdateTemplateRequest = {
  system_prompt?: string;
  input_contract?: ContractDefinition;
  output_contract?: ContractDefinition;
  llm_model?: LLMModel;
  llm_params?: LLMParams;
  provider_chain?: ProviderId[];
  error_policy?: ErrorPolicy;
};

export type UpdateTemplateResponse = {
  template_id: string;
  name: string;
  archetype: string;
  version: number;
  config: Record<string, unknown>;
  updated_at: string;
};
