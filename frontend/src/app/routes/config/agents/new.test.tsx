/**
 * `/config/agents/new` route tests — Story 2.1 T5.5.
 *
 * Covers:
 * - Submit button disabled until both archetype + name are valid.
 * - Successful submit triggers the create mutation, shows a toast, and
 *   redirects to `/config/agents/$templateId`.
 *
 * Mocks `fetch` so we exercise the real `apiFetch` + `useCreateTemplate`
 * pipeline without a live backend.
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

const FIXTURE_LIST = [
  {
    id: "producteur",
    display_name: "Producteur",
    icon_name: "wrench",
    description: "Génère le livrable.",
    default_role: "producer",
  },
  {
    id: "controleur",
    display_name: "Contrôleur",
    icon_name: "shield-check",
    description: "Review un livrable.",
    default_role: "controller",
  },
];

const FIXTURE_DETAIL = {
  id: "producteur",
  display_name: "Producteur",
  icon_name: "wrench",
  description: "Génère le livrable.",
  default_role: "producer",
  prompt_base: "Tu es un producteur.",
  input_contract: { core: { brief: "string" }, extras: {} },
  output_contract: { core: { artifact: "string" }, extras: {} },
};

function jsonResponse(body: unknown, init: ResponseInit = { status: 200 }) {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
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

describe("/config/agents/new", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("disables submit until both archetype and name are provided", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/api/v1/agents/archetypes")) {
        return Promise.resolve(jsonResponse(FIXTURE_LIST));
      }
      if (url.endsWith("/api/v1/agents/archetypes/producteur")) {
        return Promise.resolve(jsonResponse(FIXTURE_DETAIL));
      }
      return Promise.resolve(jsonResponse({}, { status: 404 }));
    });

    renderRoute("/config/agents/new");

    const submit = await screen.findByRole("button", { name: /créer le template/i });
    expect(submit).toBeDisabled();

    // Add a name first — still no archetype → still disabled.
    const nameInput = screen.getByLabelText(/nom du template/i);
    fireEvent.change(nameInput, { target: { value: "My Template" } });
    expect(submit).toBeDisabled();

    // Pick an archetype → submit becomes enabled.
    fireEvent.click(screen.getByTestId("archetype-card-producteur"));
    await waitFor(() => expect(submit).toBeEnabled());
  });

  it("submits POST /agents/templates and redirects on success", async () => {
    const newTemplateId = "11111111-2222-3333-4444-555555555555";
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.endsWith("/api/v1/agents/archetypes")) {
        return Promise.resolve(jsonResponse(FIXTURE_LIST));
      }
      if (url.endsWith("/api/v1/agents/archetypes/producteur")) {
        return Promise.resolve(jsonResponse(FIXTURE_DETAIL));
      }
      if (url.endsWith("/api/v1/agents/templates") && method === "POST") {
        return Promise.resolve(
          jsonResponse(
            {
              template_id: newTemplateId,
              name: "Code Producer",
              archetype: "producteur",
              version: 1,
              created_at: new Date().toISOString(),
            },
            { status: 201 },
          ),
        );
      }
      return Promise.resolve(jsonResponse({}, { status: 404 }));
    });

    renderRoute("/config/agents/new");

    fireEvent.change(await screen.findByLabelText(/nom du template/i), {
      target: { value: "Code Producer" },
    });
    fireEvent.click(screen.getByTestId("archetype-card-producteur"));

    const submit = screen.getByRole("button", { name: /créer le template/i });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    // Wait for the navigation: the placeholder template detail page exposes
    // the template_id in a <code> block.
    await screen.findByText(newTemplateId);

    // Verify the POST request was sent with the right body.
    const postCall = fetchMock.mock.calls.find(([, init]) => {
      const i = init as RequestInit | undefined;
      return (i?.method ?? "GET").toUpperCase() === "POST";
    });
    expect(postCall).toBeDefined();
    const [, postInit] = postCall as [unknown, RequestInit];
    expect(JSON.parse(postInit.body as string)).toEqual({
      archetype: "producteur",
      name: "Code Producer",
    });
  });
});
