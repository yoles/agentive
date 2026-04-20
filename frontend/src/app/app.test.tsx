/**
 * Smoke test — vérifie que le RouterProvider + AppLayout se montent
 * et que la Sidebar expose les 4 espaces MVP.
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { Providers } from "@/app/providers";
import { routeTree } from "@/app/routeTree.gen";

describe("App smoke", () => {
  it("renders the sidebar with 4 navigation items", async () => {
    const router = createRouter({
      routeTree,
      history: createMemoryHistory({ initialEntries: ["/dashboard"] }),
    });

    render(
      <Providers>
        <RouterProvider router={router} />
      </Providers>,
    );

    // `findByText` attend que le router ait rendu (async)
    expect(await screen.findByText("Agentive")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Dashboard/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Chat/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Trace/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Config/i })).toBeInTheDocument();
  });
});
