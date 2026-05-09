/**
 * Public surface — Agent Registry feature (Story 2.1).
 *
 * Same-feature internal imports go through this barrel to satisfy
 * `boundaries/dependencies` v6 (post-rétro Epic 1, 2026-05-08).
 */

export { ArchetypePreview } from "./ArchetypePreview";
export { ArchetypeSelector } from "./ArchetypeSelector";
export type { ArchetypeSelectorProps } from "./ArchetypeSelector";
export {
  useArchetypeDetail,
  useArchetypes,
  useCreateTemplate,
  useTemplate,
  useUpdateTemplate,
} from "./hooks";
export type {
  ArchetypeDetail,
  ArchetypeSummary,
  ContractDefinition,
  CreateTemplateRequest,
  CreateTemplateResponse,
  ErrorPolicy,
  LLMModel,
  LLMParams,
  ProviderId,
  TemplateDetail,
  UpdateTemplateRequest,
  UpdateTemplateResponse,
} from "./types";
