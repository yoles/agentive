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
