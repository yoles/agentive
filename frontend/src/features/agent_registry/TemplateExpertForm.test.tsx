/**
 * Tests — `TemplateExpertForm` (Story 2.3 T5.5).
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { TemplateExpertForm } from "./TemplateExpertForm";
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

function renderExpert(overrides: Partial<Parameters<typeof TemplateExpertForm>[0]> = {}) {
  const onSubmit = vi.fn().mockResolvedValue(undefined);
  const onFormStateChange = vi.fn();
  const formState = buildInitialForm(TEMPLATE.config);
  const utils = render(
    <TemplateExpertForm
      template={TEMPLATE}
      formState={formState}
      onFormStateChange={onFormStateChange}
      onSubmit={onSubmit}
      isPending={false}
      {...overrides}
    />,
  );
  return { ...utils, onSubmit, onFormStateChange };
}

describe("TemplateExpertForm", () => {
  afterEach(() => cleanup());

  it("renders all 5 accordion sections (Identité / Prompt / Contrats / LLM / Politique)", () => {
    renderExpert();
    // Accordion triggers are buttons with the section labels.
    expect(screen.getByRole("button", { name: /^Identité$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^System prompt$/i })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^Contrats élastiques$/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Modèle LLM/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^Politique d'erreur$/i }),
    ).toBeInTheDocument();

    // All sections open at mount (defaultValue=ALL_SECTIONS) → fields are visible.
    // `getByLabelText` returns the textarea (input control), not the label
    // element — the assertion below verifies the field is reachable from
    // its label, i.e. the accordion section is actually rendered open.
    expect(screen.getByLabelText(/^prompt$/i)).toBeInTheDocument();
  });

  it("submit triggers onSubmit (preserves Story 2.2 atomic submit pattern)", async () => {
    const { onSubmit } = renderExpert();
    fireEvent.click(screen.getByTestId("expert-save-button"));
    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledTimes(1);
    });
  });

  it("Annuler restores buildInitialForm via onFormStateChange", () => {
    const { onFormStateChange } = renderExpert();
    fireEvent.click(screen.getByRole("button", { name: /annuler/i }));
    expect(onFormStateChange).toHaveBeenCalled();
    // P-21 (CR 2026-05-10) — onFormStateChange est désormais un updater
    // functional. handleCancel passe `() => buildInitialForm(template.config)`
    // qui ignore prev. On invoque l'updater avec un état arbitraire pour
    // récupérer le résultat.
    const updater = onFormStateChange.mock.calls[0][0] as (
      prev: ReturnType<typeof buildInitialForm>,
    ) => ReturnType<typeof buildInitialForm>;
    const restored = updater(buildInitialForm(TEMPLATE.config));
    // Initial form should have system_prompt empty (P-01 fix preserved).
    expect(restored.system_prompt).toBe("");
  });
});
