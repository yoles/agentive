/**
 * Tests — `TemplateWizardForm` (Story 2.3 T4.6).
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { TemplateWizardForm } from "./TemplateWizardForm";
import { buildInitialForm } from "./templateForm";
import type { TemplateDetail } from "./types";

const TEMPLATE: TemplateDetail = {
  template_id: "11111111-2222-3333-4444-555555555555",
  name: "Code Producer",
  archetype: "producteur",
  version: 1,
  config: {
    prompt_base: "Tu es un Producteur.",
    role: "producer",
    input_contract: { core: { brief: "string" }, extras: {} },
    output_contract: { core: { artifact: "string" }, extras: {} },
  },
  created_at: new Date().toISOString(),
};

function renderWizard(overrides: Partial<Parameters<typeof TemplateWizardForm>[0]> = {}) {
  const onSubmit = vi.fn().mockResolvedValue(undefined);
  const onFormStateChange = vi.fn();
  const formState = buildInitialForm(TEMPLATE.config);
  const utils = render(
    <TemplateWizardForm
      template={TEMPLATE}
      formState={formState}
      onFormStateChange={onFormStateChange}
      onSubmit={onSubmit}
      isPending={false}
      {...overrides}
    />,
  );
  return { ...utils, onSubmit, onFormStateChange, formState };
}

describe("TemplateWizardForm", () => {
  afterEach(() => cleanup());

  it("blocks 'Suivant' when the step Zod validation fails (invalid provider_chain JSON)", async () => {
    // Story 2.3 P-08 (CR 2026-05-10) — assert intermediate state at every
    // step transition so the test fails frontally if a prior step
    // (Identité / Prompt / Contrats) starts rejecting silently. Without
    // these assertions the test would still pass for the wrong reason
    // (gate at step 1 or 2 instead of step 4).
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const onFormStateChange = vi.fn();
    const formState = {
      ...buildInitialForm(TEMPLATE.config),
      provider_chain_raw: "not json",
    };
    render(
      <TemplateWizardForm
        template={TEMPLATE}
        formState={formState}
        onFormStateChange={onFormStateChange}
        onSubmit={onSubmit}
        isPending={false}
      />,
    );

    // Step 1 — Identité (read-only). Click Next, expect Step 2 "System prompt".
    fireEvent.click(screen.getByRole("button", { name: /suivant/i }));
    expect(screen.getByRole("heading", { name: /system prompt/i })).toBeInTheDocument();

    // Step 2 — Prompt. Click Next, expect Step 3 "Contrats élastiques".
    fireEvent.click(screen.getByRole("button", { name: /suivant/i }));
    expect(screen.getByRole("heading", { name: /contrats élastiques/i })).toBeInTheDocument();

    // Step 3 — Contracts. Click Next, expect Step 4 "Modèle LLM & provider chain".
    fireEvent.click(screen.getByRole("button", { name: /suivant/i }));
    expect(screen.getByRole("heading", { name: /modèle llm/i })).toBeInTheDocument();

    // Step 4 — clicking Next must be blocked by Zod (provider_chain invalid).
    fireEvent.click(screen.getByRole("button", { name: /suivant/i }));
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/Provider chain/i);
    });
    // Still on step 4 — the Step 4 heading is present, the Save button is not.
    expect(screen.getByRole("heading", { name: /modèle llm/i })).toBeInTheDocument();
    expect(screen.queryByTestId("wizard-save-button")).not.toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("completes all 5 steps and triggers onSubmit on Save", async () => {
    const { onSubmit } = renderWizard();

    // Click Next 4 times to traverse steps 1 → 5.
    for (let i = 0; i < 4; i++) {
      fireEvent.click(screen.getByRole("button", { name: /suivant/i }));
    }

    // Step 5 reached — Save button should be visible.
    const saveButton = await screen.findByTestId("wizard-save-button");
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledTimes(1);
    });
  });
});
