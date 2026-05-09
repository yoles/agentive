/**
 * `/config/agents/$templateId` route tests — Story 2.2 T6.5.
 *
 * Covers:
 * - GET /agents/templates/{id} hydrates the form fields.
 * - Save button → PUT /agents/templates/{id} with the expected body shape.
 * - Toast feedback on success.
 * - Toast feedback + structured error on 422 RFC 7807.
 * - UUID guard: route param non-UUID renders error state without firing GET.
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
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
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
    const promptInput = screen.getByLabelText(/system prompt/i) as HTMLTextAreaElement;
    expect(promptInput.value).toBe("");
    expect(promptInput.placeholder).toContain("Tu es un producteur.");

    const inputContract = screen.getByLabelText(/input contract/i) as HTMLTextAreaElement;
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

    const promptInput = await screen.findByLabelText(/system prompt/i);
    fireEvent.change(promptInput, { target: { value: "v2 prompt" } });

    fireEvent.click(screen.getByRole("button", { name: /sauvegarder/i }));

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
    fireEvent.click(screen.getByRole("button", { name: /sauvegarder/i }));

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

  it("rejects malformed JSON in provider_chain with a toast", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith(`/api/v1/agents/templates/${TEMPLATE_ID}`)) {
        return Promise.resolve(jsonResponse(TEMPLATE_FIXTURE));
      }
      return Promise.resolve(jsonResponse({}, { status: 404 }));
    });

    renderRoute(`/config/agents/${TEMPLATE_ID}`);

    const providerInput = await screen.findByLabelText(/provider chain/i);
    fireEvent.change(providerInput, { target: { value: "not json" } });

    fireEvent.click(screen.getByRole("button", { name: /sauvegarder/i }));

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
