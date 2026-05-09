/**
 * `focusFirstInvalidField` — Story 2.3 T1.2 extract from Story 2.2 P-11 fix.
 *
 * DOM helper isolated from `templateForm.ts` because it touches
 * `document.getElementById` (not pure logic). Reused by both the Wizard
 * and Expert forms when the server returns RFC 7807 with detailed
 * `errors[].loc[1]` field paths.
 */

const FIELD_TO_INPUT_ID: Record<string, string> = {
  system_prompt: "tpl-system-prompt",
  llm_model: "tpl-llm-model",
  llm_params: "tpl-temperature", // sub-field non distinct ici — temperature en premier
  provider_chain: "tpl-provider-chain",
  input_contract: "tpl-input-contract",
  output_contract: "tpl-output-contract",
  error_policy: "tpl-on-timeout",
};

/** Extracts `errors[].loc[1]` from the RFC 7807 body and focuses the
 * matching input. Falls back on the system_prompt textarea if no match.
 */
export function focusFirstInvalidField(apiError: {
  errors?: Array<{ loc?: unknown }>;
}): void {
  const errors = apiError.errors ?? [];
  for (const err of errors) {
    const loc = Array.isArray(err.loc) ? err.loc : [];
    const field = typeof loc[1] === "string" ? loc[1] : null;
    if (field && FIELD_TO_INPUT_ID[field]) {
      const el = document.getElementById(FIELD_TO_INPUT_ID[field]);
      if (el instanceof HTMLElement) {
        el.focus();
        return;
      }
    }
  }
  // Fallback: focus the system_prompt textarea (most-edited field).
  const fallback = document.getElementById("tpl-system-prompt");
  if (fallback instanceof HTMLTextAreaElement) {
    fallback.focus();
  }
}
