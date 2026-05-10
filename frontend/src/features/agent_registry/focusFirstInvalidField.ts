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
  llm_params: "tpl-temperature", // fallback générique si loc[2] absent
  provider_chain: "tpl-provider-chain",
  input_contract: "tpl-input-contract",
  output_contract: "tpl-output-contract",
  error_policy: "tpl-on-timeout",
};

// P-22 (CR 2026-05-10) — disambiguation pour les schemas nested (llm_params,
// error_policy). Quand le backend renvoie `loc: ["body", "llm_params",
// "max_tokens"]`, on focus l'input `tpl-max-tokens` au lieu de retomber
// génériquement sur `tpl-temperature`.
const SUBFIELD_TO_INPUT_ID: Record<string, string> = {
  temperature: "tpl-temperature",
  max_tokens: "tpl-max-tokens",
  on_timeout: "tpl-on-timeout",
  max_retries: "tpl-max-retries",
  backoff_strategy: "tpl-backoff",
};

/** Extracts `errors[].loc[1]` (and `loc[2]` for nested schemas) from the
 * RFC 7807 body and focuses the matching input. Falls back on the
 * system_prompt textarea if no match.
 */
export function focusFirstInvalidField(apiError: {
  errors?: Array<{ loc?: unknown }>;
}): void {
  const errors = apiError.errors ?? [];
  for (const err of errors) {
    const loc = Array.isArray(err.loc) ? err.loc : [];
    const field = typeof loc[1] === "string" ? loc[1] : null;
    const subField = typeof loc[2] === "string" ? loc[2] : null;
    // P-22 (CR 2026-05-10) — prefer subfield resolution for nested errors.
    const inputId =
      (subField && SUBFIELD_TO_INPUT_ID[subField]) ||
      (field && FIELD_TO_INPUT_ID[field]) ||
      null;
    if (inputId) {
      const el = document.getElementById(inputId);
      if (el instanceof HTMLElement) {
        focusElement(el);
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

// P-34 (CR 2026-05-10) — Radix `<SelectTrigger>` est un `<button>` avec
// `data-slot="select-trigger"`. Un simple `.focus()` highlight le bouton
// mais n'ouvre pas le menu : l'utilisateur doit Espace/Entrée pour voir
// les options. On déclenche un `.click()` après focus pour les triggers
// (les autres types restent juste `.focus()`).
function focusElement(el: HTMLElement) {
  el.focus();
  if (el.tagName === "BUTTON" && el.dataset.slot === "select-trigger") {
    el.click();
  }
}
