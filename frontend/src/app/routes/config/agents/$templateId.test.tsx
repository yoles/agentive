/**
 * `/config/agents/$templateId` route tests — Story 2.2 T6.5 + Story 2.3 T6.2.
 *
 * Story 2.3 — Tests existants Story 2.2 adaptés pour la nouvelle structure
 * Wizard/Expert : on force le mode `expert` au beforeEach pour préserver
 * les assertions sur les inputs visibles. Un test dédié AC4 vérifie la
 * préservation du `FormState` lors d'un switch mid-edit.
 *
 * Covers:
 * - GET /agents/templates/{id} hydrates the form fields (Expert mode).
 * - Save button → PUT /agents/templates/{id} with the expected body shape.
 * - Toast feedback on success.
 * - Toast feedback + structured error on 422 RFC 7807.
 * - UUID guard: route param non-UUID renders error state without firing GET.
 * - AC4 — switch Wizard ↔ Expert preserves the `FormState`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import {
  createMemoryHistory,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { Providers } from "@/app/providers";
import { routeTree } from "@/app/routeTree.gen";
import { useModeStore } from "@/features/agent_registry";

const TEMPLATE_ID = "11111111-2222-3333-4444-555555555555";

const TEMPLATE_FIXTURE = {
  template_id: TEMPLATE_ID,
  name: "Code Producer",
  archetype: "producteur",
  version: 1,
  config: {
    prompt_base: "Tu es un producteur.",
    role: "producer",
    input_contract: { core: { brief: "string" }, extras: {} },
    output_contract: { core: { artifact: "string" }, extras: {} },
  },
  created_at: new Date().toISOString(),
};

function jsonResponse(body: unknown, init: ResponseInit = { status: 200 }) {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: {
      "Content-Type":
        init.status && init.status >= 400
          ? "application/problem+json"
          : "application/json",
      ...(init.headers ?? {}),
    },
  });
}

function renderRoute(path: string) {
  const router = createRouter({
    routeTree,
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  return render(
    <Providers>
      <RouterProvider router={router} />
    </Providers>,
  );
}

describe("/config/agents/$templateId", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    // Story 2.3 — force le mode Expert (tous les champs visibles) pour les
    // tests existants Story 2.2 ; le test AC4 réinitialise au mode Wizard.
    useModeStore.setState({ mode: "expert", hasHydrated: true });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.removeItem("agentive.agent-config-mode");
  });

  it("hydrates the form from GET /agents/templates/{id}", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith(`/api/v1/agents/templates/${TEMPLATE_ID}`)) {
        return Promise.resolve(jsonResponse(TEMPLATE_FIXTURE));
      }
      return Promise.resolve(jsonResponse({}, { status: 404 }));
    });

    renderRoute(`/config/agents/${TEMPLATE_ID}`);

    // Header reveals after fetch resolves.
    await screen.findByRole("heading", { name: /configuration de l'agent/i });

    // P-01/P-02 fix (Story 2.2 review 2026-05-09) — `system_prompt` n'est plus
    // hydraté depuis `config.prompt_base` ; le textarea reste vide et le
    // placeholder affiche le prompt archétype à titre indicatif.
    // Story 2.3 — le label du textarea est "Prompt" (pas "System prompt", qui
    // est le titre de l'accordéon). On cible précisément le textarea via id.
    const promptInput = document.getElementById("tpl-system-prompt") as HTMLTextAreaElement;
    expect(promptInput).not.toBeNull();
    expect(promptInput.value).toBe("");
    expect(promptInput.placeholder).toContain("Tu es un producteur.");

    const inputContract = document.getElementById("tpl-input-contract") as HTMLTextAreaElement;
    expect(inputContract.value).toContain("\"brief\"");
  });

  it("submits PUT /agents/templates/{id} on save and toasts success", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.endsWith(`/api/v1/agents/templates/${TEMPLATE_ID}`) && method === "GET") {
        return Promise.resolve(jsonResponse(TEMPLATE_FIXTURE));
      }
      if (url.endsWith(`/api/v1/agents/templates/${TEMPLATE_ID}`) && method === "PUT") {
        return Promise.resolve(
          jsonResponse({
            ...TEMPLATE_FIXTURE,
            version: 2,
            config: { ...TEMPLATE_FIXTURE.config, system_prompt: "v2 prompt" },
            updated_at: new Date().toISOString(),
          }),
        );
      }
      return Promise.resolve(jsonResponse({}, { status: 404 }));
    });

    renderRoute(`/config/agents/${TEMPLATE_ID}`);

    // Wait page mounted then access the textarea by id (more specific than
    // the label text which collides with the accordion trigger heading).
    await screen.findByRole("heading", { name: /configuration de l'agent/i });
    const promptInput = document.getElementById("tpl-system-prompt") as HTMLTextAreaElement;
    fireEvent.change(promptInput, { target: { value: "v2 prompt" } });

    fireEvent.click(screen.getByTestId("expert-save-button"));

    await waitFor(() => {
      const putCall = fetchMock.mock.calls.find(([, init]) => {
        const i = init as RequestInit | undefined;
        return (i?.method ?? "GET").toUpperCase() === "PUT";
      });
      expect(putCall).toBeDefined();
      const [, putInit] = putCall as [unknown, RequestInit];
      const body = JSON.parse(putInit.body as string);
      expect(body.system_prompt).toBe("v2 prompt");
      expect(body.llm_model).toBe("claude-3-5-sonnet-20241022");
      expect(body.llm_params).toEqual({ temperature: 0.7, max_tokens: 4096 });
    });
  });

  it("toasts an error and keeps the form when PUT returns 422", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.endsWith(`/api/v1/agents/templates/${TEMPLATE_ID}`) && method === "GET") {
        return Promise.resolve(jsonResponse(TEMPLATE_FIXTURE));
      }
      if (url.endsWith(`/api/v1/agents/templates/${TEMPLATE_ID}`) && method === "PUT") {
        return Promise.resolve(
          jsonResponse(
            {
              type: "/errors/validation",
              title: "Validation failed",
              status: 422,
              detail: "system_prompt must not be blank",
            },
            { status: 422 },
          ),
        );
      }
      return Promise.resolve(jsonResponse({}, { status: 404 }));
    });

    renderRoute(`/config/agents/${TEMPLATE_ID}`);

    await screen.findByRole("heading", { name: /configuration de l'agent/i });
    fireEvent.click(screen.getByTestId("expert-save-button"));

    // Form remains rendered (we did not redirect away).
    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: /configuration de l'agent/i }),
      ).toBeInTheDocument();
    });
  });

  it("renders an error state and skips fetch when route param is not a UUID", async () => {
    fetchMock.mockImplementation(() => {
      return Promise.resolve(jsonResponse({}, { status: 404 }));
    });

    renderRoute("/config/agents/not-a-uuid");

    await screen.findByText(/identifiant invalide/i);
    // GET to the templates endpoint must NOT have been issued.
    const templatesCalls = fetchMock.mock.calls.filter(([input]) => {
      const url = typeof input === "string" ? input : (input as URL | Request).toString();
      return url.includes("/api/v1/agents/templates/");
    });
    expect(templatesCalls).toHaveLength(0);
  });

  it("AC4 — switching Wizard ↔ Expert preserves the in-flight FormState", async () => {
    // Start in Wizard mode, advance to step 2 (Prompt), edit the textarea,
    // then switch to Expert and verify the same value is visible there.
    useModeStore.setState({ mode: "wizard", hasHydrated: true });
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith(`/api/v1/agents/templates/${TEMPLATE_ID}`)) {
        return Promise.resolve(jsonResponse(TEMPLATE_FIXTURE));
      }
      return Promise.resolve(jsonResponse({}, { status: 404 }));
    });

    renderRoute(`/config/agents/${TEMPLATE_ID}`);

    // Wait Wizard step 1 mounted, then click "Suivant" to reach step 2.
    await screen.findByTestId("template-wizard-form");
    fireEvent.click(screen.getByRole("button", { name: /suivant/i }));

    // Step 2 — Story 2.3 P-04 (CR 2026-05-10) : data-testid scopés par mode
    // évitent l'ambigüité transitoire pendant le switch Wizard ↔ Expert
    // (les deux formulaires partagent l'`id` HTML "tpl-system-prompt" pour
    // l'accessibilité `htmlFor`, mais les tests passent par data-testid).
    await screen.findByTestId("template-wizard-form");
    const promptTextareaWizard = screen.getByTestId(
      "wizard-tpl-system-prompt",
    ) as HTMLTextAreaElement;
    fireEvent.change(promptTextareaWizard, {
      target: { value: "preserved across switch" },
    });

    // Switch to Expert mode via the ConfigModeToggle.
    fireEvent.click(screen.getByRole("radio", { name: /mode expert/i }));

    // The expert form mounts ; the system_prompt textarea must reflect
    // the edit made in the Wizard step.
    await screen.findByTestId("template-expert-form");
    const expertPromptInput = screen.getByTestId(
      "expert-tpl-system-prompt",
    ) as HTMLTextAreaElement;
    expect(expertPromptInput.value).toBe("preserved across switch");

    // Symétrie AC4 — on reste en Expert, on édite à nouveau, on revient
    // en Wizard, l'édition Expert doit être visible côté Wizard step 2.
    fireEvent.change(expertPromptInput, {
      target: { value: "edited in expert then back" },
    });
    fireEvent.click(screen.getByRole("radio", { name: /mode wizard/i }));
    await screen.findByTestId("template-wizard-form");
    // Naviguer step 1 → step 2 pour voir le textarea prompt.
    fireEvent.click(screen.getByRole("button", { name: /suivant/i }));
    const wizardPromptAgain = screen.getByTestId(
      "wizard-tpl-system-prompt",
    ) as HTMLTextAreaElement;
    expect(wizardPromptAgain.value).toBe("edited in expert then back");
  });

  it("rejects malformed JSON in provider_chain with a toast", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith(`/api/v1/agents/templates/${TEMPLATE_ID}`)) {
        return Promise.resolve(jsonResponse(TEMPLATE_FIXTURE));
      }
      return Promise.resolve(jsonResponse({}, { status: 404 }));
    });

    renderRoute(`/config/agents/${TEMPLATE_ID}`);

    await screen.findByRole("heading", { name: /configuration de l'agent/i });
    const providerInput = document.getElementById("tpl-provider-chain") as HTMLTextAreaElement;
    fireEvent.change(providerInput, { target: { value: "not json" } });

    fireEvent.click(screen.getByTestId("expert-save-button"));

    // No PUT should have been issued.
    await waitFor(() => {
      const putCall = fetchMock.mock.calls.find(([, init]) => {
        const i = init as RequestInit | undefined;
        return (i?.method ?? "GET").toUpperCase() === "PUT";
      });
      expect(putCall).toBeUndefined();
    });
  });
});
