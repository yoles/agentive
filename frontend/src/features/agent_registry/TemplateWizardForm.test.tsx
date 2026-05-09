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
    // Step 1 is read-only (passes trivially). Advance to step 4 then break the
    // provider_chain raw value to verify the gate stops the user.
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

    // Click Next 3 times to reach step 4 (LLM).
    fireEvent.click(screen.getByRole("button", { name: /suivant/i }));
    fireEvent.click(screen.getByRole("button", { name: /suivant/i }));
    fireEvent.click(screen.getByRole("button", { name: /suivant/i }));

    // Now we're on step 4 ; clicking Next should be blocked by Zod.
    fireEvent.click(screen.getByRole("button", { name: /suivant/i }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/Provider chain/i);
    });
    // We should still be on step 4 — the "Sauvegarder" button does not appear.
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
