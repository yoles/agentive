/**
 * Public surface — Agent Registry feature (Story 2.1).
 *
 * Same-feature internal imports go through this barrel to satisfy
 * `boundaries/dependencies` v6 (post-rétro Epic 1, 2026-05-08).
 */

export { ArchetypePreview } from "./ArchetypePreview";
export { ArchetypeSelector } from "./ArchetypeSelector";
export type { ArchetypeSelectorProps } from "./ArchetypeSelector";
export { ConfigModeToggle } from "./ConfigModeToggle";
export { focusFirstInvalidField } from "./focusFirstInvalidField";
// Story 2.3 — config Wizard/Expert preference store.
export { useModeStore } from "./modeStore";
export type { ConfigMode } from "./modeStore";
// Story 2.3 — Wizard + Expert presentation modes for the template editor.
export { TemplateExpertForm } from "./TemplateExpertForm";
export { TemplateWizardForm } from "./TemplateWizardForm";
// Story 2.3 — Zod schemas miroir Pydantic backend.
export {
  ContractDefinitionSchema,
  ErrorPolicySchema,
  LLMModelSchema,
  LLMParamsSchema,
  ProviderIdSchema,
  UpdateTemplateRequestSchema,
  WizardStep1Schema,
  WizardStep2Schema,
  WizardStep3Schema,
  WizardStep4Schema,
  WizardStep5Schema,
} from "./schemas";
export {
  useArchetypeDetail,
  useArchetypes,
  useCreateTemplate,
  useTemplate,
  useUpdateTemplate,
} from "./hooks";
// Story 2.3 — extracted helpers + form constants shared between Wizard and Expert.
export {
  BACKOFF_OPTIONS,
  buildInitialForm,
  buildPayload,
  DEFAULT_ERROR_POLICY,
  DEFAULT_LLM_PARAMS,
  DEFAULT_PROVIDER_CHAIN,
  EMPTY_CONTRACT,
  LLM_MODELS,
  ON_TIMEOUT_OPTIONS,
  parseContract,
  parseProviderChain,
} from "./templateForm";
export type { BuildPayloadResult, FormState } from "./templateForm";
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
